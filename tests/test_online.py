import asyncio
import json
import base64
import time
import unittest

from agari_kit.connect import drive_connection
from agari_kit.online import OnlineSession, ProtocolError, Request, response_for_action


def request(number=1, *, possible=None, budget=2000, observation="fixture-observation"):
    return {"type": "request_action", "request_id": number,
            "time": {"grace_ms": budget, "bank_ms": 0, "deadline_ms": budget},
            "possible_actions": [{"type": "dahai", "pai": "5mr"}] if possible is None else possible,
            "observation": observation}


class Runner:
    def __init__(self, action=None, delay=0):
        self.action = action or {"type": "dahai", "actor": 0, "pai": "5mr", "tsumogiri": True}
        self.delay = delay
        self.calls = []
        self.closed = False

    async def decide(self, item, context):
        self.calls.append((item, context))
        await asyncio.sleep(self.delay)
        return self.action

    async def close(self):
        self.closed = True


class Socket:
    def __init__(self, messages, *, fail_send=False):
        self.messages = iter(messages)
        self.sent = []
        self.fail_send = fail_send

    async def recv(self):
        await asyncio.sleep(0)
        try:
            return next(self.messages)
        except StopIteration:
            raise ConnectionError("remote error body could include a secret")

    async def send(self, raw):
        if self.fail_send:
            raise ConnectionError("Authorization: Bearer DO-NOT-LOG-THIS")
        self.sent.append(json.loads(raw))


class OnlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records = []
        self.runner = Runner()
        self.session = OnlineSession(mode="ranked", runner=self.runner, emit=self.records.append,
                                     reserve_ms=0)
        await self.session.handle({"type": "start_game", "id": 0})

    async def test_only_unique_request_observations_reach_agent(self):
        for event in ({"type": "tsumo", "actor": 0, "pai": "5mr"},
                      {"type": "start_kyoku", "private_extension": "secret"},
                      {"type": "new_server_event", "token": "secret"}):
            self.assertIsNone(await self.session.handle(event))
        first = await self.session.handle(request())
        self.assertEqual(first["request_id"], 1)
        self.assertEqual(first["pai"], "5mr")
        self.assertIsNone(await self.session.handle(request()))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.runner.calls[0][0].observation, "fixture-observation")
        self.assertEqual(self.session.counts["duplicate_requests"], 1)
        self.assertEqual(self.session.counts["unknown_messages"], 1)
        self.assertNotIn("secret", json.dumps(self.records))

    async def test_conflicting_duplicate_and_wrong_actor_rejected(self):
        await self.session.handle(request())
        with self.assertRaisesRegex(ProtocolError, "reused"):
            await self.session.handle(request(observation="different"))
        self.runner.action["actor"] = 1
        with self.assertRaisesRegex(ProtocolError, "wrong_actor"):
            await self.session.handle(request(2))

    async def test_stale_request_does_not_replay_events(self):
        await self.session.handle(request(42))
        self.assertIsNone(await self.session.handle(request(41)))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.session.counts["stale_requests"], 1)

    async def test_timeout_closes_worker_and_does_not_restart_event_state(self):
        self.runner.delay = .2
        self.assertIsNone(await self.session.handle(request(budget=10)))
        self.assertTrue(self.runner.closed)
        self.assertTrue(self.session.degraded)
        self.assertIsNone(await self.session.handle(request(2)))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.session.counts["local_timeouts"], 1)
        self.assertEqual(self.session.counts["suppressed_after_timeout"], 1)

    async def test_received_timestamp_includes_queue_delay(self):
        self.assertIsNone(await self.session.handle(request(budget=100), time.monotonic() - 1))
        self.assertEqual(len(self.runner.calls), 0)
        self.assertEqual(self.session.counts["local_timeouts"], 1)

    async def test_never_substitutes_an_unlisted_action_or_red_five(self):
        with self.assertRaisesRegex(ProtocolError, "illegal_action"):
            await self.session.handle(request(possible=[{"type": "dahai", "pai": "5m"}]))

    async def test_ack_records_verdict_and_numeric_timing_without_remote_payload(self):
        await self.session.handle(request())
        ack = {"type": "action_ack", "request_id": 1, "status": "rejected", "elapsed_ms": 5,
               "reason": "secret", "attempted": {"Authorization": "secret"}, "bank_ms": "secret"}
        await self.session.handle(ack)
        await self.session.handle(ack)
        self.assertEqual(self.session.counts["ack_rejected"], 1)
        self.assertEqual(self.session.counts["duplicate_acks"], 1)
        self.assertNotIn("secret", json.dumps(self.records))
        self.assertEqual(self.records[-1]["elapsed_ms"], 5)

    async def test_ack_cannot_refer_to_a_gap_in_request_numbers(self):
        await self.session.handle(request(1))
        await self.session.handle(request(3))
        with self.assertRaisesRegex(ProtocolError, "unknown_request"):
            await self.session.handle({"type": "action_ack", "request_id": 2, "status": "accepted"})

    async def test_validation_requires_verdict_after_end_game(self):
        session = OnlineSession(mode="validate", runner=self.runner, emit=self.records.append)
        await session.handle({"type": "start_game", "id": 0})
        await session.handle({"type": "end_game", "scores": [25000] * 4})
        self.assertFalse(session.done)
        self.assertEqual(session.report()["status"], "validation_unconfirmed")
        await session.handle({"type": "validation_result", "passed": True})
        self.assertTrue(session.done)
        self.assertTrue(session.report()["validation_passed"])

    async def test_two_stage_reach_has_two_distinct_requests(self):
        self.runner.action = {"type": "reach", "actor": 0}
        reach = await self.session.handle(request(possible=[{"type": "reach", "actor": 0}]))
        self.runner.action = {"type": "dahai", "actor": 0, "pai": "5mr", "tsumogiri": False}
        discard = await self.session.handle(request(2))
        self.assertEqual([reach["type"], discard["type"]], ["reach", "dahai"])
        self.assertEqual([reach["request_id"], discard["request_id"]], [1, 2])

    async def test_transport_disconnect_is_terminal_and_secrets_are_not_logged(self):
        socket = Socket([json.dumps(request())], fail_send=True)
        report = await drive_connection(socket, self.session)
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["failure"], "client_error")
        self.assertTrue(self.runner.closed)
        self.assertNotIn("DO-NOT-LOG-THIS", json.dumps([report, self.records]))

    async def test_transport_replies_once_and_collects_validation_verdict(self):
        session = OnlineSession(mode="validate", runner=self.runner, emit=self.records.append)
        events = [b"binary ignored", json.dumps({"type": "start_game", "id": 0}),
                  json.dumps(request()), json.dumps(request()),
                  json.dumps({"type": "action_ack", "request_id": 1, "status": "accepted"}),
                  json.dumps({"type": "end_game", "scores": [25000] * 4}),
                  json.dumps({"type": "validation_result", "passed": True})]
        socket = Socket(events)
        report = await drive_connection(socket, session)
        self.assertEqual(len(socket.sent), 1)
        self.assertEqual(report["status"], "complete")
        self.assertTrue(report["validation_passed"])
        self.assertEqual(report["counts"]["ack_accepted"], 1)

    async def test_no_pass_is_invented_for_an_empty_or_malformed_legal_list(self):
        for possible in ([], [{"type": "unknown"}], [{"type": "dahai", "pai": "?"}]):
            with self.subTest(possible=possible), self.assertRaises(ProtocolError):
                await self.session.handle(request(possible=possible))
        self.assertEqual(len(self.runner.calls), 0)


