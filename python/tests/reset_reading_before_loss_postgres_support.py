"""Shared setup for reset reading before loss postgres tests."""

from __future__ import annotations

from datetime import timedelta

from reconciliation_postgres_support import DAY_START
from reset_settlement_state_postgres_support import (
    BOUNDARIES,
)

# Three ordinary days: Monday 3 August to Thursday 6 August 2026.
DAY_A, DAY_B, DAY_C = BOUNDARIES["monday"], DAY_START, BOUNDARIES["ordinary"]


DAY_D = DAY_C + timedelta(days=1)
