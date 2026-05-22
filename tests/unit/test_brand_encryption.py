"""Tests for pipeline.admin.encryption (Fernet master-key wrapper)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from pipeline.admin import encryption as enc_mod
from pipeline.admin.encryption import (
    BrandEncryption,
    EncryptionNotConfiguredError,
)


@pytest.fixture(autouse=True)
def _clear_cached_singleton():
    enc_mod.reset_for_tests()
    yield
    enc_mod.reset_for_tests()


def test_roundtrip_encrypts_then_decrypts() -> None:
    key = Fernet.generate_key().decode("ascii")
    enc = BrandEncryption(key)
    ciphertext = enc.encrypt("super-secret-token")
    assert ciphertext != "super-secret-token"  # actually encrypted
    assert enc.decrypt(ciphertext) == "super-secret-token"


def test_empty_key_raises_not_configured() -> None:
    with pytest.raises(EncryptionNotConfiguredError):
        BrandEncryption("")


def test_invalid_key_raises_value_error() -> None:
    with pytest.raises(ValueError):
        BrandEncryption("not-a-real-fernet-key-just-arbitrary-text-aaaaaaa")


def test_decrypt_with_wrong_key_raises_value_error() -> None:
    """A ciphertext encrypted with key A must fail to decrypt with key B —
    otherwise the master-key invariant is broken."""
    key_a = Fernet.generate_key().decode("ascii")
    key_b = Fernet.generate_key().decode("ascii")
    enc_a = BrandEncryption(key_a)
    enc_b = BrandEncryption(key_b)
    ciphertext = enc_a.encrypt("hello")
    with pytest.raises(ValueError):
        enc_b.decrypt(ciphertext)


def test_decrypt_or_none_handles_empty_and_none() -> None:
    enc = BrandEncryption(Fernet.generate_key().decode("ascii"))
    assert enc.decrypt_or_none(None) is None
    assert enc.decrypt_or_none("") is None
    ciphertext = enc.encrypt("ok")
    assert enc.decrypt_or_none(ciphertext) == "ok"
