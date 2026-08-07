from __future__ import annotations

import pytest

from clashlens.accounts import (
    normalize_display_name,
    normalize_group_name,
    normalize_username,
)


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("Legend_Player", "legend_player"),
        ("  player123  ", "player123"),
    ],
)
def test_username_normalization_is_ascii_case_insensitive(
    supplied: str,
    expected: str,
) -> None:
    assert normalize_username(supplied) == expected


@pytest.mark.parametrize(
    "supplied",
    [
        "ab",
        "a" * 33,
        "9player",
        "player-name",
        "pláyer",
        "admin",
        "support",
        "api",
    ],
)
def test_username_rejects_unsafe_or_reserved_values(supplied: str) -> None:
    with pytest.raises(ValueError, match="username"):
        normalize_username(supplied)


def test_display_and_group_names_trim_without_case_folding() -> None:
    assert normalize_display_name("  Legend Pushers  ") == "Legend Pushers"
    assert normalize_group_name("  My Accounts  ") == "My Accounts"


@pytest.mark.parametrize("supplied", ["", "   ", "bad\nname", "x" * 81])
def test_display_and_group_names_reject_empty_controlled_or_long_values(
    supplied: str,
) -> None:
    with pytest.raises(ValueError):
        normalize_display_name(supplied)
    with pytest.raises(ValueError):
        normalize_group_name(supplied)
