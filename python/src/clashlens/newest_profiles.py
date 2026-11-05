"""Each Reset member's newest profile at the Reset, for freezing its board.

A profile's sightings are its effects, or, for a legacy profile with none,
the profile itself. The newest is the latest sighting up to the Reset,
ordered by time and then id, exactly as one player's lookup orders them.

The lookup starts from the member's profile responses instead of their
profiles. An effect's time is its response's time, and its response is the
member's: ``ingestion.complete_profile`` saves the effect at the response's
``response_completed_at``, and a response whose profile names another player
fails as ``source_identity_mismatch`` before saving one. So the newest
sighting is at the time of the member's newest profile response up to the
Reset that saved one: migration 0049's index lists the member's responses
newest first, and the walk stops at the first such response. The sightings
of every response at that time, and the legacy profiles no older, then
compete as before.

The query this replaced visited every one of the member's accepted profiles
and each profile's newest sighting. On 4 November 2026 it ran past the
worker's 15-minute limit, took 871 seconds when tried again, and held the
Reset's lock throughout, so the Daily board published at 06:09:31 UTC.
"""

from __future__ import annotations

# Which of the member's profiles count: accepted ones, through migration
# 0098's index, or any source state, through the (tag, time) index.
ACCEPTED_VERSIONS = (
    "version.player_id = member.player_id"
    " AND version.source_contract_state = 'accepted'"
)
ANY_VERSIONS = """version.normalized_tag = (
            SELECT owner.normalized_tag FROM players AS owner
            WHERE owner.id = member.player_id)"""

# Columns: player, profile, observation, sighting time, profile JSON, tag,
# name, trophies, eligibility, current Season. Takes %(players)s and
# %(boundary)s; ``{versions}`` is one of the two conditions above.
NEWEST_PROFILES_SQL = """
    SELECT member.player_id, profile.id,
           COALESCE(effect.observation_id, profile.observation_id),
           newest.observed_at, profile.profile_json,
           profile.normalized_tag, profile.name, profile.trophies,
           profile.eligibility_state, profile.current_league_season_id
    FROM unnest(%(players)s::bigint[]) AS member (player_id)
    LEFT JOIN LATERAL (
        SELECT response.response_completed_at AS observed_at
        FROM collector_observations AS response
        WHERE response.player_id = member.player_id
          AND response.endpoint = 'profile'
          AND response.response_completed_at <= %(boundary)s
          AND EXISTS (
              SELECT FROM player_profile_effects AS effect
              JOIN player_profile_versions AS version
                ON version.id = effect.profile_version_id
              WHERE effect.observation_id = response.id AND {versions}
              -- Checked response by response: as a join, PostgreSQL read
              -- every one of the member's profiles and sightings instead.
              OFFSET 0)
        ORDER BY response.response_completed_at DESC, response.id DESC
        LIMIT 1
    ) AS seen ON true
    CROSS JOIN LATERAL (
        SELECT sighting.* FROM (
            SELECT effect.profile_version_id AS profile_id,
                   effect.id AS effect_id, effect.observed_at
            FROM collector_observations AS response
            JOIN player_profile_effects AS effect
              ON effect.observation_id = response.id
            JOIN player_profile_versions AS version
              ON version.id = effect.profile_version_id
            WHERE response.player_id = member.player_id
              AND response.endpoint = 'profile'
              AND response.response_completed_at = seen.observed_at
              AND effect.observed_at <= %(boundary)s
              AND {versions}
            UNION ALL
            SELECT version.id, NULL, version.observed_at
            FROM player_profile_versions AS version
            WHERE {versions}
              AND version.observed_at <= %(boundary)s
              AND version.observed_at >= COALESCE(seen.observed_at, '-infinity')
              AND NOT EXISTS (
                  SELECT FROM player_profile_effects AS effect
                  WHERE effect.profile_version_id = version.id)
        ) AS sighting
        ORDER BY sighting.observed_at DESC,
                 COALESCE(sighting.effect_id, sighting.profile_id) DESC
        LIMIT 1
    ) AS newest
    JOIN player_profile_versions AS profile ON profile.id = newest.profile_id
    LEFT JOIN player_profile_effects AS effect ON effect.id = newest.effect_id
    """
