import hashlib
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agari_kit.agent import make_agent
from agari_kit.plugins import agent_provenance, load_agent


class PluginTests(unittest.TestCase):
    def test_explicit_factory_and_weight_hash_binding(self):
        class Agent:
            def __init__(self, weights_path, device):
                self.weights_path, self.device = weights_path, device
            def reset(self, context):
                pass
            def act(self, observation, context):
                return None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()
            (path / "weights.pt").write_bytes(b"test-only")
            descriptor = {"factory": "fixture.model:Agent", "kwargs": {"device": "cpu"},
                          "artifacts": {"weights_path": {"path": "weights.pt",
                                        "sha256": hashlib.sha256(b"test-only").hexdigest()}}}
            (path / "agent.json").write_text(json.dumps(descriptor))
            with patch("agari_kit.plugins.importlib.import_module", return_value=types.SimpleNamespace(Agent=Agent)):
                agent = make_agent("@" + str(path / "agent.json"))
                self.assertEqual(agent.weights_path, str(path / "weights.pt"))
                self.assertEqual(agent.device, "cpu")
                (path / "weights.pt").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    make_agent("@" + str(path / "agent.json"))


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / "agari_provenance_fixture"
        self.package.mkdir()
        (self.package / "__init__.py").write_text("")
        (self.package / "model.py").write_text(
            "class Agent:\n"
            "    def __init__(self, weights_path, device='cpu'):\n"
            "        self.weights_path, self.device = weights_path, device\n"
            "    def reset(self, context): pass\n"
            "    def act(self, observation, context): pass\n"
        )
        (self.package / "features.py").write_text("FEATURE_VERSION = 1\n")
        (self.root / "assets").mkdir()
        self.weights = self.root / "assets" / "weights.pt"
        self.weights.write_bytes(b"first weights")
        self.descriptor = {"factory": "agari_provenance_fixture.model:Agent", "kwargs": {"device": "cpu"},
                           "artifacts": {"weights_path": {"path": "assets/weights.pt",
                                         "sha256": hashlib.sha256(self.weights.read_bytes()).hexdigest()}}}
        self.path = self.root / "agent.json"
        self.write_descriptor()
        search_path = patch.object(sys, "path", [str(self.root), *sys.path])
        search_path.start()
        self.addCleanup(search_path.stop)
        self.addCleanup(self.remove_fixture_modules)
        importlib.invalidate_caches()

    @staticmethod
    def remove_fixture_modules():
        for name in list(sys.modules):
            if name == "agari_provenance_fixture" or name.startswith("agari_provenance_fixture."):
                del sys.modules[name]
        importlib.invalidate_caches()

    def write_descriptor(self):
        self.path.write_text(json.dumps(self.descriptor))

    def provenance(self):
        return agent_provenance("@" + str(self.path))

    def test_builtin_provenance_and_unknown_name(self):
        self.assertEqual(agent_provenance("efficiency"), {"kind": "builtin", "name": "efficiency"})
        with self.assertRaisesRegex(ValueError, "Unknown agent"):
            agent_provenance("missing")

    def test_descriptor_changes_are_part_of_identity(self):
        before = self.provenance()
        self.descriptor["kwargs"]["device"] = "mps"
        self.write_descriptor()
        after = self.provenance()
        self.assertNotEqual(before["descriptor_sha256"], after["descriptor_sha256"])
        self.assertEqual(before["source"], after["source"])
        self.assertEqual(after["descriptor"], self.descriptor)
        self.assertEqual(load_agent(str(self.path)).device, "mps")

    def test_weights_must_match_and_updated_weights_change_identity(self):
        before = self.provenance()
        self.weights.write_bytes(b"new weights")
        for check in (self.provenance, lambda: load_agent(str(self.path))):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                check()
        self.descriptor["artifacts"]["weights_path"]["sha256"] = hashlib.sha256(self.weights.read_bytes()).hexdigest()
        self.write_descriptor()
        after = self.provenance()
        self.assertNotEqual(before["artifacts"], after["artifacts"])
        self.assertEqual(after["artifacts"]["weights_path"]["sha256"],
                         after["artifacts"]["weights_path"]["declared_sha256"])

    def test_package_helpers_are_bound_but_other_packages_are_excluded(self):
        before = self.provenance()
        self.assertEqual(before["source"]["module"], "agari_provenance_fixture.model")
        self.assertEqual(before["source"]["files"], 3)
        (self.root / "unrelated_dependency.py").write_text("UNRELATED = True\n")
        self.assertEqual(before["source"], self.provenance()["source"])
        (self.package / "features.py").write_text("FEATURE_VERSION = 2\n")
        after = self.provenance()
        self.assertNotEqual(before["source"]["source_sha256"], after["source"]["source_sha256"])
        self.assertEqual(before["descriptor_sha256"], after["descriptor_sha256"])

    def test_relative_descriptor_and_artifacts_resolve_consistently(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            provenance = agent_provenance("@agent.json")
            agent = load_agent("agent.json")
        finally:
            os.chdir(previous)
        self.assertEqual(provenance["descriptor_path"], str(self.path))
        self.assertEqual(provenance["artifacts"]["weights_path"]["path"], str(self.weights))
        self.assertEqual(agent.weights_path, str(self.weights))
        self.assertEqual(provenance["descriptor_sha256"], self.provenance()["descriptor_sha256"])

    def test_both_apis_reject_escaping_artifact_paths(self):
        for name in (str(self.weights), "../weights.pt"):
            self.descriptor["artifacts"]["weights_path"]["path"] = name
            self.write_descriptor()
            for check in (self.provenance, lambda: load_agent(str(self.path))):
                with self.subTest(path=name), self.assertRaisesRegex(ValueError, "packaged beside"):
                    check()


if __name__ == "__main__":
    unittest.main()
