from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import (
    apply_migration,
    as_api_role,
    domain_database,
    store_observation,
    text,
)
from test_army_analytics_publication_postgres import (
    DAY_START,
    _army_job,
    _event,
    _processor,
    _publish_day,
    _row,
    _seed_frozen_snapshot_at,
    _selection,
)

from clashlens import api_analytics, army_rank_bands, army_types
from clashlens.api_db import ApiDatabase
from clashlens.army_analytics import ArmyTypesPreparing

# A Duke with Revenge Deck and 2 Ice Blocks; a Duke with Rocket Backpack and
# an Earthquake.
THROWER_CODE = "h7p4e60_52u9x132-4x7s2x109"
OTHER_THROWER_CODE = "h7p4e53_52u10x132-2x7s1x10"
# 8 Dragons (160) and 6 Dragon Riders (150): Hydra, though neither leads clearly.
HYDRA_CODE = "u8x8-6x65"
NOON = datetime(2026, 8, 5, 12, tzinfo=UTC)
ATTACK_TROPHIES_MIGRATION = (
    Path(__file__).parents[2] / "deploy" / "migrations" / "0114_army_attack_trophies.sql"
)


def _result(api: ApiDatabase, population: str = "top-10", sort: str = "usage-rate") -> dict:
    result = api_analytics.get_army_analytics(
        api, _selection(category="army-types", population=population, sort=sort)
    )
    assert result is not None
    return result


def _types(api: ApiDatabase, population: str = "top-10") -> dict[str, tuple]:
    return {
        row["label"]: (row["usage_count"], row["usage_denominator"],
                       row["star_counts"], row["average_destruction"],
                       row["average_trophies"], row["average_miss_trophies"])
        for row in _result(api, population)["rows"]
    }


def _variants(api: ApiDatabase) -> dict[str, list[tuple]]:
    return {
        row["label"]: [(variant["label"], variant["usage_count"],
                        variant["usage_denominator"], variant["usually_with"])
                       for variant in row["variants"]]
        for row in _result(api)["rows"] if "variants" in row
    }


