//! Checksum algorithms the engine's providers actually hand us.
//!
//! A transfer always reports its SHA-256 so the engine has one stable identity
//! for the artifact, but providers verify with whatever they publish -- MD5 and
//! SHA-1 are still common on older hosts. Both digests come from a single pass
//! over the file: reading a finished download twice to hash it is the kind of
//! cost that shows up on large archives.

use md5::Md5;
use sha1::Sha1;
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Algorithm {
    Sha256,
    Sha1,
    Md5,
}

impl Algorithm {
    pub fn parse(name: &str) -> Option<Self> {
        match name.trim().to_ascii_lowercase().as_str() {
            "sha256" | "sha-256" => Some(Self::Sha256),
            "sha1" | "sha-1" => Some(Self::Sha1),
            "md5" => Some(Self::Md5),
            _ => None,
        }
    }
}

/// Split an `algorithm:hex` checksum into its parts.
///
/// A bare hex digest is read as SHA-256, which is what the engine sent before
/// any algorithm was named.
pub fn split(checksum: &str) -> Result<(Algorithm, String), String> {
    let checksum = checksum.trim();
    match checksum.split_once(':') {
        None => Ok((Algorithm::Sha256, checksum.to_ascii_lowercase())),
        Some((name, digest)) => match Algorithm::parse(name) {
            Some(algorithm) => Ok((algorithm, digest.trim().to_ascii_lowercase())),
            None => Err(format!("unsupported checksum algorithm: {name}")),
        },
    }
}

enum Secondary {
    Sha1(Sha1),
    Md5(Md5),
}

/// Computes SHA-256 plus, when the expected checksum uses one, a second digest.
pub struct MultiHasher {
    sha256: Sha256,
    secondary: Option<Secondary>,
}

impl MultiHasher {
    pub fn new(expected: Option<Algorithm>) -> Self {
        Self {
            sha256: Sha256::new(),
            secondary: match expected {
                // SHA-256 is already being computed; asking for it twice would
                // just hash every byte a second time.
                None | Some(Algorithm::Sha256) => None,
                Some(Algorithm::Sha1) => Some(Secondary::Sha1(Sha1::new())),
                Some(Algorithm::Md5) => Some(Secondary::Md5(Md5::new())),
            },
        }
    }

    pub fn update(&mut self, data: &[u8]) {
        self.sha256.update(data);
        match self.secondary.as_mut() {
            Some(Secondary::Sha1(hasher)) => hasher.update(data),
            Some(Secondary::Md5(hasher)) => hasher.update(data),
            None => {}
        }
    }

    /// Returns `(sha256, digest_for_the_expected_algorithm)`.
    pub fn finish(self) -> (String, String) {
        let sha256 = format!("{:x}", self.sha256.finalize());
        let secondary = match self.secondary {
            Some(Secondary::Sha1(hasher)) => format!("{:x}", hasher.finalize()),
            Some(Secondary::Md5(hasher)) => format!("{:x}", hasher.finalize()),
            None => sha256.clone(),
        };
        (sha256, secondary)
    }
}

/// Whether `digest` satisfies `checksum`, where `digest` was produced by the
/// algorithm `checksum` names.
pub fn matches(checksum: Option<&str>, digest: &str) -> bool {
    match checksum {
        None => true,
        Some(value) => match split(value) {
            Ok((_, expected)) => digest.eq_ignore_ascii_case(&expected),
            Err(_) => false,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bare_digest_is_read_as_sha256() {
        let (algorithm, digest) = split("ABC123").unwrap();
        assert_eq!(algorithm, Algorithm::Sha256);
        assert_eq!(digest, "abc123");
    }

    #[test]
    fn named_algorithms_are_split_off() {
        assert_eq!(split("md5:d41d8c").unwrap(), (Algorithm::Md5, "d41d8c".to_string()));
        assert_eq!(split("sha-1:da39a3").unwrap(), (Algorithm::Sha1, "da39a3".to_string()));
    }

    #[test]
    fn unknown_algorithms_are_refused() {
        assert!(split("crc32:deadbeef").is_err());
    }

    #[test]
    fn both_digests_come_from_one_pass() {
        let mut hasher = MultiHasher::new(Some(Algorithm::Md5));
        hasher.update(b"hello ");
        hasher.update(b"world");
        let (sha256, md5) = hasher.finish();
        assert_eq!(sha256, "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9");
        assert_eq!(md5, "5eb63bbbe01eeed093cb22bb8f5acdc3");
    }

    #[test]
    fn sha256_is_not_hashed_twice() {
        let mut hasher = MultiHasher::new(Some(Algorithm::Sha256));
        hasher.update(b"hello world");
        let (sha256, expected) = hasher.finish();
        assert_eq!(sha256, expected);
    }

    #[test]
    fn matching_is_case_insensitive_and_algorithm_aware() {
        assert!(matches(None, "anything"));
        assert!(matches(Some("md5:5EB63BBBE01EEED093CB22BB8F5ACDC3"), "5eb63bbbe01eeed093cb22bb8f5acdc3"));
        assert!(!matches(Some("md5:0000"), "5eb63bbbe01eeed093cb22bb8f5acdc3"));
        assert!(!matches(Some("crc32:0000"), "0000"), "an unknown algorithm never matches");
    }
}
