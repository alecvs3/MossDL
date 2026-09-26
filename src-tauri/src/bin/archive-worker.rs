//! Sandboxed archive worker.
//!
//! The worker is intentionally a separate process. It performs metadata
//! preflight before extraction, writes only to a staging directory, and
//! atomically promotes successful output. RAR goes through the user's own
//! 7-Zip (archive_worker/rar_external.rs): no RAR code ships with MossDL.

use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use sevenz_rust2::{ArchiveReader, Password};
use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tar::Archive as TarArchive;
use zip::ZipArchive;

#[path = "archive_worker/rar_external.rs"]
mod rar_external;

#[derive(Debug, Deserialize)]
struct RpcRequest {
    id: serde_json::Value,
    method: String,
    params: serde_json::Value,
}

#[derive(Debug, Serialize)]
struct RpcResponse {
    jsonrpc: &'static str,
    id: serde_json::Value,
    result: Option<serde_json::Value>,
    error: Option<RpcError>,
}

#[derive(Debug, Serialize)]
struct RpcError {
    code: &'static str,
    message: String,
}

#[derive(Debug, Clone, Deserialize)]
struct ArchivePolicy {
    #[serde(default = "default_max_output")]
    max_output_bytes: u64,
    #[serde(default = "default_max_files")]
    max_file_count: u64,
    #[serde(default = "default_max_depth")]
    max_nesting_depth: u32,
    #[serde(default = "default_max_ratio")]
    max_compression_ratio: u64,
    #[serde(default = "default_min_free")]
    minimum_free_space: u64,
}

fn default_max_output() -> u64 {
    500 * 1024 * 1024 * 1024
}
fn default_max_files() -> u64 {
    100_000
}
fn default_max_depth() -> u32 {
    32
}
fn default_max_ratio() -> u64 {
    1_000
}
fn default_min_free() -> u64 {
    1024 * 1024 * 1024
}

impl Default for ArchivePolicy {
    fn default() -> Self {
        Self {
            max_output_bytes: default_max_output(),
            max_file_count: default_max_files(),
            max_nesting_depth: default_max_depth(),
            max_compression_ratio: default_max_ratio(),
            minimum_free_space: default_min_free(),
        }
    }
}

#[derive(Debug, Deserialize, Clone)]
struct ArchiveParams {
    path: String,
    #[serde(default)]
    output_directory: Option<String>,
    #[serde(default)]
    password_ref: Option<String>,
    #[serde(default)]
    job_id: Option<String>,
    #[serde(default)]
    policy: ArchivePolicy,
}

#[derive(Debug, Serialize, Clone)]
struct ArchiveEntrySummary {
    path: String,
    directory: bool,
    size: u64,
    compressed_size: u64,
    encrypted: bool,
}

#[derive(Debug, Serialize)]
struct ArchiveSummary {
    format: String,
    entries: Vec<ArchiveEntrySummary>,
    expanded_bytes: u64,
    encrypted: bool,
}

#[derive(Debug, Serialize, Default)]
struct ExtractResult {
    output_directory: String,
    files: u64,
    bytes: u64,
    status: String,
    sentinel_path: Option<String>,
    promoted_files: u64,
    skipped_existing_files: u64,
    staging_removed: bool,
}

#[derive(Debug, Default, Clone, Copy)]
struct PromoteOutcome {
    promoted_files: u64,
    skipped_existing_files: u64,
}

impl ExtractResult {
    fn completed(output: &Path, budget: &Budget, outcome: PromoteOutcome) -> Self {
        Self {
            output_directory: output.display().to_string(),
            files: budget.files,
            bytes: budget.bytes,
            status: "completed".into(),
            sentinel_path: None,
            promoted_files: outcome.promoted_files,
            skipped_existing_files: outcome.skipped_existing_files,
            staging_removed: true,
        }
    }
}

struct Budget {
    bytes: u64,
    files: u64,
    max_bytes: u64,
    max_files: u64,
    cancel: Arc<AtomicBool>,
}

impl Budget {
    fn check(&self) -> io::Result<()> {
        if self.cancel.load(Ordering::Relaxed) {
            return Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "archive job canceled",
            ));
        }
        Ok(())
    }

    fn add_file(&mut self) -> io::Result<()> {
        self.check()?;
        self.files = self
            .files
            .checked_add(1)
            .ok_or_else(|| io::Error::other("file count overflow"))?;
        if self.files > self.max_files {
            return Err(io::Error::other("archive file-count limit exceeded"));
        }
        Ok(())
    }
}

