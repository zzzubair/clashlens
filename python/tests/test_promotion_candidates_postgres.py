"""The Legend II and III list a Monday re-check reads (migration 0076)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_domain_processing_postgres import _processor

from clashlens import promotion_candidates

PROFILE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
OBSERVED_AT = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
LEGEND_II = {"id": 105000035, "name": "Legend II"}
LEGEND_I = {"id": 105000036, "name": "Legend I"}


def _collector_url(connection_info: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(connection_info, options=f"{options} -c role=clashlens_collector")


def _rows(connection_info: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT normalized_tag, league_tier_id, trophies, checked_at"
            " FROM promotion_candidates ORDER BY normalized_tag"
        ).fetchall()


def test_lab_list_loads_once_keeps_newer_checks_and_skips_tracked_players(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    older = "2026-10-06T20:00:00+00:00"
    newer = "2026-10-06T21:00:00+00:00"
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO players (normalized_tag, active, eligibility_state)"
                " VALUES ('#2PP', true, 'eligible')"
            )
        url = _collector_url(connection_info)

        def load(text: str) -> int:
            monkeypatch.setattr("sys.stdin", io.StringIO(text))
            return promotion_candidates.run_command(url, "-")

        header = "tag,league_tier_id,trophies,checked_at\n"
        assert load(
            header
            + f"#2PP,105000035,5100,{newer}\n"  # tracked: left out
            + f"8QQ,105000035,4900,{older}\n"
            + f"#8QQ,105000035,4950,{newer}\n"  # same tag: newest kept
            + f"#9QQ,105000034,,{older}\n"
        ) == 0
        assert json.loads(capsys.readouterr().out) == {"added_or_updated": 2, "read": 3}
        assert _rows(connection_info) == [
            ("#8QQ", 105000035, 4950, datetime.fromisoformat(newer)),
            ("#9QQ", 105000034, None, datetime.fromisoformat(older)),
        ]

        # Loading an older check again changes nothing; a newer one replaces it.
        assert load(header + f"#8QQ,105000034,4000,{older}\n#9QQ,105000035,4800,{newer}\n") == 0
        assert json.loads(capsys.readouterr().out)["added_or_updated"] == 1
        assert [row[:3] for row in _rows(connection_info)] == [
            ("#8QQ", 105000035, 4950),
            ("#9QQ", 105000035, 4800),
        ]

        # One bad line refuses the whole file.
        assert load(header + f"#2QQ,105000035,4800,{newer}\n#0QQ,105000036,5000,{newer}\n") == 1
        assert "line 3" in capsys.readouterr().err
        assert [row[0] for row in _rows(connection_info)] == ["#8QQ", "#9QQ"]


def test_processed_profiles_add_and_remove_promotion_candidates(
    database_url: str, archive_server
) -> None:
    def profile(tier: dict, trophies: int) -> bytes:
        payload = json.loads(PROFILE.read_bytes())
        payload.update(leagueTier=tier, trophies=trophies)
        return json.dumps(payload).encode()

    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            def process(key: str, body: bytes, at: datetime) -> None:
                store_observation(
                    connection_info, archive_server, occurrence_key=key,
                    endpoint="profile", body=body, observed_at=at, normalized_tag="#2PP",
                )
                assert processor.process_once(owner=key) is not None

            process("legend-ii", profile(LEGEND_II, 4900), OBSERVED_AT)
            assert _rows(connection_info) == [("#2PP", 105000035, 4900, OBSERVED_AT)]

            promoted_at = OBSERVED_AT + timedelta(days=6)
            process("legend-i", profile(LEGEND_I, 5000), promoted_at)
            assert _rows(connection_info) == []

            # A late, older Legend II answer does not put a tracked player back.
            process("late-legend-ii", profile(LEGEND_II, 4950), OBSERVED_AT + timedelta(days=1))
            assert _rows(connection_info) == []
        finally:
            database.close()
