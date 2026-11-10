"""Shared setup for dashboard bases postgres tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from reset_scenarios_postgres_support import (
    Player,
)


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