fn safe_relative(name: &str, max_depth: u32) -> Result<PathBuf, String> {
    let normalized = name.replace('\\', "/");
    if normalized.is_empty() || normalized.starts_with('/') || normalized.contains(':') {
        return Err(format!("unsafe archive path: {name}"));
    }
    let mut output = PathBuf::new();
    let mut depth = 0;
    for part in normalized.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            return Err(format!("archive path traversal rejected: {name}"));
        }
        depth += 1;
        if depth > max_depth {
            return Err("archive nesting-depth limit exceeded".into());
        }
        output.push(part);
    }
    if output.as_os_str().is_empty() {
        return Err(format!("empty archive path: {name}"));
    }
    for component in output.components() {
        if matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        ) {
            return Err(format!("unsafe archive path: {name}"));
        }
    }
    Ok(output)
}

fn entry_key(path: &Path) -> String {
    let value = path.to_string_lossy().replace('\\', "/");
    if cfg!(windows) {
        value.to_lowercase()
    } else {
        value
    }
}

fn validate_entries(
    entries: &[ArchiveEntrySummary],
    policy: &ArchivePolicy,
    archive: &Path,
) -> Result<u64, String> {
    if entries.len() as u64 > policy.max_file_count {
        return Err("archive file-count limit exceeded".into());
    }
    let mut paths = HashSet::new();
    let mut expanded = 0u64;
    for entry in entries {
        let safe = safe_relative(&entry.path, policy.max_nesting_depth)?;
        if !paths.insert(entry_key(&safe)) {
            return Err(format!("duplicate archive path: {}", entry.path));
        }
        if entry.size > 0
            && entry.compressed_size > 0
            && entry.size / entry.compressed_size.max(1) > policy.max_compression_ratio
        {
            return Err(format!("compression-ratio limit exceeded: {}", entry.path));
        }
        expanded = expanded
            .checked_add(entry.size)
            .ok_or_else(|| "expanded size overflow".to_string())?;
    }
    let free = fs::metadata(archive)
        .ok()
        .and_then(|_| fs2_free(archive.parent().unwrap_or(Path::new("."))).ok())
        .unwrap_or(u64::MAX);
    let free_budget = free.saturating_mul(8) / 10;
    let allowed = policy.max_output_bytes.min(free_budget);
    if expanded > allowed {
        return Err(format!("archive output exceeds policy: {expanded} bytes"));
    }
    if free != u64::MAX && free < expanded.saturating_add(policy.minimum_free_space) {
        return Err("not enough free space for archive extraction".into());
    }
    Ok(expanded)
}

#[cfg(windows)]
fn fs2_free(path: &Path) -> io::Result<u64> {
    fs2::available_space(path).map_err(|e| io::Error::other(e.to_string()))
}
#[cfg(not(windows))]
fn fs2_free(path: &Path) -> io::Result<u64> {
    fs2::available_space(path).map_err(|e| io::Error::other(e.to_string()))
}

fn format_for(path: &Path) -> Result<String, String> {
    let mut header = [0u8; 8];
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let count = file.read(&mut header).map_err(|e| e.to_string())?;
    let extension = path
        .extension()
        .and_then(|v| v.to_str())
        .unwrap_or_default()
        .to_lowercase();
    if count >= 7 && &header[..7] == b"Rar!\x1A\x07\x00" {
        return Ok("rar".into());
    }
    if count >= 8 && &header[..8] == b"Rar!\x1A\x07\x01\x00" {
        return Ok("rar".into());
    }
    if count >= 6 && &header[..6] == b"7z\xBC\xAF'\x1C" {
        return Ok("7z".into());
    }
    if count >= 2 && &header[..2] == b"PK" {
        return Ok("zip".into());
    }
    if count >= 2 && header[..2] == [0x1f, 0x8b] {
        if path
            .file_stem()
            .and_then(|v| v.to_str())
            .unwrap_or_default()
            .to_lowercase()
            .ends_with(".tar")
        {
            return Ok("tar.gz".into());
        }
        return Ok("gzip".into());
    }
    if extension == "tar" {
        return Ok("tar".into());
    }
    if extension == "rar" || extension.starts_with('r') && extension.len() >= 3 && extension[1..].chars().all(|c| c.is_ascii_digit()) {
        return Ok("rar".into());
    }
    Err("unsupported archive format".into())
}

