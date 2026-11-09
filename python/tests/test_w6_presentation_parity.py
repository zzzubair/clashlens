"""Shared behavioral data checked against W9's actual TypeScript functions.

The source is 967bdac78fa99c56cd49be733d309bb2798f245d. The numerical
cases in the three fact test files were evaluated by those original functions
without modifying website/. JSON serialization ignores JavaScript's -0.
The new zero-skip Goal choice is checked separately; W9 listed skips 1–3.
Compact profile membership and rival rates also pin the independent values
from user-profile-route.test.ts:335,384,401 and dashboard-cards.test.ts:984.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from clashlens import (
    api_dashboard_facts,
    api_player_presentation,
    api_player_season_facts,
)

CASES = json.loads((Path(__file__).parents[2] / "testdata/w6-presentation-parity.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["rule"])
def test_original_website_example(case):
    args, kwargs = case["args"], case["kwargs"]
    if case["rule"] == "display_end":
        args[3] = datetime.fromisoformat(args[3])
    elif case["rule"] in ("shape_of", "heatmap_day_numbers"):
        args[0] = datetime.fromisoformat(args[0])
        if case["rule"] == "shape_of":
            args[3] = {int(key): value for key, value in args[3].items()}
    elif case["rule"] == "battle_statistics":
        args[2], args[3] = datetime.fromisoformat(args[2]), datetime.fromisoformat(args[3])
    elif case["rule"] == "trophy_trends":
        args[1], args[2] = datetime.fromisoformat(args[1]), datetime.fromisoformat(args[2])
    module = next(module for module in (api_player_presentation, api_player_season_facts, api_dashboard_facts)
                  if hasattr(module, case["rule"]))
    result = getattr(module, case["rule"])(*args, **kwargs)
    if case["rule"] == "display_starts":
        result = [day["display_start"] for day in args[0]]
    assert result == case["expected"]
