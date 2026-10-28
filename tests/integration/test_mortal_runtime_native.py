"""Opt-in real S42 boundary probes; no model download occurs during discovery.

Set AGARILAB_MORTAL_DESCRIPTOR to the fixed external descriptor. Forced setup
moves make legal native positions; they are fixtures, not teacher training data.
"""
import importlib.util
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import unittest

from agari_kit.agent import Context, Rules, ensure_legal, make_agent
from agari_kit.engine import engine_identity
from agari_kit.plugins import agent_provenance

_spec = importlib.util.spec_from_file_location("mortal_native_fixtures", Path(__file__).with_name("test_rules.py"))
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)


@unittest.skipUnless(os.environ.get("AGARILAB_MORTAL_DESCRIPTOR"), "Explicit external Mortal runtime required")
class MortalRuntimeNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = make_agent("@" + os.environ["AGARILAB_MORTAL_DESCRIPTOR"])
        cls.records = []
        cls.case_status = {}
        cls.provenance = agent_provenance("@" + os.environ["AGARILAB_MORTAL_DESCRIPTOR"])

    @classmethod
    def tearDownClass(cls):
        cls.agent.close()
        after = agent_provenance("@" + os.environ["AGARILAB_MORTAL_DESCRIPTOR"])
        destination = os.environ.get("AGARILAB_MORTAL_PROBE_REPORT")
        if destination:
            with Path(destination).open("x") as stream:
                json.dump({"schema_version": 1,
                           "status": "passed" if len(cls.case_status) == 7 and all(cls.case_status.values()) and after == cls.provenance else "failed",
                           "case_status": cls.case_status, "cases": cls.records, "timings": cls.agent.timings,
                           "teacher_provenance": cls.provenance, "teacher_provenance_after": after,
                           "engine": engine_identity(), "rules": asdict(Rules()),
                           "fixture_sha256": hashlib.sha256(Path(fixtures.__file__).read_bytes()).hexdigest(),
                           "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                           "initialization_seconds": cls.agent.initialization_seconds}, stream, indent=2)
                stream.write("\n")

    def tearDown(self):
        result = self._outcome.result
        self.case_status[self.id().split(".")[-1]] = not any(
            test is self for test, _ in result.failures + result.errors)

    def decide(self, env, seat, *, seed=17):
        native = env.get_observation(seat)
        history = list(env.mjai_log)
        class FullInitialHistory:
            def __getattr__(self, name):
                return getattr(native, name)
            def new_events(self):
                return history
        context = Context(seat, seed)
        self.agent.reset(context)
        selected = ensure_legal(self.agent.act(FullInitialHistory(), context), native)
        self.records.append({"case": self.id().split(".")[-1], "seat": seat,
                             "events": len(history), "selected": json.loads(selected.to_mjai()),
                             "legal_action_types": sorted({str(a.action_type) for a in native.legal_actions()})})
        return selected

    def test_reach_followup_discard(self):
        env = fixtures.position({0: "33346m23477p345s"}, [11], mode="4p-red-half")
        env.step({0: fixtures.action(env, 0, fixtures.ActionType.RIICHI)})
        selected = self.decide(env, 0)
        self.assertEqual(selected.action_type, fixtures.ActionType.DISCARD)
        env.step({0: selected})
        fixtures.pass_claims(env)
        self.assertTrue(env.riichi_declared[0])

    def test_red_five_pon_followup_discard(self):
        env = fixtures.position({1: "05m123456789p33s"}, [18], mode="4p-red-half")
        fixtures.discard(env, 0, 18)
        call = fixtures.action(env, 1, fixtures.ActionType.PON)
        self.assertIn(16, call.consume_tiles)
        env.step({seat: call if seat == 1 else fixtures.action(env, seat, fixtures.ActionType.PASS)
                  for seat in env.active_players})
        self.assertEqual(self.decide(env, 1).action_type, fixtures.ActionType.DISCARD)

    def test_red_five_chi_followup_discard(self):
        env = fixtures.position({1: "04m123456789p33s"}, [8], mode="4p-red-half")
        fixtures.discard(env, 0, 8)
        call = fixtures.action(env, 1, fixtures.ActionType.CHI)
        self.assertIn(16, call.consume_tiles)
        env.step({seat: call if seat == 1 else fixtures.action(env, seat, fixtures.ActionType.PASS)
                  for seat in env.active_players})
        self.assertEqual(self.decide(env, 1).action_type, fixtures.ActionType.DISCARD)

    def test_ankan_dora_rinshan(self):
        env = fixtures.position({0: "111m234p567s789s1z"}, [3], mode="4p-red-half")
        env.step({0: fixtures.action(env, 0, fixtures.ActionType.ANKAN)})
        self.decide(env, 0)

    def test_kakan_delayed_dora_chankan(self):
        env, _ = fixtures.chankan_position(game_mode="4p-red-half")
        self.assertEqual([e["type"] for e in env.mjai_log][-2:], ["kakan", "dora"])
        self.assertEqual(self.decide(env, 1, seed=42).action_type, fixtures.ActionType.RON)

    def test_two_ron_events_then_next_hand(self):
        env = fixtures.position({1: "12m456p789s111z22z", 2: "12m123p456s666z77z"}, [8], mode="4p-red-half")
        fixtures.discard(env, 0, 8)
        env.step({seat: fixtures.action(env, seat, fixtures.ActionType.RON if seat in (1, 2)
                                       else fixtures.ActionType.PASS) for seat in env.active_players})
        self.assertEqual(len([e for e in env.mjai_log if e["type"] == "hora"]), 2)
        self.assertFalse(env.done())
        self.decide(env, env.current_player)

    def test_kyushu_decision_and_new_hand(self):
        env = fixtures.position({0: "19m19p19s123z2223m"}, [12], mode="4p-red-half")
        draw = fixtures.action(env, 0, fixtures.ActionType.KYUSHU_KYUHAI)
        self.decide(env, 0)
        env.step({0: draw})
        self.assertTrue(any(e["type"] == "ryukyoku" for e in env.mjai_log))
        self.decide(env, env.current_player)


if __name__ == "__main__":
    unittest.main()