fn zip_entries(path: &Path) -> Result<Vec<ArchiveEntrySummary>, String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut archive = ZipArchive::new(file).map_err(|e| e.to_string())?;
    let mut entries = Vec::with_capacity(archive.len());
    for index in 0..archive.len() {
        let file = archive.by_index_raw(index).map_err(|e| e.to_string())?;
        let mode = file.unix_mode().unwrap_or(0);
        if mode & 0o170000 == 0o120000 {
            return Err(format!("symlink archive entry rejected: {}", file.name()));
        }
        entries.push(ArchiveEntrySummary {
            path: file.name().to_string(),
            directory: file.is_dir(),
            size: file.size(),
            compressed_size: file.compressed_size(),
            encrypted: false,
        });
    }
    Ok(entries)
}

fn sevenz_entries(
    path: &Path,
    password_ref: Option<&str>,
) -> Result<Vec<ArchiveEntrySummary>, String> {
    if password_ref.is_some() {
        return Err("password_ref must be resolved by the supervisor; plaintext passwords are never accepted by archive-worker".into());
    }
    let reader = ArchiveReader::open(path, Password::empty()).map_err(|e| e.to_string())?;
    Ok(reader
        .archive()
        .files
        .iter()
        .map(|entry| ArchiveEntrySummary {
            path: entry.name.clone(),
            directory: entry.is_directory,
            size: entry.size,
            compressed_size: entry.compressed_size,
            encrypted: false,
        })
        .collect())
}

fn tar_entries(path: &Path, gzip: bool) -> Result<Vec<ArchiveEntrySummary>, String> {
    let reader: Box<dyn Read> = if gzip {
        Box::new(GzDecoder::new(File::open(path).map_err(|e| e.to_string())?))
    } else {
        Box::new(File::open(path).map_err(|e| e.to_string())?)
    };
    let mut archive = TarArchive::new(reader);
    let mut entries = Vec::new();
    for item in archive.entries().map_err(|e| e.to_string())? {
        let entry = item.map_err(|e| e.to_string())?;
        let kind = entry.header().entry_type();
        if kind.is_symlink() || kind.is_hard_link() || !kind.is_file() && !kind.is_dir() {
            return Err(format!("unsafe TAR entry rejected: {:?}", kind));
        }
        let path = entry
            .path()
            .map_err(|e| e.to_string())?
            .to_string_lossy()
            .to_string();
        entries.push(ArchiveEntrySummary {
            path,
            directory: kind.is_dir(),
            size: entry.size(),
            compressed_size: entry.size(),
            encrypted: false,
        });
    }
    Ok(entries)
}

fn gzip_entries(path: &Path) -> Vec<ArchiveEntrySummary> {
    let name = path
        .file_name()
        .and_then(|v| v.to_str())
        .unwrap_or("archive");
    let entry_path = name.strip_suffix(".gz").unwrap_or(name).to_string();
    vec![ArchiveEntrySummary {
        path: entry_path,
        directory: false,
        size: 0,
        compressed_size: fs::metadata(path).map(|v| v.len()).unwrap_or(0),
        encrypted: false,
    }]
}

fn list_entries(params: &ArchiveParams) -> Result<ArchiveSummary, String> {
    let path = Path::new(&params.path);
    if !path.is_file() {
        return Err("archive path is not a regular file".into());
    }
    let format = format_for(path)?;
    let entries = match format.as_str() {
        "rar" => rar_entries(path, params.password_ref.as_deref())?,
        "zip" => zip_entries(path)?,
        "7z" => sevenz_entries(path, params.password_ref.as_deref())?,
        "tar" => tar_entries(path, false)?,
        "tar.gz" => tar_entries(path, true)?,
        "gzip" => gzip_entries(path),
        _ => return Err("unsupported archive format".into()),
    };
    let expanded = validate_entries(&entries, &params.policy, path)?;
    Ok(ArchiveSummary {
        format,
        encrypted: entries.iter().any(|entry| entry.encrypted),
        entries,
        expanded_bytes: expanded,
    })
}

