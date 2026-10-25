from __future__ import annotations

import json
from datetime import timedelta

from domain_test_support import as_api_role, domain_database, store_observation, text
from test_army_analytics_publication_postgres import (
    DAY_NUMBER,
    DAY_START,
    _army_job,
    _event,
    _processor,
    _publish_day,
    _row,
    _seed_frozen_snapshot_at,
    _selection,
)
from test_army_types_postgres import NOON

from clashlens import api_analytics, army_rank_bands, army_types
from clashlens.api_db import ApiDatabase

# Revenge Thrower armies: 9 Throwers (144 camp space) and 4 Healers (56), a
# Duke with Revenge Deck. The first comes with a Wall Wrecker, which saved
# armies leave out, so both codes are one distinct army.
REVENGE = "h7p4e60_52u9x132-4x7s2x109"
REVENGE_SIEGE = "h7p4e60_52u9x132-4x7-1x51s2x109"
# 2 and 3 Archers are fillers, under 5% of the camp space: the same group,
# as other distinct armies.
REVENGE_ARCHERS = ("h7p4e60_52u9x132-4x7-2x1s2x109", "h7p4e60_52u9x132-4x7-3x1s2x109")
# An Earthquake instead of Ice Blocks: another group.
REVENGE_QUAKE = "h7p4e60_52u9x132-4x7s2x10"
# Rocket Backpack: Backpack Thrower, another variant.
BACKPACK = "h7p4e53_52u10x132-2x7s1x10"


