"""Required native-engine boundary test; run in the integration job."""

import importlib.util
import json
import time
import unittest
from pathlib import Path

from agari_kit.agent import Context


# Reuse the fake process fixture without importing a TestCase into this module
# or inheriting its unit tests (both would cause duplicate unittest discovery).
_spec = importlib.util.spec_from_file_location(
    "akochan_unit_support", Path(__file__).parents[1] / "test_akochan.py")
_support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_support)


class NativeAkochanTests(unittest.TestCase):
    setUp = _support.AkochanTests.setUp
    make = _support.AkochanTests.make
    events = _support.AkochanTests.events

    def test_native_chankan_then_delayed_dora_preserves_order_and_legal_ron(self):
        # Reuse the native 136-tile wall fixture rather than synthesize legal
        # actions or directly edit engine state. Its last step must stay intact.
        fixture_path = Path(__file__).parent / "test_rules.py"
        spec = importlib.util.spec_from_file_location("akochan_native_fixtures", fixture_path)
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        observations = {}
        env, _ = fixtures.chankan_position(capture_observations=observations, game_mode="4p-red-half")
        native = observations[1]
        delta = [json.loads(raw) for raw in native.new_events()]
        self.assertEqual([event["type"] for event in delta][-2:], ["kakan", "dora"])
        history = list(env.mjai_log)

        class FirstDecisionWithHistory:
            # Feed the authentic earlier history once, so a newly started pipe
            # has start_game and the hand. Legality remains the native window.
            def __getattr__(self, name):
                return getattr(native, name)

            def new_events(self):
                return history

        self.config_data["responses"] = [[{"type": "hora", "actor": 1, "target": 2, "pai": "7s"}]]
        agent = self.make()
        context = Context(1, 42)
        agent.reset(context)
        selected = agent.act(FirstDecisionWithHistory(), context)
        self.assertEqual(selected.action_type, fixtures.ActionType.RON)
        deadline = time.monotonic() + 1
        while len(self.events()) < len(history) and time.monotonic() < deadline:
            time.sleep(.005)
        wire = self.events()
        self.assertEqual([event["type"] for event in wire], [event["type"] for event in history])
        self.assertEqual([(event["type"], event["can_act"]) for event in wire[-2:]],
                         [("kakan", True), ("dora", False)])
        self.assertEqual(sum(event["can_act"] for event in wire), 1)
        before = env.scores()[1]
        env.step({1: selected})
        self.assertGreater(env.scores()[1], before)


if __name__ == "__main__":
    unittest.main()
