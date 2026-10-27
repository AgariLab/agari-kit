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
