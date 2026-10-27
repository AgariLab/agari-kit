import json
import base64
import unittest

from agari_kit.agent import Context, RandomAgent, Rules, TsumogiriAgent, ensure_legal


class Action:
    def __init__(self, event):
        self.event = event
        self.tile = 16 if event.get("pai") == "5mr" else 17

    def to_dict(self):
        return self.event

    def to_mjai(self):
        return json.dumps(self.event)


class Observation:
    player_id = 0

    def __init__(self, actions, events=()):
        self.actions = [Action(action) for action in actions]
        self.events = list(events)

    def legal_actions(self):
        return self.actions

    def serialize_to_base64(self):
        drawn = next((action.tile for action in self.actions if action.event.get("tsumogiri")), None)
        return base64.b64encode(json.dumps({"drawn_tile": drawn, "forced_tedashi": False}).encode()).decode()

    def select_action_from_mjai(self, event):
        for action in self.actions:
            if all(action.event.get(key) == value for key, value in event.items() if key != "actor"):
                return action
        return None


class AgentTests(unittest.TestCase):
    def test_seeded_random_reproducible_and_legal(self):
        obs = Observation([{"type": "dahai", "pai": tile} for tile in ("1m", "2m", "5mr")])
        ctx = Context(0, 42)
        agent = RandomAgent()
        traces = []
        for _ in range(2):
            agent.reset(ctx)
            traces.append([ensure_legal(agent.act(obs, ctx), obs).to_dict() for _ in range(20)])
        self.assertEqual(*traces)
        with self.assertRaises(ValueError):
            ensure_legal(Action({"type": "dahai", "pai": "9s"}), obs)

    def test_tsumogiri_preserves_red_five_and_takes_wins(self):
        ctx = Context(0, 0)
        agent = TsumogiriAgent()
        obs = Observation([{"type": "dahai", "pai": "5m", "tsumogiri": False},
                           {"type": "dahai", "pai": "5mr", "tsumogiri": True}],
                          [{"type": "tsumo", "actor": 0, "pai": "5mr"}])
        self.assertEqual(agent.act(obs, ctx).event["pai"], "5mr")
        obs.actions.append(Action({"type": "hora"}))
        self.assertEqual(agent.act(obs, ctx).event["type"], "hora")

    def test_tsumogiri_declines_calls_and_forced_discard_stays_legal(self):
        ctx = Context(0, 0)
        agent = TsumogiriAgent()
        obs = Observation([{"type": "pon"}, {"type": "none"}])
        self.assertEqual(agent.act(obs, ctx).event["type"], "none")
        obs = Observation([{"type": "dahai", "pai": "1m"}])
        ensure_legal(agent.act(obs, ctx), obs)

    def test_rule_and_seat_configuration_rejects_silent_drift(self):
        with self.assertRaises(ValueError):
            Rules(game_mode="3p-red-half")
        with self.assertRaises(ValueError):
            Context(4, 0)


if __name__ == "__main__":
    unittest.main()
