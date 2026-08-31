"""License key generation and hashing.

Mirrors Sentinel Command Center's backend/app/api/mcp_keys.py pattern:
a random high-entropy token, hashed with plain SHA-256 before storage
(no salt needed — this is a 128-bit random token, not a low-entropy
password; TLS + possession is the entire trust model, same reasoning
already applied to MCP keys and CameraNode API keys in that codebase).
The raw value is generated once, shown once by the issuance CLI, and
never persisted or logged anywhere.
"""

import hashlib
import secrets

KEY_PREFIX = "slk_"


def generate_key() -> str:
    """Generate a new raw license key: slk_ + 32 hex chars."""
    return KEY_PREFIX + secrets.token_hex(16)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
