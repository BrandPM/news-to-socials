"""NTS_058 Task 6 — password hashing + validation unit tests."""

from __future__ import annotations

import bcrypt
import pytest

from pipeline.admin.passwords import (
    ValidationError,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)


def test_hash_is_bcrypt_cost_12_and_verifies() -> None:
    h = hash_password("a-good-long-password")
    assert h.startswith("$2b$12$")  # bcrypt, cost 12
    assert verify_password("a-good-long-password", h) is True
    assert verify_password("wrong-password-xx", h) is False


def test_hash_is_salted_unique() -> None:
    a = hash_password("same-password-here")
    b = hash_password("same-password-here")
    assert a != b  # distinct salts
    assert verify_password("same-password-here", a)
    assert verify_password("same-password-here", b)


def test_verify_never_raises_on_garbage() -> None:
    assert verify_password("", "") is False
    assert verify_password("x", "not-a-hash") is False


@pytest.mark.parametrize("u", ["abc", "andriy", "a_b.c-1", "user.name", "x" * 32])
def test_valid_usernames(u: str) -> None:
    assert validate_username(u) == u.strip()


@pytest.mark.parametrize(
    "u",
    ["ab", "x" * 33, "Andriy", "has space", "emoji😀", "UPPER", "tab\tx", ""],
)
def test_invalid_usernames(u: str) -> None:
    with pytest.raises(ValidationError):
        validate_username(u)


def test_password_min_length_enforced() -> None:
    with pytest.raises(ValidationError):
        validate_password("short")  # < 12
    assert validate_password("x" * 12) == "x" * 12


def test_password_max_bytes_enforced() -> None:
    # bcrypt only sees the first 72 bytes — reject longer to avoid silent
    # truncation.
    with pytest.raises(ValidationError):
        validate_password("x" * 73)


def test_hash_rejects_too_short() -> None:
    with pytest.raises(ValidationError):
        hash_password("short")
