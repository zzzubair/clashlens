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

The walk reads each response's sighting and that sighting's profile, about
ten pages a response. For about one member in ten it is long: their newest
responses before the Reset are a profile that is not accepted (a promoted
player still naming Season 0, a lower league) or "player not found". On the
16 November 2026 board, 55 of a sample of 500 members walked 3,739 responses
and the other 445 none past their first, so the 10 percent cost about 85
percent of the reads. The accepted lookup therefore walks only the newest
``NEAR`` responses. Past them, a member with at most ``FEW`` accepted
profiles takes the newest sighting of each, one index entry a profile, by
the same rule as the walk: an effect's time is its response's. Others walk
on. For the whole first boards of 13, 15 and 16 November 2026 that read 41
percent fewer pages (765,312 against 1,294,210 on 13 November), and 2.5
percent more on 17 November, when almost every member's newest responses
were not accepted. The two lookups gave the same profile for all 90,789
members of the 11 to 17 November first boards.
"""

from __future__ import annotations

# Which of the member's profiles count: accepted ones, through migration
# 0098's index, or any source state, through the (tag, time) index.
_ACCEPTED_VERSIONS = (
    "version.player_id = member.player_id"
    " AND version.source_contract_state = 'accepted'"
)
_ANY_VERSIONS = """version.normalized_tag = (
            SELECT owner.normalized_tag FROM players AS owner
            WHERE owner.id = member.player_id)"""
# The responses the accepted lookup walks first, and the most accepted
# profiles it looks up one by one past them.
NEAR = 4
FEW = 100

# The member's profile responses up to the Reset.
_RESPONSES = """collector_observations AS response
        WHERE response.player_id = member.player_id
          AND response.endpoint = 'profile'
          AND response.response_completed_at <= %(boundary)s"""
# The member's ``NEAR`` newest of them.
_NEAR_RESPONSES = f"""(
            SELECT response.id, response.response_completed_at
            FROM {_RESPONSES}
            ORDER BY response.response_completed_at DESC, response.id DESC
            LIMIT {NEAR}) AS response
        WHERE true"""
# The newest of ``{responses}`` that saved a sighting of a counted profile.
_WALK = """
        SELECT response.response_completed_at AS observed_at
        FROM {responses}
          AND EXISTS (
              SELECT FROM player_profile_effects AS effect
              JOIN player_profile_versions AS version
                ON version.id = effect.profile_version_id
              WHERE effect.observation_id = response.id AND {versions}
              -- Checked response by response: as a join, PostgreSQL read
              -- every one of the member's profiles and sightings instead.
              OFFSET 0)
        ORDER BY response.response_completed_at DESC, response.id DESC
        LIMIT 1"""
_ANY_SEEN = f"""
    LEFT JOIN LATERAL ({_WALK.format(responses=_RESPONSES, versions="{versions}")}
    ) AS seen ON true"""
# ``by_profile``: whether a member none of whose newest responses saved an
# accepted sighting has few enough accepted profiles to look each one up;
# null when one did.
_ACCEPTED_SEEN = f"""
    LEFT JOIN LATERAL ({_WALK.format(responses=_NEAR_RESPONSES, versions="{versions}")}
    ) AS near ON true
    CROSS JOIN LATERAL (
        SELECT CASE WHEN near.observed_at IS NULL THEN (
            SELECT count(*) <= {FEW} FROM (
                SELECT FROM player_profile_versions AS version
                WHERE {{versions}} LIMIT {FEW + 1}) AS few) END AS by_profile
        -- Evaluated once, not once for each lookup below.
        OFFSET 0
    ) AS route
    LEFT JOIN LATERAL (
        SELECT max(sighted.observed_at) AS observed_at
        FROM player_profile_versions AS version
        CROSS JOIN LATERAL (
            SELECT effect.observed_at FROM player_profile_effects AS effect
            WHERE effect.profile_version_id = version.id
              AND effect.observed_at <= %(boundary)s
            ORDER BY effect.observed_at DESC LIMIT 1
        ) AS sighted
        WHERE route.by_profile AND {{versions}}
    ) AS looked_up ON true
    LEFT JOIN LATERAL ({_WALK.format(
        responses=_RESPONSES + "\n          AND NOT route.by_profile", versions="{versions}")}
    ) AS walked ON true
    CROSS JOIN LATERAL (
        SELECT COALESCE(near.observed_at, looked_up.observed_at, walked.observed_at)
            AS observed_at
    ) AS seen"""

# Columns: player, profile, observation, sighting time, profile JSON, tag,
# name, trophies, eligibility, current Season. Takes %(players)s and
# %(boundary)s.
_NEWEST_PROFILES_SQL = """
    SELECT member.player_id, profile.id,
           COALESCE(effect.observation_id, profile.observation_id),
           newest.observed_at, profile.profile_json,
           profile.normalized_tag, profile.name, profile.trophies,
           profile.eligibility_state, profile.current_league_season_id
    FROM unnest(%(players)s::bigint[]) AS member (player_id){seen}
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

# The newest accepted profile, and the newest of any source state.
ACCEPTED_PROFILES_SQL = _NEWEST_PROFILES_SQL.replace("{seen}", _ACCEPTED_SEEN).replace(
    "{versions}", _ACCEPTED_VERSIONS)
ANY_PROFILES_SQL = _NEWEST_PROFILES_SQL.replace("{seen}", _ANY_SEEN).replace(
    "{versions}", _ANY_VERSIONS)
