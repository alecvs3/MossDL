//! MEGA-family AES-CTR decryption, applied to bytes as they arrive.
//!
//! MEGA and Transfer.it serve ciphertext and publish the key beside the link,
//! so the plaintext is produced during the transfer rather than by rewriting
//! the file afterwards.
//!
//! The Python transport rebuilt an AES cipher for every chunk and replayed a
//! zero buffer to re-seek the counter, which meant thousands of cipher objects
//! a second at speed. A CTR keystream is seekable, so a worker builds one
//! cipher for its range, seeks once, and then decrypts in place.

use aes::Aes128;
use ctr::cipher::{KeyIvInit, StreamCipher, StreamCipherSeek};

/// MEGA counts its counter in 64-bit big-endian blocks, with the file nonce as
/// the high half of the IV.
type Aes128Ctr = ctr::Ctr64BE<Aes128>;

pub struct MegaCtr {
    cipher: Aes128Ctr,
}

impl MegaCtr {
    /// Build a cipher from MEGA's 8-word file key.
    ///
    /// The AES key is the first four words XORed with the last four; words four
    /// and five are the nonce. Shorter keys belong to folder links and cannot
    /// decrypt a file.
    pub fn new(key_a32: &[u32]) -> Result<Self, String> {
        if key_a32.len() < 8 {
            return Err(format!(
                "encrypted download needs an 8-word file key, got {}",
                key_a32.len()
            ));
        }
        let mut key = [0_u8; 16];
        for index in 0..4 {
            let word = key_a32[index] ^ key_a32[index + 4];
            key[index * 4..index * 4 + 4].copy_from_slice(&word.to_be_bytes());
        }
        let mut iv = [0_u8; 16];
        iv[0..4].copy_from_slice(&key_a32[4].to_be_bytes());
        iv[4..8].copy_from_slice(&key_a32[5].to_be_bytes());
        // iv[8..16] stays zero: the counter starts at block zero and seek()
        // moves it to wherever this worker's range begins.
        Ok(Self {
            cipher: Aes128Ctr::new(&key.into(), &iv.into()),
        })
    }

    /// Move the keystream to `offset` bytes into the plaintext.
    pub fn seek(&mut self, offset: u64) {
        self.cipher.seek(offset);
    }

    /// Decrypt in place, continuing from the current keystream position.
    pub fn apply(&mut self, data: &mut [u8]) {
        self.cipher.apply_keystream(data);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // A file key as MEGA publishes it: eight 32-bit words.
    const KEY: [u32; 8] = [
        0x0011_2233,
        0x4455_6677,
        0x8899_aabb,
        0xccdd_eeff,
        0x0102_0304,
        0x0506_0708,
        0xdead_beef,
        0xfeed_face,
    ];

    fn decrypt_whole(data: &[u8]) -> Vec<u8> {
        let mut cipher = MegaCtr::new(&KEY).unwrap();
        let mut buffer = data.to_vec();
        cipher.apply(&mut buffer);
        buffer
    }

    #[test]
    fn a_short_key_is_refused() {
        assert!(MegaCtr::new(&KEY[..4]).is_err());
    }

    #[test]
    fn ctr_is_its_own_inverse() {
        let plaintext = b"the quick brown fox jumps over the lazy dog".to_vec();
        let ciphertext = decrypt_whole(&plaintext);
        assert_ne!(ciphertext, plaintext, "the keystream did nothing");
        assert_eq!(decrypt_whole(&ciphertext), plaintext);
    }

    #[test]
    fn seeking_matches_decrypting_from_the_start() {
        // This is what lets a segment worker start mid-file: the bytes it
        // produces must match the ones a single sequential pass would.
        let plaintext: Vec<u8> = (0..4096_u32).map(|value| value as u8).collect();
        let whole = decrypt_whole(&plaintext);

        for offset in [1_u64, 15, 16, 17, 1024, 4095] {
            let mut cipher = MegaCtr::new(&KEY).unwrap();
            cipher.seek(offset);
            let mut tail = plaintext[offset as usize..].to_vec();
            cipher.apply(&mut tail);
            assert_eq!(tail, whole[offset as usize..], "offset {offset} diverged");
        }
    }

    #[test]
    fn decrypting_in_pieces_matches_one_call() {
        let plaintext: Vec<u8> = (0..1000_u32).map(|value| (value * 7) as u8).collect();
        let whole = decrypt_whole(&plaintext);

        let mut cipher = MegaCtr::new(&KEY).unwrap();
        let mut pieced = Vec::new();
        for chunk in plaintext.chunks(7) {
            let mut buffer = chunk.to_vec();
            cipher.apply(&mut buffer);
            pieced.extend_from_slice(&buffer);
        }
        assert_eq!(pieced, whole);
    }
}