def test_army_types_from_saved_battles_to_the_armies_page(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as ci:
        ts1, ts2 = DAY_START + timedelta(hours=1), DAY_START + timedelta(hours=3)
        reports = (
            ("t1", "#2PP", _row(True, "#8PP", THROWER_CODE, ts1, 3, 100), ts1),
            ("t2", "#2PP", _row(True, "#9PP", HYDRA_CODE, ts2, 1, 50), ts2),
            ("t3", "#8PP", _row(True, "#2PP", OTHER_THROWER_CODE, ts2, 2, 80), ts2),
        )
        jobs = [
            store_observation(
                ci, archive_server, occurrence_key=key, endpoint="battle_log",
                body=json.dumps({"items": [row]}).encode(),
                observed_at=ts + timedelta(minutes=1), normalized_tag=tag,
            )[1]
            for key, tag, row, ts in reports
        ]
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(as_api_role(ci), army_cache_capacity=0)
        fact_reads: list[str] = []

        def trace(pool):
            """Record each query of ``pool`` that reads attacks."""
            original = pool.connection

            @contextmanager
            def traced(*args, **kwargs):
                with original(*args, **kwargs) as connection:
                    class Traced:
                        def execute(self, query, *args, **kwargs):
                            if "army_analytics_battle_facts" in str(query):
                                fact_reads.append(str(query))
                            return connection.execute(query, *args, **kwargs)

                        def __getattr__(self, name):
                            return getattr(connection, name)

                    yield Traced()

            pool.connection = traced
            return original

        try:
            for index, job in enumerate(jobs):
                assert processor.process_job(job, owner=f"ingest-{index}").outcome in (
                    "processed", "processed_with_gaps",
                )
            with database.pool.connection() as connection:
                battles = {
                    (text(a), text(d)): int(b)
                    for a, d, b in connection.execute(
                        """
                        SELECT atk.normalized_tag, def.normalized_tag, b.id
                        FROM legend_battles b
                        JOIN players atk ON atk.id = b.attacker_player_id
                        JOIN players def ON def.id = b.defender_player_id
                        """
                    ).fetchall()
                }
                ids = {
                    text(tag): int(player_id)
                    for tag, player_id in connection.execute(
                        "SELECT normalized_tag, id FROM players"
                    ).fetchall()
                }
                # Each distinct army got its type as its attack arrived.
                assert connection.execute(
                    "SELECT count(*) FROM army_package_types"
                ).fetchone()[0] == 3
            _publish_day(database, "#2PP", [
                _event(battles[("#2PP", "#8PP")], "offense", ts1, 3, 100, 40),
                _event(battles[("#2PP", "#9PP")], "offense", ts2, 1, 50, 10),
            ])
            _publish_day(database, "#8PP", [
                _event(battles[("#8PP", "#2PP")], "offense", ts2, 2, 80, 25),
            ])
            assert processor.process_job(_army_job(database), owner="army").outcome == "processed"
            _seed_frozen_snapshot_at(
                database, DAY_START + timedelta(days=1),
                [(ids[tag], position, "fresh", "confirmed")
                 for position, tag in enumerate(("#2PP", "#8PP", "#9PP"), start=1)],
            )
            army_rank_bands.refresh_rank_band_totals(database)
            # Thrower won 40 for its 3-star and 26 for its 2-star at 80%,
            # Hydra 10 for its 1-star at 50%, by the game's trophy table.
            expected = {
                "Thrower": (2, 3, [0, 0, 1, 1], 90.0, 33.0, 26.0),
                "Hydra": (1, 3, [0, 1, 0, 0], 50.0, 10.0, 10.0),
            }
            # Each type opens into the variants its attacks used, named by
            # hero equipment, with the spells each usually carries.
            variants = {
                "Thrower": [("Revenge Thrower", 1, 2, ["Ice Block"]),
                            ("Backpack Thrower", 1, 2, ["Earthquake"])],
                "Hydra": [("Other Hydra", 1, 1, [])],
            }

            # Nothing is shown until every saved army has its type under
            # these rules, and nothing is counted during the Reset window.
            with pytest.raises(ArmyTypesPreparing):
                _types(api)
            army_types.refresh_army_types(database, now=NOON.replace(hour=5))
            with pytest.raises(ArmyTypesPreparing):
                _types(api)

            # Labelled: the page adds up the day's attacks until it is counted.
            army_types.refresh_army_types(database, now=NOON)
            assert _types(api) == expected
            assert _types(api, "trophies-5000-9000") == expected
            assert _variants(api) == variants

            # Counted: the same numbers, from the saved daily counts.
            army_types.refresh_army_types(database, now=NOON)
            untraced = trace(api.pool)
            assert _types(api) == expected
            assert _variants(api) == variants
            assert fact_reads == []
            api.pool.connection = untraced
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT sum(attacks), sum(trophies) FROM army_package_day_counts"
                ).fetchone() == (3, 76)
            assert [row["label"] for row in _result(api, sort="average-trophies")["rows"]] == [
                "Thrower", "Hydra",
            ]

            # Days counted before migration 0114 saved no trophies. The
            # migration forgets they were counted, so the page adds them up
            # from their attacks until the worker counts them again.
            with psycopg.connect(ci, autocommit=True) as connection:
                connection.execute("UPDATE army_package_day_counts SET trophies = NULL")
                apply_migration(connection, ATTACK_TROPHIES_MIGRATION.read_text())
            assert _types(api) == expected
            army_types.refresh_army_types(database, now=NOON)
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT sum(attacks), sum(trophies) FROM army_package_day_counts"
                ).fetchone() == (3, 76)

            # A rename changes the name only.
            monkeypatch.setitem(army_types.TYPE_NAMES, army_types.THROWER, "Throwers")
            assert _types(api) == {
                ("Throwers" if name == "Thrower" else name): row
                for name, row in expected.items()
            }
            monkeypatch.setitem(army_types.TYPE_NAMES, army_types.THROWER, "Thrower")

            # An older partly read army, saved with its Revenge Deck unnamed
            # and no distinct army, is typed from its saved home troops and
            # takes its variant and spells from its saved heroes and spells,
            # counted or not.
            thrower_battle = battles[("#2PP", "#8PP")]
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE battle_army_decodes
                    SET status = 'partial', exact_army_id = NULL, identity_hash = NULL,
                        heroes = jsonb_set(heroes, '{0,equipment}', '["equipment:52"]'),
                        unresolved_components = '[{"numeric_id": 60, "quantity": 1,
                            "section": "h", "origin": "hero:7:equipment"}]'
                    WHERE id = (SELECT decode_id FROM army_analytics_battle_facts
                                WHERE is_current AND lens = 'offense' AND battle_id = %s)
                    """,
                    (thrower_battle,),
                )
                connection.execute(
                    "UPDATE army_analytics_battle_facts SET army_state = 'partial'"
                    " WHERE is_current AND lens = 'offense' AND battle_id = %s",
                    (thrower_battle,),
                )
                connection.execute("DELETE FROM army_type_counted_days")
            assert _types(api) == expected
            assert _types(api, "trophies-5000-9000") == expected
            assert _variants(api) == variants
            army_types.refresh_army_types(database, now=NOON)
            assert _types(api) == expected
            assert _variants(api) == variants
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM army_package_day_counts WHERE exact_army_id < 0"
                ).fetchone()[0] == 1

            # A rule change relabels the distinct armies only: no attack is
            # read again and the daily counts stay as they were.
            with database.pool.connection() as connection:
                counts = connection.execute(
                    "SELECT * FROM army_package_day_counts ORDER BY 1, 2, 3"
                ).fetchall()
            rules, combinations = army_types.RULES_VERSION, army_types.COMBINATIONS
            listed = army_types.VARIANTS
            monkeypatch.setattr(army_types, "RULES_VERSION", "army-types-test")
            monkeypatch.setattr(army_types, "COMBINATIONS", ())
            # A variant dropped from the list: past days read it as Other.
            monkeypatch.setattr(army_types, "VARIANTS", {
                army_types.THROWER: listed[army_types.THROWER][:3],
            })
            with pytest.raises(ArmyTypesPreparing):
                _types(api)
            untraced = trace(database.pool)
            army_types.refresh_army_types(database, now=NOON)
            assert fact_reads == []
            database.pool.connection = untraced
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT * FROM army_package_day_counts ORDER BY 1, 2, 3"
                ).fetchall() == counts
                assert {
                    (text(version), count) for version, count in connection.execute(
                        "SELECT rules_version, count(*) FROM army_package_types GROUP BY 1"
                    ).fetchall()
                } == {("army-types-test", 3)}
            assert _types(api) == {
                "Thrower": expected["Thrower"],
                "Other": expected["Hydra"],
            }
            assert _variants(api) == {"Thrower": [("Revenge Thrower", 1, 2, ["Ice Block"]),
                                                  ("Other Thrower", 1, 2, ["Earthquake"])]}

            # Rolling back relabels them under the earlier rules again.
            monkeypatch.setattr(army_types, "RULES_VERSION", rules)
            monkeypatch.setattr(army_types, "COMBINATIONS", combinations)
            monkeypatch.setattr(army_types, "VARIANTS", listed)
            with pytest.raises(ArmyTypesPreparing):
                _types(api)
            army_types.refresh_army_types(database, now=NOON)
            assert _types(api) == expected
            assert _variants(api) == variants

            # A corrected day changes the evidence hash, even with the same
            # numbers.
            evidence = _result(api)["reproducibility"]["source_evidence_hash"]
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE army_analytics_completed_days SET fact_input_hash = %s",
                    ("a" * 64,),
                )
            assert _types(api) == expected
            assert _result(api)["reproducibility"]["source_evidence_hash"] != evidence
        finally:
            api.close()
            database.close()
