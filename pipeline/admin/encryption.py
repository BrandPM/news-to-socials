"""Per-brand credential encryption helpers (NTS_025).

Sanity/Telegram/Meta tokens are stored in ``admin.db`` Fernet-encrypted.
The master key lives in ``.env`` as ``BRANDS_ENCRYPTION_KEY`` on both Mac
and VPS (must match — otherwise stored ciphertexts cannot be decrypted).

Plaintext credentials are only resolved at the moment a publisher needs
them, inside a single ``run_pipeline`` invocation (M3 carve-out). They
MUST NOT be persisted on long-lived class attributes or module globals.

Usage::

    enc = get_encryption()
    ciphertext = enc.encrypt("secret-sanity-token")
    plaintext = enc.decrypt(ciphertext)

If the master key is empty (early dev, no key configured) every call
raises ``EncryptionNotConfiguredError`` so silent fallbacks are
impossible.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from pipeline.common.config import get_settings


class EncryptionNotConfiguredError(RuntimeError):
    """Raised when BRANDS_ENCRYPTION_KEY is missing/empty at use time."""


class BrandEncryption:
    """Thin wrapper around Fernet bound to the configured master key."""

    def __init__(self, key: str) -> None:
        if not key:
            raise EncryptionNotConfiguredError(
                "BRANDS_ENCRYPTION_KEY is empty — encryption not configured. "
                "Set it in .env on both Mac and VPS (same value)."
            )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "BRANDS_ENCRYPTION_KEY is not a valid Fernet key "
                "(expected 32 url-safe base64-encoded bytes)."
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("encrypt() expects str")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "ciphertext could not be decrypted with the configured "
                "BRANDS_ENCRYPTION_KEY — wrong key or corrupted blob"
            ) from exc

    def decrypt_or_none(self, ciphertext: str | None) -> str | None:
        if ciphertext is None or ciphertext == "":
            return None
        return self.decrypt(ciphertext)


_encryption: BrandEncryption | None = None


def get_encryption() -> BrandEncryption:
    """Return the process-wide BrandEncryption, creating it on first call."""
    global _encryption
    if _encryption is None:
        _encryption = BrandEncryption(get_settings().brands_encryption_key)
    return _encryption


def reset_for_tests() -> None:
    """Drop cached encryption — tests use this when changing the key."""
    global _encryption
    _encryption = None
