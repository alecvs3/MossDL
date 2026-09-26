//! RAR through the user's own 7-Zip.
//!
//! MossDL ships no RAR code: RARLAB's UnRAR source is freeware with a use
//! restriction, not open source, and 7-Zip's RAR decoder carries the same
//! restriction. An installed 7-Zip (free, 7-zip.org) does the work instead.
//! The worker's own rules still apply: limits are checked from the listing
//! before anything is written, extraction goes to a staging directory, every
//! extracted path is re-checked, and cancellation stops the extractor.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub const MISSING: &str = "RAR archives need 7-Zip (free, from 7-zip.org). Install it, then retry the extraction.";

/// 7z.exe: MOSSDL_7ZIP, the standard install folders, then PATH.
pub fn seven_zip() -> Option<PathBuf> {
    if let Some(configured) = std::env::var_os("MOSSDL_7ZIP").map(PathBuf::from) {
        if configured.is_file() {
            return Some(configured);
        }
    }
    let mut candidates: Vec<PathBuf> = ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"]
        .iter()
        .filter_map(|var| std::env::var_os(var))
        .map(|root| PathBuf::from(root).join("7-Zip").join("7z.exe"))
        .collect();
    if let Some(path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path) {
            candidates.push(dir.join(if cfg!(windows) { "7z.exe" } else { "7z" }));
        }
    }
    candidates.into_iter().find(|p| p.is_file())
}

fn command(tool: &Path) -> Command {
    let mut cmd = Command::new(tool);
    // Never prompt (for a password or an overwrite): no stdin, -y, -p- style answers.
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    cmd
}

pub struct Entry {
    pub path: String,
    pub directory: bool,
    pub size: u64,
    pub compressed_size: u64,
    pub encrypted: bool,
}

/// `7z l -slt` technical listing, parsed into entries.
pub fn list(archive: &Path) -> Result<Vec<Entry>, String> {
    let tool = seven_zip().ok_or(MISSING)?;
    let output = command(&tool)
        .args(["l", "-slt", "-ba", "-p", "-sccUTF-8", "--"])
        .arg(archive)
        .output()
        .map_err(|e| format!("could not run 7-Zip: {e}"))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr);
        return Err(format!("7-Zip could not read the archive: {}", detail.trim()));
    }
    Ok(parse_listing(&String::from_utf8_lossy(&output.stdout)))
}

pub fn parse_listing(text: &str) -> Vec<Entry> {
    let mut entries = Vec::new();
    for block in text.replace("\r\n", "\n").split("\n\n") {
        let field = |name: &str| {
            block.lines().find_map(|line| line.strip_prefix(name).and_then(|rest| rest.strip_prefix(" = ")).map(str::to_string))
        };
        let Some(path) = field("Path") else { continue };
        entries.push(Entry {
            path: path.replace('\\', "/"),
            directory: field("Folder").as_deref() == Some("+") || field("Attributes").is_some_and(|a| a.starts_with('D')),
            size: field("Size").and_then(|s| s.parse().ok()).unwrap_or(0),
            compressed_size: field("Packed Size").and_then(|s| s.parse().ok()).unwrap_or(0),
            encrypted: field("Encrypted").as_deref() == Some("+"),
        });
    }
    entries
}

/// Extract everything into `staging` (which must exist and be empty).
pub fn extract(archive: &Path, staging: &Path, cancel: &Arc<AtomicBool>) -> Result<(), String> {
    let tool = seven_zip().ok_or(MISSING)?;
    let mut child = command(&tool)
        .args(["x", "-y", "-p", "-aoa"])
        .arg(format!("-o{}", staging.display()))
        .arg("--")
        .arg(archive)
        .spawn()
        .map_err(|e| format!("could not run 7-Zip: {e}"))?;
    loop {
        if cancel.load(Ordering::SeqCst) {
            let _ = child.kill();
            let _ = child.wait();
            return Err("cancelled".into());
        }
        match child.try_wait().map_err(|e| e.to_string())? {
            Some(status) if status.success() => return Ok(()),
            Some(status) => {
                let mut detail = String::new();
                if let Some(mut stderr) = child.stderr.take() {
                    use std::io::Read;
                    let _ = stderr.read_to_string(&mut detail);
                }
                return Err(format!("7-Zip failed ({status}): {}", detail.trim()));
            }
            None => std::thread::sleep(Duration::from_millis(100)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_technical_listing() {
        let listing = "Path = Game/part.bin\r\nFolder = -\r\nSize = 1048576\r\nPacked Size = 524288\r\nEncrypted = -\r\n\r\nPath = Game\r\nFolder = +\r\nSize = 0\r\n\r\nPath = secret.txt\r\nSize = 10\r\nEncrypted = +\r\n";
        let entries = parse_listing(listing);
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0].path, "Game/part.bin");
        assert_eq!((entries[0].size, entries[0].compressed_size), (1_048_576, 524_288));
        assert!(entries[1].directory);
        assert!(entries[2].encrypted);
    }
}