fn copy_limited<R: Read + ?Sized, W: Write>(
    reader: &mut R,
    writer: &mut W,
    budget: &mut Budget,
) -> io::Result<u64> {
    let mut buffer = [0u8; 1024 * 1024];
    let mut copied = 0;
    loop {
        budget.check()?;
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        let next = budget
            .bytes
            .checked_add(count as u64)
            .ok_or_else(|| io::Error::other("output size overflow"))?;
        if next > budget.max_bytes {
            return Err(io::Error::other("archive output-size limit exceeded"));
        }
        writer.write_all(&buffer[..count])?;
        budget.bytes = next;
        copied += count as u64;
    }
    Ok(copied)
}

const STAGING_PREFIX: &str = ".transfer-archive-staging-";
const COMPLETE_PREFIX: &str = ".transfer-archive-complete-";

fn safe_job_id(job_id: &str) -> String {
    job_id
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect()
}

fn staging_directory(output: &Path, job_id: &str) -> Result<PathBuf, String> {
    let parent = output.parent().ok_or("archive output has no parent")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    Ok(parent.join(format!("{STAGING_PREFIX}{}", safe_job_id(job_id))))
}

fn completion_sentinel_path(output: &Path, job_id: &str) -> PathBuf {
    output.join(format!("{COMPLETE_PREFIX}{}", safe_job_id(job_id)))
}

fn write_completion_sentinel(output: &Path, job_id: &str) -> Result<PathBuf, String> {
    let sentinel = completion_sentinel_path(output, job_id);
    let payload = serde_json::json!({"job_id": job_id, "state": "completed"}).to_string();
    let mut file = File::create(&sentinel).map_err(|e| format!("write completion sentinel: {e}"))?;
    file.write_all(payload.as_bytes())
        .and_then(|_| file.write_all(b"\n"))
        .and_then(|_| file.sync_all())
        .map_err(|e| format!("write completion sentinel: {e}"))?;
    Ok(sentinel)
}

fn remove_staging(staging: &Path) {
    if !staging.exists() {
        return;
    }
    if let Err(error) = fs::remove_dir_all(staging) {
        eprintln!(
            "archive-worker: staging cleanup failed for {}: {error}",
            staging.display()
        );
    }
}

fn files_equal(first: &Path, second: &Path) -> Result<bool, String> {
    let first_size = fs::metadata(first).map_err(|e| e.to_string())?.len();
    let second_size = fs::metadata(second).map_err(|e| e.to_string())?.len();
    if first_size != second_size {
        return Ok(false);
    }
    let mut left = File::open(first).map_err(|e| e.to_string())?;
    let mut right = File::open(second).map_err(|e| e.to_string())?;
    // Heap buffers: inlined compare frames must never consume the worker
    // thread's 2 MiB stack, even when this branch is not taken.
    let mut left_buffer = vec![0u8; 64 * 1024];
    let mut right_buffer = vec![0u8; 64 * 1024];
    loop {
        let left_count = left.read(&mut left_buffer).map_err(|e| e.to_string())?;
        let right_count = right.read(&mut right_buffer).map_err(|e| e.to_string())?;
        if left_count != right_count || left_buffer[..left_count] != right_buffer[..right_count] {
            return Ok(false);
        }
        if left_count == 0 {
            return Ok(true);
        }
    }
}

fn promote(staging: &Path, output: &Path) -> Result<PromoteOutcome, String> {
    // Multipart archives are extracted beside their downloaded volumes. The
    // output directory therefore legitimately already contains the source
    // .rar files. Validate the staged tree before moving anything so an
    // existing file is never overwritten and a conflict leaves the staging
    // tree intact for diagnosis/retry.
    let mut staged_files = Vec::new();
    let mut directories = Vec::new();
    let mut pending = vec![staging.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            let path = entry.path();
            let relative = path
                .strip_prefix(staging)
                .map_err(|e| e.to_string())?
                .to_path_buf();
            if path.is_dir() {
                directories.push(relative);
                pending.push(path);
            } else if path.is_file() {
                staged_files.push(relative);
            } else {
                return Err(format!("unsupported staged archive entry: {}", path.display()));
            }
        }
    }

    if !output.exists() {
        fs::rename(staging, output).map_err(|e| e.to_string())?;
        return Ok(PromoteOutcome {
            promoted_files: staged_files.len() as u64,
            skipped_existing_files: 0,
        });
    }
    if !output.is_dir() {
        return Err("archive output path is not a directory".into());
    }

    for relative in directories.iter().rev() {
        let destination = output.join(relative);
        if destination.exists() && !destination.is_dir() {
            return Err(format!("archive output path conflicts with directory: {}", destination.display()));
        }
    }
    // Retries after a crash must be resumable: byte-identical files were already
    // promoted by the previous attempt and are skipped, differing files are a
    // hard conflict so payload is never silently replaced.
    for relative in &staged_files {
        let destination = output.join(relative);
        if !destination.exists() {
            continue;
        }
        if destination.is_dir() {
            return Err(format!("archive output path conflicts with directory: {}", destination.display()));
        }
        if files_equal(&staging.join(relative), &destination)? {
            continue;
        }
        return Err(format!("archive output file differs from staged payload: {}", destination.display()));
    }

    let mut outcome = PromoteOutcome::default();
    for relative in directories.iter().rev() {
        fs::create_dir_all(output.join(relative)).map_err(|e| e.to_string())?;
    }
    for relative in staged_files {
        let destination = output.join(&relative);
        if destination.exists() {
            outcome.skipped_existing_files += 1;
            continue;
        }
        let source = staging.join(&relative);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        fs::rename(&source, &destination).map_err(|e| e.to_string())?;
        outcome.promoted_files += 1;
    }
    fs::remove_dir_all(staging).map_err(|e| format!("remove staging: {e}"))?;
    Ok(outcome)
}

