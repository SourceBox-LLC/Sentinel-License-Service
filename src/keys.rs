//! License key generation and hashing.
//!
//! A 128-bit random token, hashed with plain SHA-256 before storage. No
//! salt, deliberately: this is a high-entropy random token, not a
//! low-entropy password, so there is no dictionary to defend against and
//! a deterministic hash is what makes the lookup possible. Same reasoning
//! already applied to MCP keys and CameraNode API keys.
//!
//! The raw value is generated once, shown once by `issue`, and never
//! persisted or logged.

use sha2::{Digest, Sha256};

pub const KEY_PREFIX: &str = "slk_";

/// `slk_` + 32 hex characters, matching Python's
/// `KEY_PREFIX + secrets.token_hex(16)`.
pub fn generate_key() -> String {
    let mut bytes = [0u8; 16];
    // Straight from the OS CSPRNG. This used `rand::rngs::OsRng`, but
    // rand 0.10 removed OsRng from that path entirely; rather than chase
    // it across another rand reshuffle, `getrandom` is the thing rand was
    // calling underneath anyway, and for key material the shorter, more
    // obviously-correct call is the better dependency.
    getrandom::fill(&mut bytes).expect("OS entropy is available");
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    format!("{KEY_PREFIX}{hex}")
}

pub fn hash_key(raw_key: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(raw_key.as_bytes());
    // Explicit hex rather than `format!("{:x}", ..)`: sha2 0.11 changed
    // finalize() to return a type that no longer implements LowerHex.
    //
    // This function's output IS the stored key_hash. A silently different
    // encoding would orphan every license row rather than fail a build,
    // which is why the fixed vector below is a test and not a comment.
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// The last four characters, stored alongside the hash so an operator can
/// identify a key in a list without holding the key itself.
pub fn last4(raw_key: &str) -> String {
    let n = raw_key.chars().count();
    raw_key.chars().skip(n.saturating_sub(4)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_keys_have_the_documented_shape() {
        let k = generate_key();
        assert!(k.starts_with("slk_"));
        assert_eq!(k.len(), 4 + 32, "slk_ plus 32 hex chars");
        assert!(k[4..].chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn generated_keys_are_not_repeated() {
        let a = generate_key();
        let b = generate_key();
        assert_ne!(a, b);
    }

    #[test]
    fn hash_matches_python_sha256_hexdigest() {
        // Fixed vector, so a hashing change can never silently orphan
        // every license row in production:
        //   python -c "import hashlib; \
        //     print(hashlib.sha256(b'slk_test').hexdigest())"
        assert_eq!(
            hash_key("slk_test"),
            "34207d1a9c32cd0acb47b1f1d0e232f6b9f9049bb44e589e15ca2572153737e1"
        );
    }

    #[test]
    fn last4_takes_the_tail() {
        assert_eq!(last4("slk_abcdef1234"), "1234");
        assert_eq!(last4("ab"), "ab");
    }
}
