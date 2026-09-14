from __future__ import annotations

import hashlib
import json

from clashlens.response_fields import content_fingerprint


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _profile(**overrides: object) -> bytes:
    payload = {
        "tag": "#2PP",
        "name": "Synthetic Clasher",
        "trophies": 7000,
        "leagueTier": {"id": 105000036, "name": "Legend I"},
        "currentLeagueSeasonId": 1783918800,
        "previousLeagueSeasonId": 1781499600,
        "expLevel": 250,
        "bestTrophies": 7100,
        "clan": {"tag": "#2CLAN", "name": "Synthetic Clan"},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _battle_log(*items: object) -> bytes:
    return json.dumps({"items": list(items)}).encode()


def _legend_entry(**overrides: object) -> dict[str, object]:
    entry = {
        "battleType": "legend",
        "attack": True,
        "battleTime": 1785844800,
        "battleTimestamp": "20260804T120000.000Z",
        "stars": 3,
        "destructionPercentage": 100,
        "opponentPlayerTag": "#8PP",
        "opponentName": "Synthetic Defender",
        "lootedResources": [{"resource": "gold", "amount": 525000}],
    }
    entry.update(overrides)
    return entry


def test_ignored_profile_fields_do_not_change_the_fingerprint() -> None:
    first = _profile(donations=4, achievements=[{"name": "a"}])
    changed = _profile(donations=9, achievements=[{"name": "b"}])

    first_fp = content_fingerprint(
        "profile", first, http_status=200, response_hash=_digest(first)
    )
    changed_fp = content_fingerprint(
        "profile", changed, http_status=200, response_hash=_digest(changed)
    )

    assert first != changed
    assert first_fp == changed_fp
    assert first_fp != _digest(first)


def test_read_profile_fields_change_the_fingerprint() -> None:
    first = _profile(trophies=7000)
    changed = _profile(trophies=6990)

    first_fp = content_fingerprint(
        "profile", first, http_status=200, response_hash=_digest(first)
    )
    changed_fp = content_fingerprint(
        "profile", changed, http_status=200, response_hash=_digest(changed)
    )

    assert first_fp != changed_fp


def test_legend_statistics_and_role_change_the_fingerprint() -> None:
    first = _profile(role="member", legendStatistics={"legendTrophies": 100})
    promoted = _profile(role="leader")
    advanced = _profile(legendStatistics={"legendTrophies": 101})

    first_fp = content_fingerprint(
        "profile", first, http_status=200, response_hash=_digest(first)
    )

    assert (
        content_fingerprint(
            "profile", promoted, http_status=200, response_hash=_digest(promoted)
        )
        != first_fp
    )
    assert (
        content_fingerprint(
            "profile", advanced, http_status=200, response_hash=_digest(advanced)
        )
        != first_fp
    )


def test_clan_name_is_read_but_other_clan_fields_are_ignored() -> None:
    first = _profile()
    renamed = _profile(clan={"tag": "#2CLAN", "name": "Renamed"})
    noisy = _profile(
        clan={"tag": "#2CLAN", "name": "Synthetic Clan", "members": 42}
    )

    first_fp = content_fingerprint(
        "profile", first, http_status=200, response_hash=_digest(first)
    )

    assert (
        content_fingerprint(
            "profile", renamed, http_status=200, response_hash=_digest(renamed)
        )
        != first_fp
    )
    assert (
        content_fingerprint(
            "profile", noisy, http_status=200, response_hash=_digest(noisy)
        )
        == first_fp
    )


def test_non_legend_entries_do_not_change_the_fingerprint() -> None:
    legend = _legend_entry()
    first = _battle_log(legend, {"battleType": "homeVillage", "stars": 1})
    changed = _battle_log(legend, {"battleType": "friendly", "stars": 3})

    first_fp = content_fingerprint(
        "battle_log", first, http_status=200, response_hash=_digest(first)
    )
    changed_fp = content_fingerprint(
        "battle_log", changed, http_status=200, response_hash=_digest(changed)
    )

    assert first_fp == changed_fp


def test_legend_entry_fields_change_the_fingerprint() -> None:
    first = _battle_log(_legend_entry())
    changed = _battle_log(_legend_entry(stars=2))
    ignored = _battle_log(_legend_entry(opponentTownHallLevel=19))

    first_fp = content_fingerprint(
        "battle_log", first, http_status=200, response_hash=_digest(first)
    )

    assert (
        content_fingerprint(
            "battle_log", changed, http_status=200, response_hash=_digest(changed)
        )
        != first_fp
    )
    assert (
        content_fingerprint(
            "battle_log", ignored, http_status=200, response_hash=_digest(ignored)
        )
        == first_fp
    )


def test_unparseable_and_non_success_bodies_fall_back_to_the_raw_digest() -> None:
    body = b"not-json"
    digest = _digest(body)

    assert (
        content_fingerprint(
            "profile", body, http_status=200, response_hash=digest
        )
        == digest
    )
    assert (
        content_fingerprint(
            "profile", _profile(), http_status=429, response_hash=_digest(_profile())
        )
        == _digest(_profile())
    )


def test_endpoints_without_a_field_list_keep_byte_identity() -> None:
    body = _battle_log(_legend_entry())
    digest = _digest(body)

    for endpoint in ("global_player_rankings", "league_history"):
        assert (
            content_fingerprint(
                endpoint, body, http_status=200, response_hash=digest
            )
            == digest
        )