def test_variant_examples_from_saved_battles_to_the_armies_page(
    database_url: str, archive_server, monkeypatch
) -> None:
    codes = [
        REVENGE_SIEGE, REVENGE_SIEGE, REVENGE, REVENGE, REVENGE_SIEGE,
        *[REVENGE_ARCHERS[0]] * 4, REVENGE_ARCHERS[1], REVENGE_QUAKE, BACKPACK,
    ]
    opponents = ["#8PP", "#9PP", "#PPP", "#YPP", "#LPP", "#QPP", "#GPP", "#RPP", "#JPP",
                 "#CPP", "#UPP", "#VPP"]
    times = [DAY_START + timedelta(hours=hour) for hour in range(1, len(codes) + 1)]
    with domain_database(database_url) as ci:
        job = store_observation(
            ci, archive_server, occurrence_key="log", endpoint="battle_log",
            body=json.dumps({"items": [
                _row(True, opponent, code, ts, 2, 80)
                for opponent, code, ts in zip(opponents, codes, times, strict=True)
            ]}).encode(),
            observed_at=times[-1] + timedelta(minutes=1), normalized_tag="#2PP",
        )[1]
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(as_api_role(ci), army_cache_capacity=0)
        try:
            assert processor.process_job(job, owner="ingest").outcome in (
                "processed", "processed_with_gaps",
            )
            with database.pool.connection() as connection:
                battles = {
                    text(tag): int(battle)
                    for tag, battle in connection.execute(
                        """
                        SELECT def.normalized_tag, b.id FROM legend_battles b
                        JOIN players def ON def.id = b.defender_player_id
                        """
                    ).fetchall()
                }
                attacker = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            _publish_day(database, "#2PP", [
                _event(battles[opponent], "offense", ts, 2, 80, 26)
                for opponent, ts in zip(opponents, times, strict=True)
            ])
            assert processor.process_job(_army_job(database), owner="army").outcome == "processed"
            with database.pool.connection() as connection:
                # The Wall Wrecker-less battles seen from their defenders'
                # side too, and older saved versions of them: still 2 battles
                # against the Wall Wrecker code's 3.
                connection.execute(
                    """
                    INSERT INTO battle_army_decodes (
                        battle_id, evidence_id, perspective, raw_code, decoder_version,
                        catalog_version, catalog_hash, status, exact_army_id,
                        identity_hash, is_active)
                    SELECT battle_id, evidence_id, side.perspective, raw_code,
                           decoder_version, catalog_version, catalog_hash, status,
                           exact_army_id, identity_hash, side.active
                    FROM battle_army_decodes
                    CROSS JOIN (VALUES ('defender', true), ('attacker', false))
                        AS side (perspective, active)
                    WHERE raw_code = %s
                    """,
                    (REVENGE,),
                )
            _seed_frozen_snapshot_at(
                database, DAY_START + timedelta(days=1), [(attacker, 1, "fresh", "confirmed")]
            )
            army_rank_bands.refresh_rank_band_totals(database)

            def examples(population: str) -> dict:
                result = api_analytics.get_army_analytics(
                    api, _selection(category="army-types", population=population),
                    examples=(army_types.THROWER, 3),
                )
                assert result is not None
                return result

            def variant_attacks(population: str) -> int:
                result = api_analytics.get_army_analytics(
                    api, _selection(category="army-types", population=population)
                )
                assert result is not None
                (thrower,) = [row for row in result["rows"] if row["label"] == "Thrower"]
                return next(row["usage_count"] for row in thrower["variants"]
                            if row["label"] == "Revenge Thrower")

            # The 3 Ice Block armies are one group of 10 attacks, its example
            # the army used 5 times, copied with the Wall Wrecker 3 of them
            # took. The Earthquake army is the second group.
            expected = [
                {"attacks": 10, "armies": 3, "example_attacks": 5,
                 "army_share_code": REVENGE_SIEGE},
                {"attacks": 1, "armies": 1, "example_attacks": 1,
                 "army_share_code": REVENGE_QUAKE},
            ]
            # Labelled, then counted: the same examples, from the day's
            # attacks and then from its saved counts, for a Top N and a
            # trophy range, and they add up to the variant's row.
            army_types.refresh_army_types(database, now=NOON)
            for _counted in (False, True):
                for population in ("top-10", "trophies-5000-9000"):
                    result = examples(population)
                    assert result["groups"] == expected
                    assert sum(group["attacks"] for group in result["groups"]) == (
                        variant_attacks(population)
                    )
                army_types.refresh_army_types(database, now=NOON)
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT sum(attacks) FROM army_package_day_counts"
                ).fetchone()[0] == len(codes)

            assert result["covered_days"] == [DAY_NUMBER]

            # Older partly read armies, saved with their Revenge Deck unnamed
            # and no distinct army, count with the same army saved the newer
            # way, here with its Ice Blocks split between home and Clan
            # Castle: 2 older Wall Wrecker attacks and 3 newer attacks, 2
            # without it, are one army of 5 that beats the Archers army's 4,
            # copied with the code most of all 5 took. The Earthquake army
            # alone still makes its group.
            older = [battles[opponent] for opponent in ("#8PP", "#9PP", "#UPP")]
            with database.pool.connection() as connection:
                decodes = """(SELECT decode_id FROM army_analytics_battle_facts
                              WHERE is_current AND lens = 'offense'
                                AND battle_id = ANY(%s::bigint[]))"""
                connection.execute(
                    f"""
                    UPDATE battle_army_decodes
                    SET status = 'partial', exact_army_id = NULL, identity_hash = NULL,
                        heroes = jsonb_set(heroes, '{{0,equipment}}', '["equipment:52"]'),
                        unresolved_components = '[{{"numeric_id": 60, "quantity": 1,
                            "section": "h", "origin": "hero:7:equipment"}}]'
                    WHERE id IN {decodes}
                    """,
                    (older,),
                )
                connection.execute(
                    f"""
                    UPDATE battle_army_decodes
                    SET spells = '[["spell:109", 1, "home"], ["spell:109", 1, "clan_castle"]]'
                    WHERE id IN {decodes}
                    """,
                    (older[:2],),
                )
                connection.execute(
                    """
                    UPDATE army_analytics_battle_facts SET army_state = 'partial'
                    WHERE is_current AND lens = 'offense' AND battle_id = ANY(%s::bigint[])
                    """,
                    (older,),
                )
                connection.execute("DELETE FROM army_type_counted_days")
            for _counted in (False, True):
                for population in ("top-10", "trophies-5000-9000"):
                    assert examples(population)["groups"] == expected
                army_types.refresh_army_types(database, now=NOON)

            assert result["rule"] == "With Revenge Deck, without Fireball or Rocket Backpack."
            assert result["army_units"]["troop:51"] == {"name": "Wall Wrecker", "siege": True}
            other = api_analytics.get_army_analytics(
                api, _selection(category="army-types", population="top-10"),
                examples=(army_types.HYDRA, army_types.OTHER_VARIANT),
            )
            assert other is not None and other["groups"] == []
            assert other["rule"] == "Without Revenge Deck or Rocket Backpack."
        finally:
            api.close()
            database.close()
