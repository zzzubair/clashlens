from __future__ import annotations

import re
import unicodedata

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$", flags=re.ASCII)
_RESERVED_USERNAMES = frozenset(
    {
        "account",
        "admin",
        "analytics",
        "api",
        "clashlens",
        "groups",
        "leaderboard",
        "login",
        "logout",
        "players",
        "settings",
        "support",
        "users",
    }
)
_MAX_NAME_LENGTH = 80


def normalize_username(value: str) -> str:
    normalized = value.strip().lower()
    if not _USERNAME_RE.fullmatch(normalized) or normalized in _RESERVED_USERNAMES:
        raise ValueError("username must be a safe non-reserved ASCII name")
    return normalized


def normalize_display_name(value: str) -> str:
    return _normalize_name(value, "display name")


def normalize_group_name(value: str) -> str:
    return _normalize_name(value, "group name")


def _normalize_name(value: str, label: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > _MAX_NAME_LENGTH:
        raise ValueError(
            f"{label} must contain between 1 and {_MAX_NAME_LENGTH} characters"
        )
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(f"{label} must not contain control characters")
    return normalized
