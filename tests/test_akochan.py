import base64
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agari_kit.agent import Context, Rules
from agari_kit.opponents.akochan import AkochanAgent, AkochanError


class Action:
    def __init__(self, move, tile=None):
        self.move, self.tile = move, tile

    def to_mjai(self):
        return json.dumps(self.move)


class Observation:
    player_id = 0

    def __init__(self, events, actions, drawn=None):
        self.events = [json.dumps(event) for event in events]
        self.actions, self.drawn = actions, drawn

    def legal_actions(self):
        return self.actions

    def new_events(self):
        return self.events

    def serialize_to_base64(self):
        return base64.b64encode(json.dumps({"drawn_tile": self.drawn}).encode()).decode()


def initial(action=None):
    return Observation([
        {"type": "start_game", "secret_wall": [1, 2]},
        {"type": "start_kyoku", "tehais": [["1m"] * 13 for _ in range(4)], "future": "secret"},
        {"type": "tsumo", "actor": 1, "pai": "9m"},
        {"type": "dahai", "actor": 1, "pai": "9m", "tsumogiri": True},
        {"type": "tsumo", "actor": 0, "pai": "1m"},
    ], [action or Action({"type": "dahai", "actor": 0, "pai": "1m"}, 0)], drawn=0)


class AkochanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        (self.checkout / "params").mkdir()
        self.parameter = self.checkout / "params" / "weights.txt"
        self.parameter.write_text("original parameter")
        self.config = self.checkout / "setup.json"
        self.binary = self.checkout / "fake.py"
        self.binary.write_text(
            f"#!{sys.executable}\n"
            "import json,os,subprocess,sys,time\n"
            "from pathlib import Path\n"
            "config=json.loads(Path(sys.argv[2]).read_text())\n"
            "Path('pid').write_text(str(os.getpid()))\n"
            "Path('pgrp').write_text(str(os.getpgrp()))\n"
            "if config.get('descendant'):\n"
            " child=subprocess.Popen([sys.executable,'-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])\n"
            " Path('child_pid').write_text(str(child.pid))\n"
            "for line in sys.stdin:\n"
            " with open('events.jsonl','a') as log: log.write(line)\n"
            " event=json.loads(line)\n"
            " if event.get('can_act'):\n"
            "  if config.get('exit'):sys.exit(3)\n"
            "  time.sleep(config.get('sleep',0))\n"
            "  if config.get('invalid'): print('invalid json',flush=True)\n"
            "  else: print((json.dumps(config['responses'].pop(0))+'\\n')*(2 if config.get('extra') else 1),end='',flush=True)\n"
        )
        self.binary.chmod(0o755)
        self.config_data = {"responses": [[{"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": True}]]}
        self.manifest_path = self.root / "manifest.json"

    def make(self, **kwargs):
        self.config.write_text(json.dumps(self.config_data))
        files = {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (self.binary, self.config, self.parameter)}
        self.manifest = {"schema_version": 1, "checkout": "checkout", "binary": "checkout/fake.py",
                         "config": "checkout/setup.json", "compiled_threads": 2, "files": files}
        self.manifest_path.write_text(json.dumps(self.manifest))
        agent = AkochanAgent(binary_path=str(self.binary), config_path=str(self.config),
                             manifest_path=str(self.manifest_path), **kwargs)
        self.addCleanup(agent.close)
        agent.reset(Context(0, 2026))
        return agent

    def events(self):
        # Reaching a response guarantees all preceding event writes were read.
        return [json.loads(line) for line in (self.checkout / "events.jsonl").read_text().splitlines()]

    def test_player_boundary_rules_and_only_current_decision_triggers(self):
        agent = self.make()
        action = agent.act(initial(), Context(0, 2026))
        self.assertEqual(action.tile, 0)
        events = self.events()
        self.assertEqual(events[0], {"type": "start_game", "id": 0, "kyoku_first": 0,
                                     "aka_flag": True, "can_act": False})
        self.assertEqual(events[1]["tehais"][1:], [["?"] * 13] * 3)
        self.assertNotIn("future", events[1])
        self.assertEqual(events[2]["pai"], "?")
        self.assertEqual([event["can_act"] for event in events], [False] * 4 + [True])
        self.assertEqual(int((self.checkout / "pgrp").read_text()), agent._process.pid)

    def test_east_rule_mapping_and_reset_closes_process(self):
        agent = self.make()
        agent.act(initial(), Context(0, 2026))
        old = agent._process
        context = Context(0, 2026, Rules(game_mode="4p-red-east"))
        agent.reset(context)
        self.assertIsNotNone(old.poll())
        agent.act(initial(), context)
        self.assertEqual(self.events()[5]["kyoku_first"], 4)

    def test_reach_discard_queue_uses_echo_without_new_inference(self):
        reach = {"type": "reach", "actor": 0}
        discard = {"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": True}
        self.config_data["responses"] = [[reach, discard]]
        agent = self.make()
        self.assertEqual(agent.act(initial(Action(reach)), Context(0, 2026)).move, reach)
        observation = Observation([reach], [Action({key: value for key, value in discard.items()
                                                    if key != "tsumogiri"}, 0)], drawn=0)
        self.assertEqual(agent.act(observation, Context(0, 2026)).tile, 0)
        self.assertIsNone(agent._pending)
        self.assertIsNone(agent._process.poll())

    def test_only_delta_api_events_are_forwarded(self):
        agent = self.make()
        first = initial()
        agent.act(first, Context(0, 2026))
        # A wrapper may retain historical events while exposing a fresh delta.
        class HistoryObservation(Observation):
            def new_events(self):
                return [json.dumps({"type": "ankan", "actor": 1, "consumed": ["9m"] * 4})]
        later = HistoryObservation([json.loads(raw) for raw in first.events], [Action({"type": "none"})])
        self.assertEqual(agent.act(later, Context(0, 2026)).move, {"type": "none"})

    def test_pon_queue_and_competing_claim_discard_pending(self):
        pon = {"type": "pon", "actor": 0, "target": 1, "pai": "5m", "consumed": ["5mr", "5m"]}
        discard = {"type": "dahai", "actor": 0, "pai": "1m", "tsumogiri": False}
        self.config_data["responses"] = [[pon, discard], [{"type": "dahai", "actor": 0,
                                                          "pai": "1m", "tsumogiri": True}]]
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                agent = self.make()
                first = initial()
                first.events[-1] = json.dumps({"type": "dahai", "actor": 1, "pai": "5m"})
                first.actions = [Action({key: value for key, value in pon.items() if key != "target"})]
                self.assertEqual(agent.act(first, Context(0, 2026)).move["type"], "pon")
                next_events = [pon] if accepted else [{"type": "pon", "actor": 2, "target": 1,
                                                     "pai": "5m", "consumed": ["5m", "5m"]},
                                                    {"type": "tsumo", "actor": 0, "pai": "1m"}]
                obs = Observation(next_events, [Action({"type": "dahai", "actor": 0, "pai": "1m"}, 0)],
                                  drawn=None if accepted else 0)
                self.assertEqual(agent.act(obs, Context(0, 2026)).tile, 0)
                agent.close()

    def test_wrong_tsumogiri_red_and_actor_rejected_and_child_reaped(self):
        for bad in ({"tsumogiri": False}, {"pai": "5mr"}, {"actor": 1}, {"tsumogiri": None}):
            with self.subTest(bad=bad):
                self.config_data["responses"] = [[{"type": "dahai", "actor": 0, "pai": "1m",
                                                   "tsumogiri": True, **bad}]]
                agent = self.make()
                with self.assertRaises(RuntimeError):
                    agent.act(initial(), Context(0, 2026))
                self.assertIsNone(agent._process)
                pid = int((self.checkout / "pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_call_target_and_red_consumed_strictly_checked(self):
        agent = self.make()
        events = [{"type": "dahai", "actor": 1, "pai": "5m"}]
        obs = Observation(events, [Action({"type": "pon", "actor": 0, "pai": "5m", "consumed": ["5mr", "5m"]})])
        good = {"type": "pon", "actor": 0, "target": 1, "pai": "5m", "consumed": ["5m", "5mr"]}
        self.assertIs(agent._match(good, obs, events), obs.actions[0])
        for change in ({"target": 2}, {"consumed": ["5m", "5m"]}):
            with self.assertRaises(AkochanError):
                agent._match({**good, **change}, obs, events)

    def test_timeout_respects_context_deadline_and_reaps_child(self):
        self.config_data["sleep"] = 10
        agent = self.make(timeout_seconds=5)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            agent.act(initial(), Context(0, 2026, deadline=started + 0.2))
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNone(agent._process)
        pid = int((self.checkout / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_exit_and_malformed_output_fail_without_substitute(self):
        for key in ("exit", "invalid"):
            with self.subTest(key=key):
                self.config_data[key] = True
                agent = self.make()
                with self.assertRaises(AkochanError):
                    agent.act(initial(), Context(0, 2026))
                self.assertIsNone(agent._process)
                self.config_data.pop(key)

    def test_parameter_or_manifest_change_stops_before_starting(self):
        agent = self.make()
        self.parameter.write_text("altered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            agent.act(initial(), Context(0, 2026))
        self.assertFalse((self.checkout / "pid").exists())
        agent = self.make()
        self.manifest_path.write_text(self.manifest_path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            agent.act(initial(), Context(0, 2026))

    def test_omitted_parameter_and_thread_overrun_rejected(self):
        self.make()
        self.manifest["files"].pop("checkout/params/weights.txt")
        self.manifest_path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "every parameter"):
            AkochanAgent(binary_path=str(self.binary), config_path=str(self.config), manifest_path=str(self.manifest_path))
        with self.assertRaisesRegex(ValueError, "thread limit"):
            self.make(threads=3)

    def test_worker_group_marker_is_validated(self):
        for marker, inherit in ((str(os.getpgrp()), True), ("stale-group", False)):
            with self.subTest(marker=marker), patch.dict(os.environ, AGARILAB_AGENT_PROCESS_GROUP=marker):
                agent = self.make()
                agent.act(initial(), Context(0, 2026))
                group = int((self.checkout / "pgrp").read_text())
                self.assertEqual(group, os.getpgrp() if inherit else agent._process.pid)
                agent.close()
                agent.close()

    def test_zero_deadline_does_not_start_process(self):
        agent = self.make()
        with self.assertRaises(TimeoutError):
            agent.act(initial(), Context(0, 2026, deadline=0))
        self.assertFalse((self.checkout / "pid").exists())

    def test_unsolicited_second_response_is_rejected(self):
        self.config_data["extra"] = True
        agent = self.make()
        with self.assertRaisesRegex(AkochanError, "unsolicited"):
            agent.act(initial(), Context(0, 2026))
        self.assertIsNone(agent._process)

    def test_native_pass_actor_and_kyushu_reason_are_checked(self):
        for move, native in (({"type": "none", "actor": 0}, {"type": "none"}),
                             ({"type": "ryukyoku", "actor": 0, "reason": "kyushukyuhai"},
                              {"type": "ryukyoku", "actor": 0})):
            self.config_data["responses"] = [[move]]
            agent = self.make()
            self.assertEqual(agent.act(initial(Action(native)), Context(0, 2026)).move, native)
            agent.close()

    def test_ankan_ron_window_fails_instead_of_passing_a_legal_win(self):
        agent = self.make()
        obs = initial()
        obs.events[-1] = json.dumps({"type": "ankan", "actor": 1, "consumed": ["1m"] * 4})
        obs.actions = [Action({"type": "none"}), Action({"type": "hora", "actor": 0})]
        with self.assertRaisesRegex(AkochanError, "cannot decide"):
            agent.act(obs, Context(0, 2026))

    def test_standalone_close_kills_descendant_ignoring_term(self):
        self.config_data["descendant"] = True
        agent = self.make()
        agent.act(initial(), Context(0, 2026))
        pid = int((self.checkout / "child_pid").read_text())
        agent.close()
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.02)
        else:
            self.fail("Opponent descendant survived close")


if __name__ == "__main__":
    unittest.main()
