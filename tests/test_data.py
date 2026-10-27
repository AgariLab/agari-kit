import gzip
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from agari_kit.dataset import Catalog, freeze_split, make_split
from agari_kit.mjai import DataError, inspect_game, player_event, read_events


def fixture():
    # Synthetic structural fixture, not a playable or engine-verified hanchan.
    return [{"type": "start_game"},
            {"type": "start_kyoku", "tehais": [["1m"] * 13 for _ in range(4)]},
            {"type": "end_kyoku"},
            {"type": "end_game", "scores": [25000] * 4}]


def metadata(index):
    return {"game_id": f"selfplay:{index:04d}",
            "sha256": hashlib.sha256(str(index).encode()).hexdigest(),
            "played_at": f"2026-01-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
            "source": "selfplay-fixture", "rules": "4p-red-half/tenhou",
            "usage_basis": "synthetic unit-test metadata only", "validation": "engine-verified"}


class DataTests(unittest.TestCase):
    def test_gzip_and_plain_hash_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain, zipped = Path(tmp) / "one.jsonl", Path(tmp) / "one.jsonl.gz"
            plain.write_text("".join(json.dumps(e) + "\n" for e in fixture()))
            with gzip.open(zipped, "wt") as stream:
                stream.write("".join(json.dumps(e, sort_keys=True) + "\n" for e in fixture()))
            first = inspect_game(read_events(plain))
            self.assertEqual(first, inspect_game(read_events(zipped)))
            self.assertEqual(first["validation"], "structural-only")

    def test_bad_record_is_locatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text('{"type":"start_game"}\nno-json\n')
            with self.assertRaisesRegex(DataError, "bad.jsonl:2"):
                list(read_events(path))

    def test_incomplete_duplicate_and_after_end_games_fail(self):
        for events in (fixture()[:-1], fixture() + fixture(), [], fixture()[1:]):
            with self.subTest(events=events), self.assertRaises(DataError):
                inspect_game(events)

    def test_bounded_line_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.jsonl"
            path.write_text("x" * 200)
            with self.assertRaisesRegex(DataError, "size limit"):
                list(read_events(path, max_line_bytes=100))

    def test_private_hands_draws_and_future_annotations_are_removed(self):
        event = {"type": "start_kyoku", "tehais": [[str(i)] * 13 for i in range(4)],
                 "wall": ["secret"], "future": {"winner": 2}}
        projected = player_event(event, 2)
        self.assertEqual(projected["tehais"][2], ["2"] * 13)
        for seat in (0, 1, 3):
            self.assertEqual(projected["tehais"][seat], ["?"] * 13)
        self.assertNotIn("wall", projected)
        self.assertNotIn("future", projected)
        self.assertEqual(event["tehais"][0][0], "0")
        self.assertEqual(player_event({"type": "tsumo", "actor": 1, "pai": "5mr"}, 2)["pai"], "?")
        self.assertEqual(player_event({"type": "tsumo", "actor": 2, "pai": "5mr"}, 2)["pai"], "5mr")
        self.assertNotIn("ura_markers", player_event({"type": "hora", "actor": 1, "ura_markers": ["1m"]}, 2))
        with self.assertRaises(DataError):
            player_event({"type": "future_extension", "hidden": 1}, 0)

    def test_catalog_idempotence_and_conflicting_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp, Catalog(Path(tmp) / "catalog.sqlite") as db:
            self.assertTrue(db.register(**metadata(1)))
            self.assertFalse(db.register(**metadata(1)))
            conflict = dict(metadata(1), game_id="other-id")
            with self.assertRaisesRegex(DataError, "conflicting"):
                db.register(**conflict)
            self.assertEqual(len(db.games()), 1)

    def test_whole_game_split_is_disjoint_chronological_and_frozen(self):
        games = [metadata(i) for i in reversed(range(100))]
        manifest = make_split(games)
        partitions = manifest["partitions"]
        self.assertEqual([len(partitions[key]) for key in ("train", "validation", "test")], [90, 5, 5])
        all_ids = [game["game_id"] for part in partitions.values() for game in part]
        self.assertEqual(len(set(all_ids)), 100)
        self.assertLess(partitions["train"][-1]["played_at"], partitions["validation"][0]["played_at"])
        self.assertEqual(manifest, make_split(list(reversed(games))))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "split.json"
            self.assertTrue(freeze_split(path, manifest))
            self.assertFalse(freeze_split(path, manifest))
            with self.assertRaisesRegex(DataError, "Frozen split"):
                freeze_split(path, make_split(games + [metadata(100)]))

    def test_unverified_mixed_duplicate_and_too_small_splits_rejected(self):
        games = [metadata(i) for i in range(20)]
        for bad in (games[:19], games + [games[0]],
                    [dict(games[0], validation="structural-only")] + games[1:],
                    [dict(games[0], source="different-platform")] + games[1:]):
            with self.subTest(bad=bad), self.assertRaises(DataError):
                make_split(bad)

    def test_failed_freeze_does_not_publish_a_partial_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frozen.json"
            manifest = make_split([metadata(i) for i in range(20)])
            with patch("agari_kit.dataset.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    freeze_split(path, manifest)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).glob(".agari-split-*")), [])
            self.assertTrue(freeze_split(path, manifest))
            self.assertFalse(freeze_split(path, manifest))


if __name__ == "__main__":
    unittest.main()
