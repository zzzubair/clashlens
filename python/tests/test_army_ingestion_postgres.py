from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from domain_test_support import domain_database, store_observation, text

from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor


def _processor(connection_info: str, archive_server):
    database = Database(connection_info)
    processor = ObservationProcessor(
        database,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )
    return database, processor


def _live_row(attack: bool, tag: str, code: str | None, ts: datetime):
    row = {
        "battleType": "legend",
        "attack": attack,
        "battleTimestamp": ts.isoformat().replace("+00:00", "Z"),
        "stars": 3,
        "destructionPercentage": 100,
        "opponentPlayerTag": tag,
        "opponentName": "Opp",
        "opponentTownHallLevel": 17,
    }
    if code is not None:
        row["armyShareCode"] = code
    return row


def test_missing_army_code_remains_canonical_no_army_facts(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        observed_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
        # missing code row (no armyShareCode key)
        body_missing = json.dumps(
            {"items": [_live_row(True, "#8PP", None, observed_at)]}
        ).encode()
        # we need to remove the key: _live_row with code None still adds no key, good
        _, job_missing = store_observation(
            ci,
            archive_server,
            occurrence_key="missing-code",
            endpoint="battle_log",
            body=body_missing,
            observed_at=observed_at,
            normalized_tag="#2PP",
        )
        # empty code row
        body_empty = json.dumps(
            {"items": [_live_row(True, "#9PP", "", observed_at + timedelta(minutes=1))]}
        ).encode()
        _, job_empty = store_observation(
            ci,
            archive_server,
            occurrence_key="empty-code",
            endpoint="battle_log",
            body=body_empty,
            observed_at=observed_at + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        try:
            assert processor.process_job(job_missing, owner="t1").outcome in (
                "processed",
                "processed_with_gaps",
            )
            assert processor.process_job(job_empty, owner="t2").outcome in (
                "processed",
                "processed_with_gaps",
            )
            with database.pool.connection() as conn:
                battles = conn.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0]
                evidences = conn.execute(
                    "SELECT count(*) FROM battle_evidence"
                ).fetchone()[0]
                decodes = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE status='decoded'"
                ).fetchone()[0]
                failures = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE status='failed'"
                ).fetchone()[0]
                # Two canonical battles should exist despite missing codes
                assert battles == 2
                assert evidences == 2
                # No decoded facts, only failures (missing/empty)
                assert decodes == 0
                assert failures == 2
                cats = [
                    text(r[0])
                    for r in conn.execute(
                        "SELECT failure_category FROM battle_army_decodes ORDER BY id"
                    ).fetchall()
                ]
                assert set(cats) == {
                    "missing_army_share_code",
                    "empty_army_share_code",
                }
        finally:
            database.close()


