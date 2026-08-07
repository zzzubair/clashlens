from __future__ import annotations

import hashlib

from .profile import normalize_player_tag

SNAPSHOT_ORDERING_RULE_VERSION = "tracked-player-order-v1"
FRESHNESS_RULE_VERSION = "profile-freshness-10m-v1"
PROFILE_FRESHNESS_SECONDS = 600
ANALYTICS_RULE_VERSION = "legend-analytics-v1"
CLASSIFICATION_VERSION = "army-classifier-unavailable-v1"
CLASSIFICATION_CONFIDENCE = "unclassified"


def deterministic_tag_hash(tag: str) -> str:
    normalized_tag = normalize_player_tag(tag)
    return hashlib.sha256(normalized_tag.encode("ascii")).hexdigest()
