"""Shared setup for reset acceptance postgres tests."""

from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict, make_conninfo


def _as_worker(connection_info: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(
        connection_info, options=f"{options} -c role=clashlens_python_worker".strip()
    )
