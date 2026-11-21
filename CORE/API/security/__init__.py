"""API security primitives.

The package replaces the old `security.py` module. The previous public
symbols (`hash_api_key`, `verify_api_key`) remain importable as
`from security import ...` for backward compatibility; new modules
(`password_policy`) live alongside as submodules.
"""

import bcrypt


def hash_api_key(api_key: str) -> str:
    """Hashes an API key using bcrypt.

    Bcrypt has a 72-byte password limit, so we truncate the key if needed.
    This is safe because 72 bytes still provides plenty of entropy for security.
    """
    # Truncate to 72 bytes (bcrypt's maximum)
    key_bytes = api_key.encode('utf-8')
    if len(key_bytes) > 72:
        key_bytes = key_bytes[:72]

    # Use bcrypt directly
    hashed = bcrypt.hashpw(key_bytes, bcrypt.gensalt())
    return hashed.decode('utf-8')


def verify_api_key(plain_api_key: str, hashed_api_key: str) -> bool:
    """Verifies a plain API key against its hashed version.

    Must truncate to 72 bytes to match the hash_api_key behavior.
    """
    # Truncate to 72 bytes (same logic as hash_api_key)
    key_bytes = plain_api_key.encode('utf-8')
    if len(key_bytes) > 72:
        key_bytes = key_bytes[:72]

    return bcrypt.checkpw(key_bytes, hashed_api_key.encode('utf-8'))


__all__ = ["hash_api_key", "verify_api_key"]