fn extract_zip(
    params: &ArchiveParams,
    cancel: Arc<AtomicBool>,
    output: &Path,
) -> Result<ExtractResult, String> {
    let summary = list_entries(params)?;
    if summary.encrypted {
        return Err("password-protected ZIP requires a supervisor-resolved secret and is not accepted directly by archive-worker".into());
    }
    let staging = staging_directory(output, params.job_id.as_deref().unwrap_or("job"))?;
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|e| format!("remove staging: {e}"))?;
    }
    fs::create_dir_all(&staging).map_err(|e| format!("create staging: {e}"))?;
    let result = (|| {
        let file = File::open(&params.path).map_err(|e| e.to_string())?;
        let mut archive = ZipArchive::new(file).map_err(|e| e.to_string())?;
        let mut budget = Budget {
            bytes: 0,
            files: 0,
            max_bytes: summary.expanded_bytes.min(params.policy.max_output_bytes),
            max_files: params.policy.max_file_count,
            cancel,
        };
        for index in 0..archive.len() {
            budget.check().map_err(|e| e.to_string())?;
            let mut entry = match archive
                .by_index_decrypt(index, b"")
                .map_err(|e| e.to_string())?
            {
                Ok(entry) => entry,
                Err(_) => {
                    return Err(
                        "password-protected ZIP requires a supervisor-resolved secret".into(),
                    )
                }
            };
            let safe = safe_relative(entry.name(), params.policy.max_nesting_depth)?;
            let target = staging.join(safe);
            if entry.is_dir() {
                fs::create_dir_all(&target).map_err(|e| e.to_string())?;
                continue;
            }
            if entry.unix_mode().unwrap_or(0) & 0o170000 == 0o120000 {
                return Err("symlink archive entry rejected".into());
            }
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            let mut file = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&target)
                .map_err(|e| e.to_string())?;
            budget.add_file().map_err(|e| e.to_string())?;
            copy_limited(&mut entry, &mut file, &mut budget).map_err(|e| e.to_string())?;
        }
        let outcome = promote(&staging, output)?;
        Ok(ExtractResult::completed(output, &budget, outcome))
    })();
    if result.is_err() {
        remove_staging(&staging);
    }
    result
}

