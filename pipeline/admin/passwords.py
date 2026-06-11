"""Password hashing + validation for admin_users (NTS_058).

bcrypt with cost factor 12. We never store or log plaintext — only the
hash goes to the DB, and callers mask the password in any log line.

bcrypt has a 72-byte input limit; longer inputs are silently truncated by
the algorithm. We hash the input as-is (passwords 12–72 chars are the
expected range) but guard absurdly long inputs to avoid surprising
truncation semantics.
"""

from __future__ import annotations

import re

import bcrypt

BCRYPT_ROUNDS = 12

USERNAME_RE = re.compile(r"^[a-z0-9_.-]+$")
USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 12
# bcrypt only considers the first 72 bytes; reject longer so we never
# silently accept a password whose tail is ignored.
PASSWORD_MAX = 72


class ValidationError(ValueError):
    """Raised for invalid username/password — message is safe to surface."""


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValidationError("username must be a string")
    u = username.strip()
    if not (USERNAME_MIN <= len(u) <= USERNAME_MAX):
        raise ValidationError(
            f"username must be {USERNAME_MIN}-{USERNAME_MAX} characters"
        )
    if not USERNAME_RE.match(u):
        raise ValidationError(
            "username may contain only lowercase letters, digits, '_', '.', '-'"
        )
    return u


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise ValidationError("password must be a string")
    # Length is measured in bytes for the bcrypt limit; most passwords are
    # ASCII so this matches character count, but be precise for unicode.
    nbytes = len(password.encode("utf-8"))
    if len(password) < PASSWORD_MIN:
        raise ValidationError(f"password must be at least {PASSWORD_MIN} characters")
    if nbytes > PASSWORD_MAX:
        raise ValidationError(
            f"password must be at most {PASSWORD_MAX} bytes (bcrypt limit)"
        )
    return password


def hash_password(password: str) -> str:
    """Return a bcrypt hash string. Validates length first."""
    validate_password(password)
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt verify. Never raises on bad input → False."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("ascii")
        )
    except (ValueError, TypeError):
        return False
