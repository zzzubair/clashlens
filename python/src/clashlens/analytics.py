from __future__ import annotations

import hashlib

from .profile import normalize_player_tag

# v2 ranks each player by the Reset proof their day shares with the Season
# summary (reset_settlement.DayEnd); v1 by their last reading alone.
SNAPSHOT_ORDERING_RULE_VERSION = "tracked-player-order-v2"
FRESHNESS_RULE_VERSION = "profile-freshness-10m-v1"
PROFILE_FRESHNESS_SECONDS = 600
ANALYTICS_RULE_VERSION = "legend-analytics-v1"
CLASSIFICATION_VERSION = "army-classifier-unavailable-v1"
CLASSIFICATION_CONFIDENCE = "unclassified"


def deterministic_tag_hash(tag: str) -> str:
    normalized_tag = normalize_player_tag(tag)
    return hashlib.sha256(normalized_tag.encode("ascii")).hexdigest()
