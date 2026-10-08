"""Count who is tracked and who still needs an eligibility answer.

The ``population-status`` command, run with the collector database role,
prints one JSON object (migration 0082):

* ``tracked``: tracked players split into ``available`` (a current profile
  naming this Season), ``waiting_to_sign_up`` (a Legend I profile not naming
  this Season, such as Season ID 0) and ``unavailable`` (the profile is no
  longer found), and how many still wait for their first battle log;
* ``first_battle_log_delay``: players whose first check was added in the last
  7 days and who are now tracked, how many have a first battle log, and the
  median, 95th-percentile and longest seconds from that check to the log;
* ``untracked``: known players not tracked, by eligibility;
* ``untracked_this_week``: for untracked battle opponents and for other known
  players (imports, rankings, lookups, the promotion list) separately, how
  many have this week's answer, a check waiting, a retry due, or no check;
* ``old_classifications_remaining``: untracked players with a saved profile
  but no recognized league, queued or not, until a recognized one is saved;
* ``checks_this_week``: this week's weekly and discovery checks by outcome,
  with how many of their players are now tracked;
* ``eligibility_due``: players saved as due an eligibility check;
* ``promotion_list``: listed Legend II and III players, how many were asked
  since the Monday Reset, and untracked players whose latest recognized
  profile shows Legend II or III but who are not listed;
* ``repair_candidates``: untracked players never answered and not due.

``--repair`` first saves every repair candidate as due, so the collector
checks them again, and lists every unlisted Legend II or III player. It
refetches nothing itself and deletes nothing; its counts print under
``repair``.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .domain import ranked_day_for


def add_command(
    subparsers: Any, database_argument: Callable[[argparse.ArgumentParser], None]
) -> None:
    """Add the ``population-status`` command to the CLI."""
    command = subparsers.add_parser(
        "population-status",
        help="count tracked, waiting and unavailable players and pending eligibility checks (collector database role)",
    )
    database_argument(command)
    command.add_argument(
        "--repair",
        action="store_true",
        help="first save never-answered players as due a check and list known Legend II and III players",
    )


def run_command(database_url: str, *, repair: bool) -> int:
    import psycopg

    now = datetime.now(UTC)
    report: dict[str, Any] = {}
    with psycopg.connect(database_url) as connection:
        if repair:
            report["repair"] = connection.execute(
                "SELECT clashlens_repair_population(%s)", (now,)
            ).fetchone()[0]
            connection.commit()
        connection.read_only = True
        report.update(
            connection.execute(
                "SELECT clashlens_population_report(%s, %s)",
                (now, ranked_day_for(now).official_season_id),
            ).fetchone()[0]
        )
    print(json.dumps(report, sort_keys=True))
    return 0
