import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agari_kit.collection import collect_selfplay, file_sha256, verify_record, verified_split
from agari_kit.dataset import Catalog
from agari_kit.mjai import DataError, read_events
from agari_kit.storage import Storage


class CollectionTests(unittest.TestCase):
    def test_wall_budget_pauses_after_verified_game_and_resumes_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp))
            args = dict(name="budget", games=2, seed_start=100, agents=["efficiency"] * 4)
            # Start, first game allowed, next game disallowed, final timing.
            with patch("agari_kit.collection.time.monotonic", side_effect=[0, 0, 11, 11]):
                first = collect_selfplay(storage, **args, max_wall_seconds=10)
            self.assertEqual((first["status"], first["added"]), ("paused", 1))
            state_path = storage.path("datasets/manifests/budget/acquisition.json")
            state = json.loads(state_path.read_text())
            self.assertNotIn("completed_at", state)
            self.assertNotIn("max_wall_seconds", state["config"])
            with Catalog(Path(first["catalog"])) as catalog:
                saved = catalog.games()[0]
            second = collect_selfplay(storage, **args, max_wall_seconds=60)
            self.assertEqual((second["status"], second["added"], second["reused"]), ("complete", 1, 1))
            with Catalog(Path(second["catalog"])) as catalog:
                records = catalog.games()
                self.assertEqual(len(records), 2)
                self.assertIn(saved, records)
            verify_record(storage, saved)

    def test_budget_before_first_game_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp))
            args = dict(name="empty-budget", games=1, seed_start=100, agents=["efficiency"] * 4)
            for invalid in (0, -1, float("nan"), float("inf"), True):
                with self.assertRaisesRegex(DataError, "finite and positive"):
                    collect_selfplay(storage, **args, max_wall_seconds=invalid)
            self.assertFalse(storage.path("datasets/manifests/empty-budget").exists())
            with patch("agari_kit.collection.time.monotonic", side_effect=[0, 11, 11]):
                paused = collect_selfplay(storage, **args, max_wall_seconds=10)
            self.assertEqual((paused["status"], paused["added"]), ("paused", 0))
            self.assertEqual(collect_selfplay(storage, **args)["status"], "complete")

    def test_raw_preservation_resume_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp))
            args = dict(name="small", games=2, seed_start=100, agents=["efficiency"] * 4)
            first = collect_selfplay(storage, **args)
            with Catalog(Path(first["catalog"])) as catalog:
                original = catalog.games()
            second = collect_selfplay(storage, **args)
            with Catalog(Path(first["catalog"])) as catalog:
                self.assertEqual(original, catalog.games())
            self.assertEqual((first["added"], second["added"], second["reused"]), (2, 0, 2))
            raw = list(read_events(storage.path("datasets/raw/small/game-000000.jsonl.gz")))
            normalized = list(read_events(storage.path(original[0]["mjai_path"])))
            self.assertNotIn("scores", raw[-1])
            self.assertEqual(len(normalized[-1]["scores"]), 4)
            import polars as pl
            self.assertEqual(pl.read_parquet(storage.path("datasets/manifests/small/metadata.parquet")).height, 2)
            trace = storage.path(original[0]["trace_path"])
            trace.write_bytes(trace.read_bytes() + b"tampered")
            with self.assertRaisesRegex(DataError, "hashes disagree"):
                verify_record(storage, original[0])

    def test_validation_failure_is_quarantined_and_resume_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp))
            args = dict(name="retry", games=1, seed_start=5, agents=["random"] * 4)
            with patch("agari_kit.validation.validate_mjai", side_effect=DataError("fixture failure")):
                with self.assertRaisesRegex(DataError, "fixture failure"):
                    collect_selfplay(storage, **args)
            failures = list(storage.path("datasets/quarantine/retry").glob("*/failure.json"))
            self.assertEqual(len(failures), 1)
            self.assertEqual(json.loads(failures[0].read_text())["index"], 0)
            result = collect_selfplay(storage, **args)
            self.assertEqual(result["status"], "complete")
            state = json.loads(storage.path("datasets/manifests/retry/acquisition.json").read_text())
            self.assertNotIn("failure", state)

    def test_frozen_dataset_requires_artifacts_not_a_verification_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp))
            storage.initialize(min_free_bytes=0)
            path = storage.path("datasets/manifests/fake.sqlite")
            with Catalog(path) as catalog:
                catalog.register(game_id="forged", sha256="0" * 64,
                                 played_at="2026-01-01T00:00:00Z", source="selfplay",
                                 rules="4p-red-half/tenhou", usage_basis="fixture",
                                 validation="engine-verified")
            with self.assertRaisesRegex(DataError, "artifact field"):
                verified_split(storage, path, storage.path("datasets/manifests/frozen.json"))

    def test_model_version_change_cannot_resume_or_mix_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp))
            args = dict(name="bound", games=1, seed_start=5, agents=["random"] * 4)
            collect_selfplay(storage, **args)
            with patch("agari_kit.collection.agent_provenance", return_value={"kind": "changed"}):
                with self.assertRaisesRegex(DataError, "configuration changed"):
                    collect_selfplay(storage, **args)
            # Initial provenance is frozen, then checked again before a game.
            original = {"kind": "builtin", "name": "random"}
            with patch("agari_kit.collection.agent_provenance",
                       side_effect=[original] * 4 + [{"kind": "changed"}] * 4):
                with self.assertRaisesRegex(DataError, "changed during collection"):
                    collect_selfplay(storage, **dict(args, name="changed"))
            self.assertFalse(storage.path("datasets/manifests/changed/game-000000.json").exists())


if __name__ == "__main__":
    unittest.main()
