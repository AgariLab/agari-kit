import contextlib
from dataclasses import asdict
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agari_kit.agent import Rules
from agari_kit.storage import Storage

KIT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("qualification_script", KIT / "scripts/build_mortal_qualification.py")
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class QualificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.storage = Storage(self.root)
        self.teacher = {"kind": "external", "identity": "fixed teacher"}
        self.engine = {"version": "0.4.10", "binary_sha256": "test-engine"}
        self.code = {"source_sha256": "test-code"}
        shared = {"status": "passed", "teacher_provenance": self.teacher,
                  "teacher_provenance_after": self.teacher, "engine": self.engine,
                  "rules": asdict(Rules())}
        self.native = {**shared, "case_status": {case: True for case in module.REQUIRED_CASES},
                       "fixture_sha256": module.digest(KIT / "tests/integration/test_rules.py"),
                       "test_sha256": module.digest(KIT / "tests/integration/test_mortal_runtime_native.py")}
        self.games = {**shared, "code_before": self.code, "code_after": self.code, "games": []}
        for seat in range(4):
            receipt = {"validation": "engine-verified"}
            for suffix, field in (("mjai.jsonl.gz", "mjai_file_sha256"), ("trace.jsonl.gz", "trace_sha256")):
                path = self.root / f"game-{seat:03d}.{suffix}"
                path.write_bytes(f"fixture {seat} {suffix}".encode())
                receipt[field] = module.digest(path)
            self.games["games"].append({"seat": seat, "illegal_actions": 0, "validation": receipt})

    def run_builder(self):
        (self.root / "native.json").write_text(json.dumps(self.native))
        (self.root / "games.json").write_text(json.dumps(self.games))
        argv = ["build_mortal_qualification.py", "--agent", str(self.root / "agent.json"),
                "--native-report", "native.json", "--games-report", "games.json", "--output", "qualification"]
        with (patch.object(module.Storage, "from_env", return_value=self.storage),
              patch.object(module.Storage, "check", return_value={}),
              patch.object(module, "managed_agents", side_effect=lambda _: contextlib.nullcontext()),
              patch.object(module, "agent_provenance", return_value=self.teacher),
              patch.object(module, "engine_identity", return_value=self.engine),
              patch.object(module, "source_fingerprint", return_value=self.code),
              patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO())):
            module.main()

    def test_complete_evidence_binds_all_artifacts(self):
        self.run_builder()
        report = json.loads((self.root / "qualification/report.json").read_text())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["checks"]), 16)
        for item in report["checks"]:
            self.assertEqual(item["evidence_sha256"], module.digest(self.root / item["evidence_path"]))
        self.assertEqual(report["teacher_provenance_sha256"],
                         hashlib.sha256(module.canonical_json(self.teacher).encode()).hexdigest())

    def test_teacher_changed_after_probe_is_rejected(self):
        self.native["teacher_provenance_after"] = {"identity": "other teacher"}
        with self.assertRaisesRegex(ValueError, "identity differs"):
            self.run_builder()

    def test_missing_or_failed_boundary_is_rejected(self):
        self.native["case_status"].pop(next(iter(module.REQUIRED_CASES)))
        with self.assertRaisesRegex(ValueError, "scenarios"):
            self.run_builder()

    def test_changed_raw_evidence_is_rejected(self):
        (self.root / "game-002.trace.jsonl.gz").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.run_builder()

    def test_incomplete_seat_coverage_is_rejected(self):
        self.games["games"][3]["seat"] = 0
        with self.assertRaisesRegex(ValueError, "Four-seat"):
            self.run_builder()

    def test_code_drift_is_rejected(self):
        self.games["code_after"] = {"source_sha256": "changed"}
        with self.assertRaisesRegex(ValueError, "code changed"):
            self.run_builder()


if __name__ == "__main__":
    unittest.main()
