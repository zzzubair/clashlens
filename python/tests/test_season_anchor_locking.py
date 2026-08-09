from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from clashlens.db import Database


class _Result:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, params: Any = None) -> _Result:
        del params
        self.statements.append(statement)
        if "FROM legend_season_anchors AS a" in statement:
            return _Result(
                (
                    1,
                    "1783918800",
                    "1781499600",
                    datetime(2026, 7, 13, 5, tzinfo=UTC),
                    datetime(2026, 8, 9, tzinfo=UTC),
                )
            )
        return _Result()


def test_same_season_anchor_common_path_does_not_lock_confirmed_row() -> None:
    connection = _Connection()
    profile: Any = SimpleNamespace(
        eligibility_state="eligible",
        current_league_season_id="1783918800",
        previous_league_season_id="1781499600",
        observed_at=datetime(2026, 8, 9, 1, tzinfo=UTC),
    )

    outcome = Database._record_season_anchor(connection, 42, profile)

    anchor_reads = [
        statement
        for statement in connection.statements
        if "FROM legend_season_anchors AS a" in statement
    ]
    assert outcome == "accepted"
    assert len(anchor_reads) == 1
    assert "FOR UPDATE" not in anchor_reads[0]
    assert any(
        "INSERT INTO season_anchor_evidence" in sql for sql in connection.statements
    )