fn extract_tar(
    params: &ArchiveParams,
    cancel: Arc<AtomicBool>,
    output: &Path,
    gzip: bool,
) -> Result<ExtractResult, String> {
    let summary = list_entries(params)?;
    let staging = staging_directory(output, params.job_id.as_deref().unwrap_or("job"))?;
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|e| e.to_string())?;
    }
    fs::create_dir_all(&staging).map_err(|e| e.to_string())?;
    let result = (|| {
        let reader: Box<dyn Read> = if gzip {
            Box::new(GzDecoder::new(
                File::open(&params.path).map_err(|e| e.to_string())?,
            ))
        } else {
            Box::new(File::open(&params.path).map_err(|e| e.to_string())?)
        };
        let mut archive = TarArchive::new(reader);
        let mut budget = Budget {
            bytes: 0,
            files: 0,
            max_bytes: summary.expanded_bytes.min(params.policy.max_output_bytes),
            max_files: params.policy.max_file_count,
            cancel,
        };
        for item in archive.entries().map_err(|e| e.to_string())? {
            let mut entry = item.map_err(|e| e.to_string())?;
            budget.check().map_err(|e| e.to_string())?;
            let kind = entry.header().entry_type();
            if kind.is_symlink() || kind.is_hard_link() || !kind.is_file() && !kind.is_dir() {
                return Err("unsafe TAR entry rejected".into());
            }
            let safe = safe_relative(
                &entry.path().map_err(|e| e.to_string())?.to_string_lossy(),
                params.policy.max_nesting_depth,
            )?;
            let target = staging.join(safe);
            if kind.is_dir() {
                fs::create_dir_all(&target).map_err(|e| e.to_string())?;
                continue;
            }
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            let mut file = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&target)
                .map_err(|e| e.to_string())?;
            budget.add_file().map_err(|e| e.to_string())?;
            copy_limited(&mut entry, &mut file, &mut budget).map_err(|e| e.to_string())?;
        }
        let outcome = promote(&staging, output)?;
        Ok(ExtractResult::completed(output, &budget, outcome))
    })();
    if result.is_err() {
        remove_staging(&staging);
    }
    result
}

fn extract_gzip(
    params: &ArchiveParams,
    cancel: Arc<AtomicBool>,
    output: &Path,
) -> Result<ExtractResult, String> {
    let staging = staging_directory(output, params.job_id.as_deref().unwrap_or("job"))?;
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|e| format!("remove gzip staging: {e}"))?;
    }
    fs::create_dir_all(&staging).map_err(|e| format!("create gzip staging: {e}"))?;
    let result = (|| {
        let name = Path::new(&params.path)
            .file_name()
            .and_then(|v| v.to_str())
            .unwrap_or("archive")
            .strip_suffix(".gz")
            .unwrap_or("archive");
        let target = staging.join(safe_relative(name, params.policy.max_nesting_depth)?);
        let mut input =
            GzDecoder::new(File::open(&params.path).map_err(|e| format!("open gzip: {e}"))?);
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&target)
            .map_err(|e| format!("create gzip output: {e}"))?;
        let mut budget = Budget {
            bytes: 0,
            files: 0,
            max_bytes: params.policy.max_output_bytes,
            max_files: params.policy.max_file_count,
            cancel,
        };
        budget.add_file().map_err(|e| e.to_string())?;
        copy_limited(&mut input, &mut file, &mut budget).map_err(|e| format!("read gzip: {e}"))?;
        drop(file);
        let outcome =
            promote(&staging, output).map_err(|e| format!("promote gzip output: {e}"))?;
        Ok(ExtractResult::completed(output, &budget, outcome))
    })();
    if result.is_err() {
        remove_staging(&staging);
    }
    result
}

fn extract_sevenz(
    params: &ArchiveParams,
    cancel: Arc<AtomicBool>,
    output: &Path,
) -> Result<ExtractResult, String> {
    if params.password_ref.is_some() {
        return Err("password_ref must be resolved by the supervisor; plaintext passwords are never accepted by archive-worker".into());
    }
    let summary = list_entries(params)?;
    let staging = staging_directory(output, params.job_id.as_deref().unwrap_or("job"))?;
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|e| e.to_string())?;
    }
    fs::create_dir_all(&staging).map_err(|e| e.to_string())?;
    let result = (|| {
        let mut reader =
            ArchiveReader::open(&params.path, Password::empty()).map_err(|e| e.to_string())?;
        let mut budget = Budget {
            bytes: 0,
            files: 0,
            max_bytes: summary.expanded_bytes.min(params.policy.max_output_bytes),
            max_files: params.policy.max_file_count,
            cancel,
        };
        reader
            .for_each_entries(|entry, input| {
                budget.check().map_err(sevenz_rust2::Error::from)?;
                let safe = safe_relative(&entry.name, params.policy.max_nesting_depth)
                    .map_err(io::Error::other)?;
                if entry.is_directory {
                    fs::create_dir_all(staging.join(safe)).map_err(sevenz_rust2::Error::from)?;
                    return Ok(true);
                }
                let target = staging.join(safe);
                if let Some(parent) = target.parent() {
                    fs::create_dir_all(parent).map_err(sevenz_rust2::Error::from)?;
                }
                let mut file = OpenOptions::new()
                    .create_new(true)
                    .write(true)
                    .open(&target)
                    .map_err(sevenz_rust2::Error::from)?;
                budget.add_file().map_err(sevenz_rust2::Error::from)?;
                copy_limited(input, &mut file, &mut budget).map_err(sevenz_rust2::Error::from)?;
                Ok(true)
            })
            .map_err(|e| e.to_string())?;
        let outcome = promote(&staging, output)?;
        Ok(ExtractResult::completed(output, &budget, outcome))
    })();
    if result.is_err() {
        remove_staging(&staging);
    }
    result
}

