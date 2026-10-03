"""Whether a player's Reset trophies are known to have settled.

The game can apply the previous day's automatic defense loss minutes after
the 05:00 UTC Reset, so the profile read at the Reset is only provisional.
Each Reset starts as ``provisional``; a later check may record it as
``settled``, with the accepted trophies and their proof, or ``unresolved``.
Nothing reads this state yet, so day results are unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb


def record_provisional_boundary(
    connection: Any,
    *,
    player_id: int,
    boundary_at: datetime,
    sweep_id: int,
    early_baseline_id: int,
    early_state: str,
    reasons: list[str],
) -> None:
    """Record the Reset pair evidence of a still-provisional boundary.

    The pair's state and failure reasons are kept as they are: a complete
    pair proves the responses were processed, not that trophies settled. A
    repeat with the same evidence changes nothing, and a boundary already
    settled or unresolved keeps that verdict.
    """
    proof_json = {
        "early": {"baseline_id": early_baseline_id, "state": early_state},
    }
    connection.execute(
        """
        INSERT INTO reset_boundary_settlements (
            player_id, boundary_at, sweep_id, early_baseline_id,
            proof_json, reasons
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (player_id, boundary_at) DO UPDATE
        SET sweep_id = EXCLUDED.sweep_id,
            early_baseline_id = EXCLUDED.early_baseline_id,
            proof_json = EXCLUDED.proof_json,
            reasons = EXCLUDED.reasons,
            change_number = reset_boundary_settlements.change_number + 1,
            updated_at = clock_timestamp()
        WHERE reset_boundary_settlements.state = 'provisional'
          AND (
              reset_boundary_settlements.early_baseline_id,
              reset_boundary_settlements.proof_json,
              reset_boundary_settlements.reasons
          ) IS DISTINCT FROM (
              EXCLUDED.early_baseline_id,
              EXCLUDED.proof_json,
              EXCLUDED.reasons
          )
        """,
        (
            player_id,
            boundary_at,
            sweep_id,
            early_baseline_id,
            Jsonb(proof_json),
            Jsonb(reasons),
        ),
    )
