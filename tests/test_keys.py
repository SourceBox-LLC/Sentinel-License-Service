from app.core.keys import KEY_PREFIX, generate_key, hash_key


def test_generate_key_has_expected_shape():
    key = generate_key()
    assert key.startswith(KEY_PREFIX)
    assert len(key) == len(KEY_PREFIX) + 32  # 16 bytes -> 32 hex chars


def test_generate_key_is_random():
    assert generate_key() != generate_key()


def test_hash_key_is_deterministic():
    key = generate_key()
    assert hash_key(key) == hash_key(key)


def test_hash_key_differs_for_different_keys():
    assert hash_key(generate_key()) != hash_key(generate_key())


def test_hash_key_is_sha256_hexdigest_shape():
    assert len(hash_key(generate_key())) == 64