def test_fixture_decodes_and_permutations_share_exact_army(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        base = "h0p9e14_32d1x53u2x58-1x97s2x2"
        perm = "s2x2u2x58-1x97h0p9e14_32d1x53"
        ts1 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        ts2 = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
        body1 = json.dumps({"items": [_live_row(True, "#8PP", base, ts1)]}).encode()
        body2 = json.dumps({"items": [_live_row(True, "#9PP", perm, ts2)]}).encode()
        _, j1 = store_observation(
            ci,
            archive_server,
            occurrence_key="perm1",
            endpoint="battle_log",
            body=body1,
            observed_at=ts1 + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        _, j2 = store_observation(
            ci,
            archive_server,
            occurrence_key="perm2",
            endpoint="battle_log",
            body=body2,
            observed_at=ts2 + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j1, owner="p1")
            proc.process_job(j2, owner="p2")
            with db.pool.connection() as conn:
                exact_cnt = conn.execute(
                    "SELECT count(*) FROM exact_armies"
                ).fetchone()[0]
                decodes = conn.execute(
                    "SELECT identity_hash, exact_army_id FROM battle_army_decodes WHERE status='decoded' ORDER BY id"
                ).fetchall()
                assert exact_cnt == 1, "permutations must normalize to same army"
                assert len(decodes) == 2
                assert decodes[0][0] == decodes[1][0]
                assert decodes[0][1] == decodes[1][1]
                # Two battles referencing one army count as two uses (check via count of decodes)
                assert (
                    conn.execute(
                        "SELECT count(*) FROM battle_army_decodes WHERE exact_army_id = %s",
                        (decodes[0][1],),
                    ).fetchone()[0]
                    == 2
                )
        finally:
            db.close()


def test_changing_siege_or_cc_troops_does_not_change_identity(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        base = "h0p9e14_32d1x53u2x58-1x97s2x2"
        with_siege = "h0p9e14_32d1x53u1x51-2x58-1x97s2x2"
        with_cc = "h0p9e14_32d1x53u2x58-1x97i1x0s2x2"
        ts = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        _, j_base = store_observation(
            ci,
            archive_server,
            occurrence_key="base-id",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", base, ts)]}).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        _, j_siege = store_observation(
            ci,
            archive_server,
            occurrence_key="siege-id",
            endpoint="battle_log",
            body=json.dumps(
                {
                    "items": [
                        _live_row(True, "#9PP", with_siege, ts + timedelta(hours=1))
                    ]
                }
            ).encode(),
            observed_at=ts + timedelta(hours=1, minutes=1),
            normalized_tag="#2PP",
        )
        _, j_cc = store_observation(
            ci,
            archive_server,
            occurrence_key="cc-id",
            endpoint="battle_log",
            body=json.dumps(
                {"items": [_live_row(True, "#YPP", with_cc, ts + timedelta(hours=2))]}
            ).encode(),
            observed_at=ts + timedelta(hours=2, minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j_base, owner="a1")
            proc.process_job(j_siege, owner="a2")
            proc.process_job(j_cc, owner="a3")
            with db.pool.connection() as conn:
                hashes = [
                    text(r[0])
                    for r in conn.execute(
                        "SELECT identity_hash FROM battle_army_decodes WHERE status='decoded' ORDER BY id"
                    ).fetchall()
                ]
                assert hashes[0] == hashes[1] == hashes[2]
                # siege preserved but not in identity
                siege_rows = conn.execute(
                    "SELECT siege FROM battle_army_decodes WHERE status='decoded' ORDER BY id"
                ).fetchall()
                assert (
                    siege_rows[0][0] == []
                    or siege_rows[0][0] is None
                    or len(siege_rows[0][0]) == 0
                )
                assert len(siege_rows[1][0]) == 1
        finally:
            db.close()


def test_attacker_defender_perspectives_count_once(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        code = "h0p9e14_32d1x53u2x58-1x97s2x2"
        ts = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        attacker_body = json.dumps(
            {"items": [_live_row(True, "#8PP", code, ts)]}
        ).encode()
        defender_row = _live_row(False, "#2PP", code, ts)
        defender_row["opponentPlayerTag"] = "#2PP"
        defender_body = json.dumps({"items": [defender_row]}).encode()
        _, j_att = store_observation(
            ci,
            archive_server,
            occurrence_key="att-persp",
            endpoint="battle_log",
            body=attacker_body,
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        _, j_def = store_observation(
            ci,
            archive_server,
            occurrence_key="def-persp",
            endpoint="battle_log",
            body=defender_body,
            observed_at=ts + timedelta(minutes=2),
            normalized_tag="#8PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j_att, owner="att")
            proc.process_job(j_def, owner="def")
            with db.pool.connection() as conn:
                battles = conn.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0]
                decodes = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE status='decoded'"
                ).fetchone()[0]
                assert battles == 1
                assert decodes == 1, (
                    "defender perspective must not create second army use"
                )
        finally:
            db.close()


def test_correction_replaces_stale_facts(database_url: str, archive_server) -> None:
    with domain_database(database_url) as ci:
        ts1 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        code1 = "h0p9e14_32d1x53u2x58-1x97s2x2"
        code2 = "h0p9e14_32d1x53u1x58-1x97s2x2"  # qty change
        _, j1 = store_observation(
            ci,
            archive_server,
            occurrence_key="corr1",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", code1, ts1)]}).encode(),
            observed_at=ts1 + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j1, owner="c1")
            with db.pool.connection() as conn:
                first_hash = text(
                    conn.execute(
                        "SELECT identity_hash FROM battle_army_decodes WHERE is_active=true"
                    ).fetchone()[0]
                )
            # reprocess same battle with corrected attacker code (new observation, later timestamp, same battle identity)
            _, j2 = store_observation(
                ci,
                archive_server,
                occurrence_key="corr2",
                endpoint="battle_log",
                body=json.dumps(
                    {"items": [_live_row(True, "#8PP", code2, ts1)]}
                ).encode(),
                observed_at=ts1 + timedelta(minutes=5),
                normalized_tag="#2PP",
            )
            proc.process_job(j2, owner="c2")
            with db.pool.connection() as conn:
                active = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE is_active=true"
                ).fetchone()[0]
                total = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes"
                ).fetchone()[0]
                second_hash = text(
                    conn.execute(
                        "SELECT identity_hash FROM battle_army_decodes WHERE is_active=true"
                    ).fetchone()[0]
                )
                assert active == 1
                assert total == 2, "old result remains auditable but not active"
                assert first_hash != second_hash
                # stale current-version rows removed (is_active false)
                assert (
                    conn.execute(
                        "SELECT count(*) FROM battle_army_decodes WHERE is_active=false"
                    ).fetchone()[0]
                    == 1
                )
        finally:
            db.close()
