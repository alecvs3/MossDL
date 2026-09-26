//! Durable coverage journal. Only the disk writer acknowledges extents.
use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, UNIX_EPOCH};

#[derive(Serialize, Deserialize)]
pub struct Journal {
    schema: u32,
    size: u64,
    identity: String,
    created_ns: u128,
    ranges: Vec<(u64, u64)>, // half-open, sorted and coalesced
    #[serde(skip)]
    path: PathBuf,
}

fn creation(file: &File) -> u128 {
    file.metadata().and_then(|m| m.created()).ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_nanos()).unwrap_or(0)
}

impl Journal {
    pub fn open(path: PathBuf, file: &File, size: u64, identity: String) -> Self {
        let created_ns = creation(file);
        if !identity.is_empty() && created_ns != 0 {
            if let Ok(raw) = fs::read(&path) {
                if let Ok(mut old) = serde_json::from_slice::<Self>(&raw) {
                    let mut cursor = 0;
                    let valid = old.ranges.iter().all(|&(start, end)| {
                        let ok = start >= cursor && end > start && end <= size;
                        cursor = end;
                        ok
                    });
                    if old.schema == 1 && old.identity == identity && old.size == size
                        && old.created_ns == created_ns && valid
                        && file.metadata().map(|m| m.len() == size).unwrap_or(false) {
                        old.path = path;
                        return old;
                    }
                }
                eprintln!("[RESUME_INVALIDATED] incompatible checkpoint or partial file");
            }
        }
        Self { schema: 1, size, identity, created_ns, ranges: vec![], path }
    }

    pub fn record(&mut self, start: u64, len: u64) -> Result<(), String> {
        let end = start.checked_add(len).ok_or("extent overflow")?;
        if end > self.size { return Err("write outside declared file".into()); }
        if len == 0 { return Ok(()); }
        self.ranges.push((start, end));
        self.ranges.sort_unstable();
        let mut merged: Vec<(u64, u64)> = vec![];
        for &(a, b) in &self.ranges {
            if let Some(last) = merged.last_mut() {
                if a <= last.1 { last.1 = last.1.max(b); continue; }
            }
            merged.push((a, b));
        }
        self.ranges = merged;
        Ok(())
    }

    pub fn completed(&self) -> u64 { self.ranges.iter().map(|(a, b)| b - a).sum() }

    pub fn pending(&self, width: usize) -> Vec<(u64, u64)> {
        let chunk = (self.size / width.max(1) as u64).max(1);
        let mut output = vec![];
        let mut cursor = 0;
        for &(a, b) in self.ranges.iter().chain(std::iter::once(&(self.size, self.size))) {
            while cursor < a {
                let end = cursor.saturating_add(chunk).min(a);
                output.push((cursor, end - 1));
                cursor = end;
            }
            cursor = b;
        }
        output
    }

    pub fn save(&self, file: &File) -> Result<(), String> {
        file.sync_all().map_err(|e| e.to_string())?;
        let temporary = self.path.with_extension("json.tmp");
        let mut out = File::create(&temporary).map_err(|e| e.to_string())?;
        out.write_all(&serde_json::to_vec(self).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
        out.sync_all().map_err(|e| e.to_string())?;
        drop(out);
        replace(&temporary, &self.path)
    }
}

pub fn replace(source: &Path, destination: &Path) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        #[link(name = "kernel32")]
        extern "system" { fn MoveFileExW(a: *const u16, b: *const u16, flags: u32) -> i32; }
        let a: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
        let b: Vec<u16> = destination.as_os_str().encode_wide().chain(Some(0)).collect();
        if unsafe { MoveFileExW(a.as_ptr(), b.as_ptr(), 0x1 | 0x8) } == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        Ok(())
    }
    #[cfg(not(windows))]
    { fs::rename(source, destination).map_err(|e| e.to_string()) }
}

/// How often the journal (and the data it vouches for) is made durable. A crash
/// loses at most this much transfer time: at 15 s a 30 MB/s download killed at
/// 6 s kept nothing and fetched 1.4x the file. The data has to reach the disk
/// anyway, so flushing more often costs little extra.
pub const CHECKPOINT_EVERY: Duration = Duration::from_secs(3);

pub fn checkpoint_due(last: Instant) -> bool { last.elapsed() >= CHECKPOINT_EVERY }

#[cfg(test)]
mod tests {
    use super::*;
    fn empty() -> Journal {
        Journal { schema: 1, size: 100, identity: "v1".into(), created_ns: 1,
                  ranges: vec![], path: PathBuf::new() }
    }
    #[test]
    fn coverage_not_sparse_length_proves_completion() {
        let mut j = empty();
        j.record(0, 51).unwrap();
        assert_eq!(j.completed(), 51);
        assert_eq!(j.pending(1), vec![(51, 99)]);
        j.record(75, 25).unwrap();
        assert_eq!(j.pending(1), vec![(51, 74)]);
        j.record(40, 40).unwrap();
        assert_eq!(j.completed(), 100);
        assert!(j.pending(4).is_empty());
    }
    #[test]
    fn bounds_are_enforced() {
        assert!(empty().record(90, 11).is_err());
        assert!(empty().record(u64::MAX, 10).is_err());
    }
}