fn rar_entries(
    path: &Path,
    password_ref: Option<&str>,
) -> Result<Vec<ArchiveEntrySummary>, String> {
    if password_ref.is_some() {
        return Err("password_ref must be resolved by the supervisor; plaintext passwords are never accepted by archive-worker".into());
    }
    let entries = rar_external::list(path)?
        .into_iter()
        .map(|entry| ArchiveEntrySummary {
            path: entry.path,
            directory: entry.directory,
            size: entry.size,
            compressed_size: entry.compressed_size,
            encrypted: entry.encrypted,
        })
        .collect();
    Ok(entries)
}

fn extract_rar(
    params: &ArchiveParams,
    cancel: Arc<AtomicBool>,
    output: &Path,
) -> Result<ExtractResult, String> {
    if params.password_ref.is_some() {
        return Err("password_ref must be resolved by the supervisor; plaintext passwords are never accepted by archive-worker".into());
    }
    let summary = list_entries(params)?;
    if summary.encrypted {
        return Err("encrypted archive requires a supervisor-resolved secret".into());
    }
    let staging = staging_directory(output, params.job_id.as_deref().unwrap_or("job"))?;
    if staging.exists() {
        fs::remove_dir_all(&staging).map_err(|e| e.to_string())?;
    }
    fs::create_dir_all(&staging).map_err(|e| e.to_string())?;

    let result = (|| {
        let mut budget = Budget {
            bytes: 0,
            files: 0,
            max_bytes: summary.expanded_bytes.min(params.policy.max_output_bytes),
            max_files: params.policy.max_file_count,
            cancel,
        };

        // Limits were checked from the listing (list_entries above) before this
        // writes anything; the staged tree is re-validated by promote().
        rar_external::extract(Path::new(&params.path), &staging, &budget.cancel)?;
        for entry in rar_external::list(Path::new(&params.path))? {
            if !entry.directory {
                safe_relative(&entry.path, params.policy.max_nesting_depth)?;
                budget.add_file().map_err(|e| e.to_string())?;
                budget.bytes = budget.bytes.saturating_add(entry.size);
            }
        }
        if budget.bytes > budget.max_bytes {
            return Err("archive output-size limit exceeded".into());
        }

        let outcome = promote(&staging, output)?;
        Ok(ExtractResult::completed(output, &budget, outcome))
    })();

    if result.is_err() {
        remove_staging(&staging);
    }
    result
}

