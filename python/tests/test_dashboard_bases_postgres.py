"""End-to-end: Bases you attacked compares each base with Legends at similar
trophies. Saved game data runs through the real worker against PostgreSQL,
and the dashboard's today read serves each base with its comparison.

Held share falls and trophies lost rise as trophies rise, so a typical base
near 5,400 holds far less often than the all-Legends share. Compared with
every Legend it read Easy; compared with players near its trophies it reads
Average, by held share and by trophies lost a defense.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from domain_test_support import as_api_role, domain_database
from test_reset_scenarios_postgres import (
    SEASON,
    Battle,
    Player,
    Story,
    _tag,
    day,
    defenses,
)

from clashlens import api_dashboard
from clashlens.api_db import ApiDatabase

YOU, TYPICAL, STRONG = "#2PP", "#8PP", "#9PP"
HELD, TRIPLED = 16, 40
# The website's bands: website/app/lib/dashboard.ts BASE_STRENGTH_BAND (Held %)
# and BASE_TROPHY_BAND (Trophies, a defense).
BAND = 15
TROPHY_BAND = 1.25


@dataclass
class Facing(Player):
    """A player whose battles at the given times face the named players."""

    faced: dict[datetime, str] = field(default_factory=dict)

    def log(self, at: datetime) -> bytes:
        body = json.loads(super().log(at))
        for item in body["items"]:
            for when, tag in self.faced.items():
                if item["battleTimestamp"] == when.strftime("%Y%m%dT%H%M%S.000Z"):
                    item["opponentPlayerTag"] = tag
        return json.dumps(body).encode()


def _defenses(start: datetime, held: int) -> list[Battle]:
    """8 defenses, the first ``held`` held at 2 stars, the rest tripled."""
    return defenses(start, *[HELD] * held, *[TRIPLED] * (8 - held))


def _strength(row: dict[str, object]) -> str:
    """The website's Held % rating of a served base (baseStrength)."""
    defended, legends = row["defenses"], row["legends_held"]
    held = sum(defense["stars"] < 3 for defense in defended)
    points = 100 * held / len(defended) - 100 * legends["held"] / legends["defenses"]
    return "hard" if points > BAND else "easy" if points < -BAND else "average"


def _trophy_strength(row: dict[str, object]) -> str:
    """The website's Trophies rating of a served base (baseStrength)."""
    defended, legends = row["defenses"], row["legends_held"]
    lost = -sum(defense["trophy_change"] for defense in defended)
    fewer = legends["lost"] / legends["defenses"] * len(defended) - lost
    band = TROPHY_BAND * len(defended)
    return "hard" if fewer > band else "easy" if fewer < -band else "average"


def test_bases_compare_with_legends_at_similar_trophies(
    database_url: str, archive_server, monkeypatch
) -> None:
    """24 players near 4,900 hold 7 of 8 defenses and 24 near 5,400 hold 2
    of 8: 56.5% across all, 27.9% near 5,400. You attack two 5,400 bases: a
    typical one holding 3 of 8 (37.5%) and a strong one holding 7 of 8."""
    today = day(3)
    you_hit = today + timedelta(hours=7, minutes=30)
    players: dict[str, Player] = {
        YOU: Facing(YOU, [
            Battle(you_hit, TRIPLED, True),
            Battle(you_hit + timedelta(minutes=5), HELD, True),
        ], faced={you_hit: TYPICAL, you_hit + timedelta(minutes=5): STRONG}),
        # Your hits are their last defenses.
        TYPICAL: Facing(TYPICAL, _defenses(today, 3), faced={you_hit: YOU}),
        STRONG: Facing(
            STRONG,
            _defenses(today, 6)[:7] + [Battle(you_hit + timedelta(minutes=5), -HELD, False)],
            faced={you_hit + timedelta(minutes=5): YOU},
        ),
    }
    starts = {YOU: 5400, TYPICAL: 5400, STRONG: 5410}
    for n in range(24):
        low, high = f"#{_tag(1000 + n)}", f"#{_tag(2000 + n)}"
        players[low] = Player(low, _defenses(today, 7))
        players[high] = Player(high, _defenses(today, 2))
        starts[low], starts[high] = 4880 + 2 * n, 5380 + 2 * n
    ends = {
        tag: start + sum(battle.trophies for battle in players[tag].battles)
        for tag, start in starts.items()
    }

    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, players)
        story.reset(today, {tag: (start, SEASON) for tag, start in starts.items()})
        story.reads(today + timedelta(hours=9), ends)
        story.reset(day(4), {tag: (end, SEASON) for tag, end in ends.items()})
        unfinished = story.unfinished_jobs()
        api = ApiDatabase(as_api_role(connection_info))
        try:
            # The board is read at once, not in the background.
            board = api_dashboard.DashboardBoard(run=lambda read: read())
            read = api_dashboard.get_player_today(
                api, board, YOU, now=today + timedelta(hours=12)
            )
        finally:
            api.close()

    assert unfinished == []
    assert read is not None
    bases = {row["tag"]: row for row in read["opponents"]}
    assert {tag: row["reset_trophies"] for tag, row in bases.items()} == {
        TYPICAL: 5400, STRONG: 5410,
    }
    # Players within 100 trophies of each base: the 24 near 5,400 and the
    # two bases, 58 of 208 held, losing 6,928 (33.3 a defense); the 192
    # defenses near 4,900 are left out.
    near = {"held": 58, "defenses": 208, "lost": 24 * 272 + 248 + 152}
    assert {tag: row["legends_held"] for tag, row in bases.items()} == {
        TYPICAL: near, STRONG: near,
    }
    assert {tag: _strength(row) for tag, row in bases.items()} == {
        TYPICAL: "average", STRONG: "hard",
    }
    # By trophies: the typical base lost 248 (31 a defense) and the strong
    # one 152 (19), both more than 1.25 a defense under 33.3.
    assert {tag: _trophy_strength(row) for tag, row in bases.items()} == {
        TYPICAL: "hard", STRONG: "hard",
    }
    # A base losing what players near 5,400 usually do, 272 in 8 (34 a
    # defense), is Average here; against the old fixed 32 it read Easy.
    usual = [{"stars": 3, "trophy_change": -34}] * 8
    assert _trophy_strength({**bases[TYPICAL], "defenses": usual}) == "average"
    fixed = {"held": 0, "defenses": 1, "lost": 32}
    assert _trophy_strength({"defenses": usual, "legends_held": fixed}) == "easy"
    # Against every player's 56.5% the typical base was Easy.
    assert read["legends_held"] == {"held": 226, "defenses": 400}
    assert _strength({**bases[TYPICAL], "legends_held": read["legends_held"]}) == "easy"
