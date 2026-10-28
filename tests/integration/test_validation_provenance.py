"""Native receipts bind four recorded agents without inspecting held-out play."""

import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agari_kit.collection import file_sha256, record_game, verify_record, write_json
from agari_kit.mjai import DataError, canonical_json
from agari_kit.storage import Storage
from agari_kit.validation import validate_mjai


class ReceiptAgentProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.storage = Storage(Path(cls.temporary.name))
        descriptor = cls.storage.path("teacher.json")
        descriptor.write_text(json.dumps({"factory": "agari_kit.efficiency:EfficiencyAgent"}))
        cls.names = ["@" + str(descriptor), "random", "efficiency", "tsumogiri"]
        cls.paths = {key: cls.storage.path(key + ".jsonl.gz") for key in ("raw", "mjai", "trace")}
        record_game(seed=610010, agents=cls.names, raw_path=cls.paths["raw"],
                    mjai_path=cls.paths["mjai"], trace_path=cls.paths["trace"])
        with gzip.open(cls.paths["trace"], "rt") as stream:
            cls.trace = [json.loads(line) for line in stream]

    def write_trace(self, rows, name):
        path = self.storage.path(name + ".jsonl")
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def test_receipt_hashes_complete_mixed_provenance_in_seat_order(self):
        receipt = validate_mjai(self.paths["mjai"], self.paths["trace"])
        expected = [hashlib.sha256(canonical_json(item).encode()).hexdigest()
                    for item in self.trace[0]["agent_provenance"]]
        self.assertEqual(receipt["agent_provenance_sha256"], expected)
        self.assertEqual(len(set(expected)), 4)
        self.assertEqual(receipt["seed"], 610010)
        self.assertEqual(receipt["trace_sha256"], file_sha256(self.paths["trace"]))
        self.assertEqual(receipt["validation"], "engine-verified")

    def test_names_and_malformed_present_provenance_fail_before_replay(self):
        variants = [None, [], {}, [None] * 4, self.trace[0]["agent_provenance"][:3]]
        mismatched = copy.deepcopy(self.trace[0]["agent_provenance"])
        mismatched[0]["name"] = "random"
        variants.append(mismatched)
        wrong_kind = copy.deepcopy(self.trace[0]["agent_provenance"])
        wrong_kind[0]["kind"] = "builtin"
        variants.append(wrong_kind)
        incomplete = copy.deepcopy(self.trace[0]["agent_provenance"])
        incomplete[0].pop("kind")
        variants.append(incomplete)
        nonfinite = copy.deepcopy(self.trace[0]["agent_provenance"])
        nonfinite[0]["measurement"] = float("nan")
        variants.append(nonfinite)
        for index, value in enumerate(variants):
            rows = copy.deepcopy(self.trace)
            rows[0]["agent_provenance"] = value
            path = self.write_trace(rows, f"malformed-{index}")
            with self.subTest(case=index), patch("agari_kit.validation.new_environment", side_effect=AssertionError("Reject header before replay")), self.assertRaises(DataError):
                validate_mjai(self.paths["mjai"], path)

    def test_nested_provenance_changes_are_bound_and_changed_receipt_is_rejected(self):
        original = validate_mjai(self.paths["mjai"], self.paths["trace"])
        rows = copy.deepcopy(self.trace)
        rows[0]["agent_provenance"][0]["source"]["source_sha256"] = "a" * 64
        changed_path = self.write_trace(rows, "changed-content")
        changed = validate_mjai(self.paths["mjai"], changed_path)
        self.assertNotEqual(original["trace_sha256"], changed["trace_sha256"])
        self.assertNotEqual(original["agent_provenance_sha256"][0], changed["agent_provenance_sha256"][0])
        self.assertEqual(original["agent_provenance_sha256"][1:], changed["agent_provenance_sha256"][1:])
        receipt_path = self.storage.path("bound.receipt.json")
        write_json(receipt_path, original)
        record = {"game_id": "selfplay:" + original["sha256"], "sha256": original["sha256"],
                  "mjai_path": self.paths["mjai"].name, "trace_path": self.paths["trace"].name,
                  "validation_receipt": receipt_path.name, "validation_receipt_sha256": file_sha256(receipt_path)}
        verify_record(self.storage, record, replay=True)
        tampered = copy.deepcopy(original)
        tampered["agent_provenance_sha256"][1] = tampered["agent_provenance_sha256"][0]
        write_json(receipt_path, tampered)
        with self.assertRaisesRegex(DataError, "Validation receipt changed"):
            verify_record(self.storage, record, replay=False)

    def test_legacy_header_and_receipt_without_provenance_remain_compatible(self):
        rows = copy.deepcopy(self.trace)
        rows[0].pop("agent_provenance")
        path = self.write_trace(rows, "legacy-no-provenance")
        receipt = validate_mjai(self.paths["mjai"], path)
        self.assertNotIn("agent_provenance_sha256", receipt)
        self.assertEqual(receipt["validation"], "engine-verified")
        self.assertEqual(receipt["seed"], 610010)
        receipt_path = self.storage.path("legacy.receipt.json")
        write_json(receipt_path, receipt)
        record = {"game_id": "selfplay:" + receipt["sha256"], "sha256": receipt["sha256"],
                  "mjai_path": self.paths["mjai"].name, "trace_path": path.name,
                  "validation_receipt": receipt_path.name, "validation_receipt_sha256": file_sha256(receipt_path)}
        self.assertEqual(verify_record(self.storage, record, replay=True)["seed"], 610010)
        # Existing provenance-bearing archives may also carry an older receipt.
        older = validate_mjai(self.paths["mjai"], self.paths["trace"])
        older.pop("agent_provenance_sha256")
        write_json(receipt_path, older)
        record.update(trace_path=self.paths["trace"].name, validation_receipt_sha256=file_sha256(receipt_path))
        self.assertNotIn("agent_provenance_sha256", verify_record(self.storage, record, replay=True))


if __name__ == "__main__":
    unittest.main()
