"""Shared setup for frozen board locator postgres tests."""

from __future__ import annotations


def _rows_read(plan: dict, relation: str) -> int:
    read = 0
    if plan.get("Relation Name") == relation:
        per_loop = plan["Actual Rows"] + plan.get("Rows Removed by Filter", 0)
        read += round(per_loop * plan["Actual Loops"])
    for child in plan.get("Plans", []):
        read += _rows_read(child, relation)
    return read
