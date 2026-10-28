import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agari_kit.agent import Context, Rules
from agari_kit.opponents.mortal import MortalAgent, MortalError


class Action:
    def __init__(self, message, tile=None):
        self.message, self.tile = message, tile

    def to_mjai(self):
        return json.dumps(self.message)


class Observation:
    player_id = 0

    def __init__(self, events, actions, drawn=None, forced=False):
        self.events, self.actions, self.drawn, self.forced = events, actions, drawn, forced

    def new_events(self):
        return [json.dumps(event) for event in self.events]

    def legal_actions(self):
        return self.actions

    def serialize_to_base64(self):
        return base64.b64encode(json.dumps({"drawn_tile": self.drawn, "forced_tedashi": self.forced}).encode()).decode()


DISCARD = {"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": True}


def initial(actions=None):
    return Observation([
        {"type": "start_game", "secret_wall": [99]},
        {"type": "start_kyoku", "tehais": [["1m"] * 13 for _ in range(4)], "future": "secret"},
        {"type": "tsumo", "actor": 1, "pai": "9m"},
        {"type": "dahai", "actor": 1, "pai": "9m", "tsumogiri": True},
        {"type": "tsumo", "actor": 0, "pai": "1m"},
    ], actions or [Action(DISCARD, 0)], drawn=0)


class MortalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.source = self.root / "source"
        (self.source / "weights").mkdir(parents=True)
        (self.source / "runtime").mkdir()
        for relative in ("inference.py", "model-identity.json", "weights/s42-inference.pth", "runtime/LICENSE"):
            (self.source / relative).write_text("test fixture")
        self.python = self.root / "fake-python"
        self.python.write_text(
            f"#!{sys.executable}\n"
            "import json,os,sys,time\n"
            "from pathlib import Path\n"
            "config=json.loads(Path('../test-config.json').read_text())\n"
            "Path('../token-present').write_text(str('RIICHI_BOT_TOKEN' in os.environ))\n"
            "Path('../pid').write_text(str(os.getpid()))\n"
            "print(json.dumps({'ready':True,'protocol':'agari-mortal-worker-v1'}),flush=True)\n"
            "for line in sys.stdin:\n"
            " req=json.loads(line)\n"
            " with open('../requests.jsonl','a') as f:f.write(line)\n"
            " if req['command']=='reset':out={'id':req['id'],'reset':True}\n"
            " else:\n"
            "  if config.get('exit'):sys.exit(3)\n"
            "  time.sleep(config.get('sleep',0))\n"
            "  out={'id':req['id']+(1 if config.get('wrong_id') else 0),'action':config['actions'].pop(0)}\n"
            "  if config.get('error'):out={'id':req['id'],'error':'fixture error'}\n"
            " if req['command']=='act' and config.get('bad_json'):print('{',flush=True)\n"
            " else:print(json.dumps(out)+('\\n{}' if req['command']=='act' and config.get('extra') else ''),flush=True)\n"
        )
        self.python.chmod(0o755)
        self.config = {"actions": [DISCARD]}
        self.dependency = self.root / "dependency.py"
        self.dependency.write_text("# fixed isolated dependency\n")

    def make(self, **kwargs):
        (self.root / "test-config.json").write_text(json.dumps(self.config))
        files = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.source.rglob("*") if p.is_file()}
        inventory = self.root / "environment-files.json"
        inventory.write_text(json.dumps({"schema_version": 1, "files": {
            "dependency.py": hashlib.sha256(self.dependency.read_bytes()).hexdigest()}}))
        files["environment-files.json"] = hashlib.sha256(inventory.read_bytes()).hexdigest()
        manifest = {"schema_version": 1, "kind": "mortal-runtime-v1", "checkout": "source",
                    "python": {"path": str(self.python), "sha256": hashlib.sha256(self.python.read_bytes()).hexdigest()},
                    "environment_manifest": "environment-files.json", "files": files}
        self.manifest_path = self.root / "runtime-manifest.json"
        self.manifest_path.write_text(json.dumps(manifest))
        agent = MortalAgent(manifest_path=str(self.manifest_path), **kwargs)
        self.addCleanup(agent.close)
        agent.reset(Context(0, 2026))
        return agent

    def test_visible_events_and_worker_reused_across_games(self):
        self.config["actions"] = [DISCARD, DISCARD]
        with patch.dict(os.environ, {"RIICHI_BOT_TOKEN": "not-a-real-token"}):
            agent = self.make()
        observation = initial()
        self.assertIs(agent.act(observation, Context(0, 2026)), observation.actions[0])
        pid = (self.root / "pid").read_text()
        agent.reset(Context(0, 2027))
        agent.act(initial(), Context(0, 2027))
        self.assertEqual((self.root / "pid").read_text(), pid)
        self.assertEqual((self.root / "token-present").read_text(), "False")
        requests = [json.loads(x) for x in (self.root / "requests.jsonl").read_text().splitlines()]
        events = requests[1]["events"]
        self.assertNotIn("secret_wall", events[0])
        self.assertNotIn("future", events[1])
        self.assertEqual(events[1]["tehais"][1], ["?"] * 13)
        self.assertEqual(events[2]["pai"], "?")
        self.assertEqual([e["can_act"] for e in events], [False] * 4 + [True])

    def test_delayed_dora_preserves_order_and_decision_trigger(self):
        self.config["actions"] = [{"type": "none"}]
        agent = self.make()
        obs = initial([Action({"type": "none"})])
        obs.events[-1:] = [{"type": "kakan", "actor": 1, "pai": "5mr", "consumed": ["5m"] * 3},
                          {"type": "dora", "dora_marker": "1p"}]
        agent.act(obs, Context(0, 2026))
        events = json.loads((self.root / "requests.jsonl").read_text().splitlines()[-1])["events"]
        self.assertEqual([(e["type"], e["can_act"]) for e in events[-2:]], [("kakan", True), ("dora", False)])

    def test_reach_followup_discard_and_meta(self):
        self.config["actions"] = [{"type": "reach", "actor": 0, "meta": {"q_values": [1.2]}},
                                  {**DISCARD, "tsumogiri": False}]
        agent = self.make()
        reach = Action({"type": "reach", "actor": 0})
        self.assertIs(agent.act(initial([reach]), Context(0, 2026)), reach)
        discard = Action(DISCARD, 0)
        obs = Observation([{"type": "reach", "actor": 0}], [discard], drawn=0, forced=True)
        self.assertIs(agent.act(obs, Context(0, 2026)), discard)

    def test_strict_red_identity_and_physical_tsumogiri(self):
        for move in ({**DISCARD, "pai": "5mr"}, {**DISCARD, "tsumogiri": False}, {**DISCARD, "actor": 2}):
            with self.subTest(move=move):
                self.config["actions"] = [move]
                agent = self.make()
                with self.assertRaises(MortalError):
                    agent.act(initial(), Context(0, 2026))
                self.assertIsNone(agent._process)

    def test_no_reaction_only_accepts_mandatory_pass(self):
        self.config["actions"] = [None]
        agent = self.make()
        obs = initial([Action({"type": "none"})])
        self.assertIs(agent.act(obs, Context(0, 2026)), obs.actions[0])
        agent.close()
        agent = self.make()
        with self.assertRaises(MortalError):
            agent.act(initial(), Context(0, 2026))

    def test_unsupported_ankan_ron_window_is_not_silently_passed(self):
        self.config["actions"] = [None]
        agent = self.make()
        obs = initial([Action({"type": "none"}), Action({"type": "hora", "actor": 0, "target": 1})])
        obs.events[-1] = {"type": "ankan", "actor": 1, "consumed": ["1m"] * 4}
        with self.assertRaisesRegex(MortalError, "nontrivial"):
            agent.act(obs, Context(0, 2026))

    def test_libriichi_hora_without_tile_and_kyushu_without_actor(self):
        for move, native in (({"type": "hora", "actor": 0, "target": 0},
                              {"type": "hora", "actor": 0, "target": 0, "pai": "1m"}),
                             ({"type": "ryukyoku"}, {"type": "ryukyoku", "actor": 0})):
            with self.subTest(move=move):
                self.config["actions"] = [move]
                agent = self.make()
                action = Action(native)
                self.assertIs(agent.act(initial([action]), Context(0, 2026)), action)
                agent.close()

    def test_malformed_worker_responses_fail_and_close(self):
        for kind in ("wrong_id", "extra", "bad_json", "error", "exit"):
            with self.subTest(kind=kind):
                self.config = {"actions": [DISCARD], kind: True}
                agent = self.make()
                with self.assertRaises(MortalError):
                    agent.act(initial(), Context(0, 2026))
                self.assertIsNone(agent._process)

    def test_timeout_closes_worker(self):
        self.config["sleep"] = 1
        agent = self.make(timeout_seconds=0.05)
        with self.assertRaises(TimeoutError):
            agent.act(initial(), Context(0, 2026))
        self.assertIsNone(agent._process)

    def test_partial_empty_duplicate_history_rejected(self):
        agent = self.make()
        obs = initial()
        obs.events = obs.events[1:]
        with self.assertRaisesRegex(MortalError, "partial"):
            agent.act(obs, Context(0, 2026))
        agent = self.make()
        obs.events = []
        with self.assertRaisesRegex(MortalError, "nonempty"):
            agent.act(obs, Context(0, 2026))
        agent = self.make()
        agent.act(initial(), Context(0, 2026))
        with self.assertRaisesRegex(MortalError, "Duplicate"):
            agent.act(initial(), Context(0, 2026))

    def test_artifact_mutation_detected_before_inference(self):
        agent = self.make()
        (self.source / "weights/s42-inference.pth").write_text("modified weights")
        with self.assertRaisesRegex(ValueError, "changed"):
            agent.act(initial(), Context(0, 2026))

    def test_manifest_mutation_detected(self):
        agent = self.make()
        self.manifest_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            agent.act(initial(), Context(0, 2026))

    def test_dependency_mutation_rejected_at_next_reset(self):
        agent = self.make()
        self.dependency.write_text("# changed package, same installed version\n")
        with self.assertRaisesRegex(ValueError, "dependency files changed"):
            agent.reset(Context(0, 2027))
        self.assertIsNone(agent._process)

    def test_dependency_mutation_rejected_at_game_close(self):
        agent = self.make()
        agent.act(initial(), Context(0, 2026))
        self.dependency.write_text("# changed during game\n")
        with self.assertRaisesRegex(ValueError, "dependency files changed"):
            agent.close()
        self.assertIsNone(agent._process)

    def test_context_and_rules_are_explicit(self):
        agent = self.make()
        with self.assertRaisesRegex(MortalError, "context"):
            agent.act(initial(), Context(0, 2027))
        with self.assertRaisesRegex(ValueError, "half-game"):
            agent.reset(Context(0, 2026, Rules(game_mode="4p-red-east")))

    def test_manifest_path_cannot_escape(self):
        agent = self.make()
        with self.assertRaises(ValueError):
            agent._resolve("../outside")


if __name__ == "__main__":
    unittest.main()
