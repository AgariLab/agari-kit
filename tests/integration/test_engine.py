"""Run explicitly with installed pinned dependencies; absence is a failure, not a skip."""

import tempfile
import unittest
import json
from pathlib import Path

from agari_kit.agent import Context, RandomAgent, TsumogiriAgent, action_key, ensure_legal, make_agent
from agari_kit.engine import new_environment, require_engine
from agari_kit.evaluation import evaluate
from agari_kit.online import OnlineSession, clean_action, response_for_action
from agari_kit.worker import ProcessRunner


class EngineIntegrationTests(unittest.TestCase):
    def test_serialization_roundtrip_and_native_action_mapping(self):
        require_engine()
        from riichienv import Observation
        env = new_environment(17)
        for seat, obs in env.reset().items():
            restored = Observation.deserialize_from_base64(obs.serialize_to_base64())
            self.assertEqual(obs.to_dict(), restored.to_dict())
            action = TsumogiriAgent().act(restored, Context(seat, 17))
            self.assertIn(action_key(action), {action_key(a) for a in obs.legal_actions()})

    def test_four_complete_hanchan(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate([0], ["random", "tsumogiri", "random", "tsumogiri"], Path(tmp) / "run")
            self.assertEqual(report["games"], 4)

    def test_online_action_roundtrip_over_complete_games(self):
        for seed in range(2):
            env = new_environment(seed)
            agents = [make_agent(name) for name in ("efficiency", "random", "tsumogiri", "random")]
            for seat, agent in enumerate(agents):
                agent.reset(Context(seat, seed))
            observations = env.reset()
            decisions = 0
            while not env.done():
                actions = {}
                for seat, obs in observations.items():
                    possible = [clean_action(json.loads(action.to_mjai())) for action in obs.legal_actions()]
                    selected = agents[seat].act(obs, Context(seat, seed))
                    reply = response_for_action(selected, obs, possible, seat)
                    actions[seat] = ensure_legal(obs.select_action_from_mjai(reply), obs)
                    decisions += 1
                observations = env.step(actions)
            self.assertGreater(decisions, 100)

    def test_efficiency_does_not_consult_opponent_hands_or_future_annotations(self):
        obs = new_environment(73).reset()[0]

        class AnnotatedObservation:
            def __getattr__(self, name):
                return getattr(obs, name)

            def to_dict(self):
                state = obs.to_dict()
                state["hands"] = [[135] * 14 for _ in range(4)]
                state["future_wall"] = ["must-not-be-read"]
                return state

        agent = make_agent("efficiency")
        context = Context(0, 73)
        self.assertEqual(action_key(agent.act(obs, context)),
                         action_key(agent.act(AnnotatedObservation(), context)))


class OnlineWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_observation_in_persistent_process(self):
        env = new_environment(42)
        obs = env.reset()[0]
        possible = [clean_action(json.loads(action.to_mjai())) for action in obs.legal_actions()]
        message = {"type": "request_action", "request_id": 1,
                   "observation": obs.serialize_to_base64(), "possible_actions": possible,
                   "time": {"grace_ms": 3000, "bank_ms": 0, "deadline_ms": 3000}}
        runner = ProcessRunner("tsumogiri")
        try:
            await runner.start()
            session = OnlineSession(mode="ranked", runner=runner, emit=lambda record: None)
            await session.handle({"type": "start_game", "id": 0})
            reply = await session.handle(message)
            self.assertEqual(reply["request_id"], 1)
            ensure_legal(obs.select_action_from_mjai(reply), obs)
            self.assertIsNone(await session.handle(message))
        finally:
            await runner.close()


if __name__ == "__main__":
    unittest.main()