fn archive_operation(
    method: &str,
    params: ArchiveParams,
    cancel: Arc<AtomicBool>,
) -> Result<serde_json::Value, String> {
    let summary = list_entries(&params)?;
    match method {
        "probe" | "list" => serde_json::to_value(summary).map_err(|e| e.to_string()),
        "test" => {
            if summary.encrypted {
                return Err("encrypted archive requires a supervisor-resolved secret".into());
            }
            let job_id: String = params
                .job_id
                .as_deref()
                .unwrap_or("test")
                .chars()
                .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
                .collect();
            let temporary = std::env::temp_dir().join(format!("transfer-archive-test-{job_id}"));
            if temporary.exists() {
                fs::remove_dir_all(&temporary).map_err(|e| e.to_string())?;
            }
            let result = match summary.format.as_str() {
                "rar" => extract_rar(&params, cancel.clone(), &temporary),
                "zip" => extract_zip(&params, cancel.clone(), &temporary),
                "7z" => extract_sevenz(&params, cancel.clone(), &temporary),
                "tar" => extract_tar(&params, cancel.clone(), &temporary, false),
                "tar.gz" => extract_tar(&params, cancel.clone(), &temporary, true),
                "gzip" => extract_gzip(&params, cancel.clone(), &temporary),
                _ => Err("unsupported archive format".into()),
            }?;
            fs::remove_dir_all(&temporary).map_err(|e| e.to_string())?;
            Ok(
                serde_json::json!({"format": summary.format, "valid": true, "expanded_bytes": result.bytes}),
            )
        }
        "extract" => {
            let output = PathBuf::from(
                params
                    .output_directory
                    .as_ref()
                    .ok_or("output_directory is required for extract")?,
            );
            if output.as_os_str().is_empty() {
                return Err("empty output_directory".into());
            }
            let mut result = match summary.format.as_str() {
                "rar" => extract_rar(&params, cancel, &output)?,
                "zip" => extract_zip(&params, cancel, &output)?,
                "7z" => extract_sevenz(&params, cancel, &output)?,
                "tar" => extract_tar(&params, cancel, &output, false)?,
                "tar.gz" => extract_tar(&params, cancel, &output, true)?,
                "gzip" => extract_gzip(&params, cancel, &output)?,
                _ => return Err("unsupported archive format".into()),
            };
            // Sentinel is written only after promote succeeded and staging is
            // gone, so its presence proves the payload is complete on disk.
            if let Some(job_id) = params.job_id.as_deref() {
                let sentinel = write_completion_sentinel(&output, job_id)?;
                result.sentinel_path = Some(sentinel.display().to_string());
            }
            result.status = "completed".into();
            serde_json::to_value(result).map_err(|e| e.to_string())
        }
        _ => Err(format!("unknown archive method: {method}")),
    }
}

fn response_ok(id: serde_json::Value, result: serde_json::Value) -> RpcResponse {
    RpcResponse {
        jsonrpc: "2.0",
        id,
        result: Some(result),
        error: None,
    }
}
fn response_err(id: serde_json::Value, message: String) -> RpcResponse {
    RpcResponse {
        jsonrpc: "2.0",
        id,
        result: None,
        error: Some(RpcError {
            code: "ARCHIVE_FAILED",
            message,
        }),
    }
}

fn main() {
    let stdin = io::stdin();
    let output = Arc::new(Mutex::new(io::stdout()));
    let cancellations: Arc<Mutex<std::collections::HashMap<String, Arc<AtomicBool>>>> =
        Arc::new(Mutex::new(std::collections::HashMap::new()));
    let mut jobs = Vec::new();
    for line in stdin.lock().lines() {
        let request = match line
            .ok()
            .and_then(|value| serde_json::from_str::<RpcRequest>(&value).ok())
        {
            Some(request) => request,
            None => {
                continue;
            }
        };
        if request.method == "cancel" {
            let job_id = request
                .params
                .get("job_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            let canceled = cancellations
                .lock()
                .ok()
                .and_then(|map| map.get(&job_id).cloned())
                .map(|flag| {
                    flag.store(true, Ordering::Relaxed);
                    true
                })
                .unwrap_or(false);
            let response = response_ok(request.id, serde_json::json!({"canceled": canceled}));
            if let Ok(mut writer) = output.lock() {
                writeln!(writer, "{}", serde_json::to_string(&response).unwrap()).ok();
                writer.flush().ok();
            }
            continue;
        }
        let params = match serde_json::from_value::<ArchiveParams>(request.params) {
            Ok(params) => params,
            Err(error) => {
                let response = response_err(request.id, format!("invalid params: {error}"));
                if let Ok(mut writer) = output.lock() {
                    writeln!(writer, "{}", serde_json::to_string(&response).unwrap()).ok();
                }
                continue;
            }
        };
        let job_id = params
            .job_id
            .clone()
            .unwrap_or_else(|| request.id.to_string());
        let cancel = Arc::new(AtomicBool::new(false));
        if let Ok(mut map) = cancellations.lock() {
            map.insert(job_id.clone(), cancel.clone());
        }
        let output_clone = output.clone();
        let cancellations_clone = cancellations.clone();
        jobs.push(std::thread::spawn(move || {
            let response = match archive_operation(&request.method, params, cancel) {
                Ok(result) => response_ok(request.id, result),
                Err(error) => response_err(request.id, error),
            };
            if let Ok(mut writer) = output_clone.lock() {
                writeln!(writer, "{}", serde_json::to_string(&response).unwrap()).ok();
                writer.flush().ok();
            }
            if let Ok(mut map) = cancellations_clone.lock() {
                map.remove(&job_id);
            }
        }));
    }
    for job in jobs {
        let _ = job.join();
    }
}