class EnvelopeTests(unittest.TestCase):
    def test_budget_and_id_validation(self):
        for change in ({"request_id": True}, {"request_id": -1}, {"time": None},
                       {"observation": ""}, {"time": {"deadline_ms": -1, "grace_ms": 10, "bank_ms": 0}}):
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                Request.parse(dict(request(), **change), time.monotonic(), 100)


class Action:
    def __init__(self, fields):
        self.fields = fields
        self.tile = 16 if fields.get("pai") == "5mr" else 17

    def to_dict(self):
        return self.fields

    def to_mjai(self):
        return json.dumps({key: value for key, value in self.fields.items() if key != "tsumogiri"})


class Observation:
    player_id = 0

    def __init__(self, actions, events=()):
        self.actions, self.events = [Action(action) for action in actions], list(events)

    def legal_actions(self):
        return self.actions

    def serialize_to_base64(self):
        drawn = next((action.tile for action in self.actions if action.fields.get("tsumogiri")), None)
        return base64.b64encode(json.dumps({"drawn_tile": drawn, "forced_tedashi": False}).encode()).decode()

    def select_action_from_mjai(self, event):
        for action in self.actions:
            if all(action.fields.get(key) == value for key, value in event.items() if key not in ("actor", "target")):
                return action
        return None


class NativeMappingTests(unittest.TestCase):
    def test_red_draw_preserves_tsumogiri(self):
        obs = Observation([{"type": "dahai", "pai": "5mr", "tsumogiri": True}])
        response = response_for_action(obs.actions[0], obs, [{"type": "dahai", "pai": "5mr"}], 0)
        self.assertTrue(response["tsumogiri"])
        self.assertEqual(response["pai"], "5mr")

    def test_call_target_comes_from_player_observation(self):
        call = {"type": "pon", "pai": "5p", "consumed": ["5p", "5pr"]}
        obs = Observation([call], [{"type": "dahai", "actor": 2, "pai": "5p"}])
        response = response_for_action(obs.actions[0], obs, [call], 0)
        self.assertEqual(response["target"], 2)
        self.assertEqual(response["consumed"], ["5p", "5pr"])


if __name__ == "__main__":
    unittest.main()
