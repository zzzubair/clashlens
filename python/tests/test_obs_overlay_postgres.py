"""OBS overlay against the real schema: the website's saves through the
private API, the overlay's reads by its private link, a new link retiring the
old one for good, and the collector checking an open overlay's player first,
30 seconds apart, as its own database role."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_discord_bot_postgres import DISCORD_ID, _account, _verify

from clashlens import api_overlays, collector_overlays
from clashlens.api import ApiError
from clashlens.api_db import ApiDatabase
from clashlens.collector_db import REVISIT_INTERVAL, CollectorDatabase

# Before 04:55 UTC, so regular checks are open.
NOW = datetime(2020, 9, 11, 4, 0, tzinfo=UTC)


def _players(connection_info: str, *tags: str) -> dict[str, int]:
    with psycopg.connect(connection_info, autocommit=True) as connection:
        return {
            tag: int(
                connection.execute(
                    "INSERT INTO players (normalized_tag, active, next_due_at)"
                    " VALUES (%s, true, %s) RETURNING id",
                    (tag, NOW - timedelta(minutes=5)),
                ).fetchone()[0]
            )
            for tag in tags
        }


def _as_collector(connection_info: str) -> CollectorDatabase:
    options = conninfo_to_dict(connection_info).get("options", "")
    return CollectorDatabase(
        make_conninfo(connection_info, options=f"{options} -c role=clashlens_collector")
    )


def test_overlay_saves_reads_by_link_and_a_new_link_retires_the_old(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            _players(connection_info, "#2PP", "#8PY")
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            theirs = _account(owner, "google", "someone-else", "other")
            _verify(owner, mine, "#2PP")
            _verify(owner, theirs, "#8PY")

            first = api_overlays.get_overlay(api, mine)
            assert first["players"] == [{"tag": "#2PP", "name": None, "league": None}]
            assert first["tag"] is None and first["version"] == 1
            assert first["settings"] == api_overlays.DEFAULTS
            # Asking again keeps the same link: one row per account.
            assert api_overlays.get_overlay(api, mine)["link"] == first["link"]

            # Another account's player is never shown.
            with pytest.raises(ApiError) as refused:
                api_overlays.save_overlay(api, mine, tag="#8PY", settings={})
            assert refused.value.status_code == 404

            saved = api_overlays.save_overlay(
                api, mine, tag="#2PP",
                settings={"w": "1110", "h": "66", "font": "esports", "bg": "glitter",
                          "show": "trophies,rank", "script": "<b>", "x": "-4"},
            )
            assert saved["version"] == 2
            shown = api_overlays.read_link(api, first["link"])
            assert shown == {
                "kind": "obs-overlay-link",
                "tag": "#2PP",
                "version": 2,
                # Unknown settings and values are dropped.
                "settings": {"w": "1110", "h": "66", "font": "esports", "show": "trophies,rank"},
            }

            # The first read marks the overlay open; later reads within a
            # minute write nothing.
            with psycopg.connect(connection_info, autocommit=True) as connection:
                seen = connection.execute(
                    "SELECT last_seen_at FROM obs_overlays WHERE account_id = %s", (mine,)
                ).fetchone()[0]
                assert seen is not None
                api_overlays.read_link(api, first["link"])
                assert connection.execute(
                    "SELECT last_seen_at FROM obs_overlays WHERE account_id = %s", (mine,)
                ).fetchone()[0] == seen

            replaced = api_overlays.new_link(api, mine, replaces=first["link"])
            assert replaced["link"] != first["link"]
            assert replaced["settings"] == shown["settings"]
            # The old link shows nothing from now on, and asking twice with
            # it makes no second new link.
            assert api_overlays.read_link(api, first["link"]) is None
            again = api_overlays.new_link(api, mine, replaces=first["link"])
            assert again["link"] == replaced["link"]
            assert api_overlays.read_link(api, replaced["link"])["tag"] == "#2PP"

            # A player that leaves the account stops being shown.
            _verify(owner, theirs, "#2PP")
            assert api_overlays.read_link(api, replaced["link"])["tag"] is None
        finally:
            owner.close()
            api.close()


def test_an_open_overlays_player_is_checked_first_30_seconds_apart(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        collector = _as_collector(connection_info)
        try:
            ids = _players(connection_info, "#2PP", "#8PY", "#9QQ")
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, mine, "#2PP")
            link = api_overlays.get_overlay(api, mine)["link"]
            api_overlays.save_overlay(api, mine, tag="#2PP", settings={})
            # Saving alone does not mark it open: only OBS reading the link does.
            assert collector_overlays.players_in_use(collector) == []
            api_overlays.read_link(api, link)
            assert collector_overlays.players_in_use(collector) == [(ids["#2PP"], "#2PP")]

            # Checked 30 seconds ago: the regular check is a minute away, but
            # an open overlay's player is due now, ahead of overdue players.
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute(
                    "UPDATE players SET next_due_at = %s WHERE id = %s",
                    (NOW - timedelta(seconds=30) + REVISIT_INTERVAL, ids["#2PP"]),
                )
            due_by = NOW + REVISIT_INTERVAL - collector_overlays.OVERLAY_GAP
            regular = collector.claim_due_players(limit=10, now=NOW - timedelta(seconds=1))
            assert sorted(work.normalized_tag for work in regular) == ["#8PY", "#9QQ"]
            claimed = collector.claim_due_players(
                limit=10, now=NOW, only=[ids["#2PP"]], due_by=due_by
            )
            assert [work.normalized_tag for work in claimed] == ["#2PP"]
            # Just checked: the next one is 30 seconds away, not sooner.
            assert collector.claim_due_players(
                limit=10, now=NOW, only=[ids["#2PP"]], due_by=due_by
            ) == []
            later = NOW + collector_overlays.OVERLAY_GAP
            assert [
                work.normalized_tag
                for work in collector.claim_due_players(
                    limit=10, now=later, only=[ids["#2PP"]],
                    due_by=later + REVISIT_INTERVAL - collector_overlays.OVERLAY_GAP,
                )
            ] == ["#2PP"]

            # Five minutes after OBS last read the link, it no longer counts.
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute(
                    "UPDATE obs_overlays SET last_seen_at = clock_timestamp() - interval '301 seconds'"
                )
            assert collector_overlays.players_in_use(collector) == []
        finally:
            owner.close()
            api.close()
            collector.close()


def test_viewer_presence_moves_with_reads_the_clock_and_new_links(database_url: str) -> None:
    """Each ViewerPresence state and event: reads mark it seen, at most once
    a minute and only with a player chosen; the clock alone ages it; a new
    link closes it."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        collector = _as_collector(connection_info)
        try:
            ids = _players(connection_info, "#2PP")
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, mine, "#2PP")
            link = api_overlays.get_overlay(api, mine)["link"]
            with psycopg.connect(connection_info, autocommit=True) as connection:

                def state() -> tuple[str, Any]:
                    return connection.execute(
                        f"SELECT {collector_overlays.PRESENCE_SQL}, overlay.last_seen_at"
                        " FROM obs_overlays AS overlay WHERE overlay.account_id = %s",
                        (mine,),
                    ).fetchone()

                def seen(seconds_ago: int) -> None:
                    connection.execute(
                        "UPDATE obs_overlays SET last_seen_at ="
                        " clock_timestamp() - make_interval(secs => %s)",
                        (seconds_ago,),
                    )

                in_use = [(ids["#2PP"], "#2PP")]
                # No player chosen: OBS reading the link writes nothing.
                api_overlays.read_link(api, link)
                assert state() == ("closed", None)
                api_overlays.save_overlay(api, mine, tag="#2PP", settings={})
                assert state() == ("closed", None)
                # CLOSED -> JUST_SEEN on a read.
                api_overlays.read_link(api, link)
                presence, first = state()
                assert presence == "just_seen" and first is not None
                assert collector_overlays.players_in_use(collector) == in_use
                # JUST_SEEN: a read under a minute later writes nothing.
                seen(50)
                held = state()
                api_overlays.read_link(api, link)
                assert state() == held and held[0] == "just_seen"
                # The clock alone: JUST_SEEN -> OPEN, still in use; a read
                # then writes again.
                seen(61)
                assert state()[0] == "open"
                assert collector_overlays.players_in_use(collector) == in_use
                api_overlays.read_link(api, link)
                presence, again = state()
                assert presence == "just_seen" and again > held[1]
                # The clock alone: OPEN -> CLOSED; a read opens it again.
                seen(301)
                assert state()[0] == "closed"
                assert collector_overlays.players_in_use(collector) == []
                api_overlays.read_link(api, link)
                assert state()[0] == "just_seen"
                # A new link: CLOSED at once, and the old link opens nothing.
                new = api_overlays.new_link(api, mine, replaces=link)["link"]
                assert state() == ("closed", None)
                assert api_overlays.read_link(api, link) is None
                assert state() == ("closed", None)
                assert collector_overlays.players_in_use(collector) == []
                api_overlays.read_link(api, new)
                assert state()[0] == "just_seen"
                # Choosing no player closes it at once.
                api_overlays.save_overlay(api, mine, tag=None, settings={})
                assert state()[0] == "closed"
                assert collector_overlays.players_in_use(collector) == []
        finally:
            owner.close()
            api.close()
            collector.close()


def test_a_link_is_absent_then_current_then_retired_for_good(database_url: str) -> None:
    """With no overlay there is no link to replace; replacing the current one
    bumps the version once, and a retired one changes nothing."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            with pytest.raises(ApiError) as absent:
                api_overlays.new_link(api, mine, replaces="A" * 22)
            assert absent.value.status_code == 404
            current = api_overlays.get_overlay(api, mine)
            replaced = api_overlays.new_link(api, mine, replaces=current["link"])
            assert replaced["version"] == current["version"] + 1
            for retired in (current["link"], "B" * 22):
                assert api_overlays.new_link(api, mine, replaces=retired) == replaced
                assert api_overlays.read_link(api, retired) is None
        finally:
            owner.close()
            api.close()
