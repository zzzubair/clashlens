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

# Authoritative inappropriate-name filter. The website mirrors this rule only
# for early feedback; Python rejection always wins.
_INAPPROPRIATE_TERMS = (
    "anal", "anus", "arse", "asshole", "bastard", "bitch", "blowjob",
    "bollocks", "boner", "bullshit", "butthole", "clitoris", "cock",
    "cocksucker", "cunt", "dick", "dildo", "douche", "fag", "faggot",
    "fuck", "gangbang", "gook", "handjob", "hitler", "homo", "jackass",
    "jerkoff", "kike", "killyourself", "kys", "nazi", "nigg", "nipple",
    "orgasm", "paki", "pedo", "penis", "piss", "porn", "prick", "pussy",
    "queef", "rape", "rapist", "retard", "scrotum", "sex", "shit", "slut",
    "spic", "suckmydick", "tits", "titty", "tranny", "twat", "vagina",
    "wank", "whore",
)
_LEET_REPLACEMENTS = (
    ("@", "a"),
    ("r0", "or"),
    ("0", "o"),
    ("1", "i"),
    ("!", "i"),
    ("|", "i"),
    ("*", "i"),
    ("3", "e"),
    ("4", "a"),
    ("5", "s"),
    ("$", "s"),
    ("7", "t"),
    ("8", "b"),
    ("9", "g"),
    ("2", "z"),
    ("6", "g"),
    ("v", "u"),
)


def _filter_normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    for original, replacement in _LEET_REPLACEMENTS:
        without_marks = without_marks.replace(original, replacement)
    return "".join(
        character for character in without_marks if character in _ASCII_NAME_CHARS
    )


_ASCII_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def is_inappropriate_name(value: str) -> bool:
    normalized = _filter_normalize(value)
    if not normalized:
        return False
    return any(term in normalized for term in _INAPPROPRIATE_TERMS)


def normalize_username(value: str) -> str:
    normalized = value.strip().lower()
    if not _USERNAME_RE.fullmatch(normalized) or normalized in _RESERVED_USERNAMES:
        raise ValueError("username must be a safe non-reserved ASCII name")
    if is_inappropriate_name(normalized):
        raise ValueError("username was not accepted")
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
    if is_inappropriate_name(normalized):
        raise ValueError(f"{label} was not accepted")
    return normalized
