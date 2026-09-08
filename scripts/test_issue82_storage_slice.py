"""Focused checks for the issue #82 storage-slice helpers (no PG/GCS)."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]

from scripts.issue82_storage_slice import (
    MAX_LOG_COLUMN_BYTES,
    _seed_tag,
    atomic_write_json,
    classify_body,
    derive_log_width,
    percentiles,
)


class ClassifyBodyTest(unittest.TestCase):
    def test_profile_shape(self):
        body = json.dumps({"tag": "#X", "name": "n", "trophies": 6000,
                           "attackWins": 1}).encode()
        self.assertEqual(classify_body(body), ("profile", 0))

    def test_battle_log_shape(self):
        body = json.dumps({"items": [{"battleType": "homeVillage",
                                      "stars": 3}]}).encode()
        self.assertEqual(classify_body(body), ("battle_log", 1))

    def test_rankings_shape(self):
        items = [{"rank": i + 1, "tag": "#Y", "trophies": 9000}
                 for i in range(200)]
        body = json.dumps({"items": items, "paging": {}}).encode()
        category, count = classify_body(body)
        self.assertEqual((category, count), ("global_player_rankings", 200))

    def test_empty_items_stay_ambiguous(self):
        category, count = classify_body(b'{"items": []}')
        self.assertEqual((category, count), ("empty_items", 0))

    def test_non_json_and_non_object(self):
        self.assertEqual(classify_body(b"\xff\xfe\x00")[0], "undecodable")
        self.assertEqual(classify_body(b"[1,2]")[0], "unknown")
        self.assertEqual(classify_body(b'{"a": 1}')[0], "unknown")

    def test_no_values_leak(self):
        # Only category and counts come back; nothing identifier-bearing.
        result = classify_body(json.dumps(
            {"tag": "#SECRET", "trophies": 1}).encode())
        self.assertEqual(result, ("profile", 0))
        self.assertNotIn("#SECRET", repr(result))


class SeedTagTest(unittest.TestCase):
    def test_valid_charset_and_unique(self):
        import re

        tags = [_seed_tag(n) for n in range(1, 12501)]
        self.assertEqual(len(set(tags)), 12500)
        pattern = re.compile(r"^#[0289PYLQGRJCUV]+$")
        for tag in tags[::1234]:
            self.assertIsNotNone(pattern.fullmatch(tag))


class PercentilesTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(percentiles([])["count"], 0)

    def test_known_distribution(self):
        values = list(range(1, 101))
        got = percentiles(values)
        self.assertEqual(got["count"], 100)
        self.assertEqual(got["min"], 1)
        self.assertEqual(got["max"], 100)
        self.assertEqual(got["mean"], 50.5)
        self.assertTrue(got["p50"] <= got["p90"] <= got["p95"] <= got["p99"])


class DeriveLogWidthTest(unittest.TestCase):
    """Decision-bearing math: the live-working-set width per scenario."""

    def test_proportional_scaling(self):
        # 24,008 B column for a ~21.6 KB synthetic body, scaled to the
        # measured battle-log p50 body size.
        width = derive_log_width(24008.0, 21665.0, 66116.0)
        self.assertAlmostEqual(width, 24008.0 * 66116.0 / 21665.0, places=1)

    def test_p90_exceeds_p50(self):
        p50 = derive_log_width(24008.0, 21665.0, 66116.0)
        p90 = derive_log_width(24008.0, 21665.0, 67377.0)
        self.assertGreater(p90, p50)

    def test_cap_clamps(self):
        self.assertEqual(
            derive_log_width(24008.0, 21665.0, 10_000_000.0),
            MAX_LOG_COLUMN_BYTES)

    def test_non_positive_inputs_yield_zero(self):
        self.assertEqual(derive_log_width(0.0, 21665.0, 66116.0), 0.0)
        self.assertEqual(derive_log_width(24008.0, 0.0, 66116.0), 0.0)
        self.assertEqual(derive_log_width(24008.0, 21665.0, -1.0), 0.0)


class AtomicWriteTest(unittest.TestCase):
    def test_write_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            digest = atomic_write_json(path, {"b": 2, "a": 1})
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            self.assertEqual(json.loads(raw), {"a": 1, "b": 2})
            sidecar = Path(str(path) + ".sha256").read_text()
            self.assertTrue(sidecar.startswith(digest))


if __name__ == "__main__":
    unittest.main()
