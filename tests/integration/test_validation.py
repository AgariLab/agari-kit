import base64
import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from agari_kit.agent import Context, Rules, make_agent
from agari_kit.engine import new_environment
from agari_kit.mjai import DataError, inspect_game, read_events
from agari_kit.provenance import source_fingerprint
from agari_kit.validation import _check_player_view, validate_mjai


def create_archive(seed=2026):
    names = ["efficiency", "random", "efficiency", "random"]
    agents = [make_agent(name) for name in names]
    for seat, agent in enumerate(agents):
        agent.reset(Context(seat, seed))
    environment = new_environment(seed)
    observations = environment.reset()
    first_observation = json.loads(base64.b64decode(observations[0].serialize_to_base64()))
    trace = [{"type": "agari_trace", "schema_version": 1, "seed": seed,
              "rules": asdict(Rules()), "agents": names, "code": source_fingerprint()}]
    steps = 0
    while not environment.done():
        actions = {seat: agents[seat].act(observation, Context(seat, seed))
                   for seat, observation in observations.items()}
        trace.append({"step": steps, "actions": {str(seat): action.to_dict() for seat, action in actions.items()},
                      "observations_sha256": {str(seat): hashlib.sha256(observation.serialize_to_base64().encode()).hexdigest()
                                              for seat, observation in observations.items()}})
        observations = environment.step(actions)
        steps += 1
    scores = list(environment.scores())
    trace.append({"type": "trace_end", "steps": steps, "scores": scores})
    events = environment.mjai_log
    events[-1] = dict(events[-1], scores=scores)
    return events, trace, first_observation


class ReplayValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events, cls.trace, cls.observation = create_archive()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.mjai = Path(self.temporary.name) / "game.jsonl.gz"
        self.trace_path = Path(self.temporary.name) / "trace.jsonl.gz"
        self.write()

    def write(self, *, events=None, trace=None):
        for path, rows in ((self.mjai, self.events if events is None else events),
                           (self.trace_path, self.trace if trace is None else trace)):
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                stream.writelines(json.dumps(row) + "\n" for row in rows)

    def test_seeded_trace_independently_reproduces_game_and_scores(self):
        receipt = validate_mjai(self.mjai, self.trace_path)
        self.assertEqual(receipt["validation"], "engine-verified")
        self.assertEqual(receipt["sha256"], inspect_game(read_events(self.mjai))["sha256"])
        self.assertEqual(receipt["trace_sha256"], hashlib.sha256(self.trace_path.read_bytes()).hexdigest())
        self.assertEqual(receipt["scores"], self.trace[-1]["scores"])
        self.assertGreater(receipt["decisions"], 100)
        self.assertTrue(all(receipt["checks"].values()))

    def test_missing_trace_never_earns_engine_verification(self):
        receipt = validate_mjai(self.mjai)
        self.assertEqual(receipt["validation"], "structural-only")
        self.assertFalse(receipt["checks"]["action_legality"])
        self.assertFalse(receipt["checks"]["final_scores"])

    def test_illegal_trace_action_is_locatable(self):
        trace = copy.deepcopy(self.trace)
        trace[1]["actions"]["0"]["tile"] = 999
        self.write(trace=trace)
        with self.assertRaisesRegex(DataError, r"trace.jsonl.gz:2: illegal action"):
            validate_mjai(self.mjai, self.trace_path)

    def test_observation_hash_tampering_is_rejected(self):
        trace = copy.deepcopy(self.trace)
        trace[1]["observations_sha256"]["0"] = "0" * 64
        self.write(trace=trace)
        with self.assertRaisesRegex(DataError, "Observation hash mismatch"):
            validate_mjai(self.mjai, self.trace_path)

    def test_corrupted_tile_counts_or_future_draws_fail_replay(self):
        for index, key, value in ((1, "tehais", [["1m"] * 13] * 4),
                                  (2, "pai", "P")):
            events = copy.deepcopy(self.events)
            events[index][key] = value
            self.write(events=events)
            with self.subTest(event=index), self.assertRaisesRegex(DataError, "disagrees"):
                validate_mjai(self.mjai, self.trace_path)

    def test_final_score_and_round_delta_tampering_fail(self):
        events = copy.deepcopy(self.events)
        events[-1]["scores"][0] += 100
        self.write(events=events)
        with self.assertRaisesRegex(DataError, "disagrees"):
            validate_mjai(self.mjai, self.trace_path)
        events = copy.deepcopy(self.events)
        result = next(event for event in events if event["type"] == "hora")
        result["deltas"][0] += 100
        self.write(events=events)
        with self.assertRaisesRegex(DataError, "disagrees"):
            validate_mjai(self.mjai, self.trace_path)

    def test_discard_tsumogiri_flag_is_checked_strictly(self):
        # The upstream MJAI action mapper can fall back to an otherwise legal
        # discard when tsumogiri is inconsistent. Exact execution comparison
        # must still reject that mislabeled record.
        events = copy.deepcopy(self.events)
        discard = next(event for event in events if event["type"] == "dahai")
        discard["tsumogiri"] = not discard["tsumogiri"]
        self.write(events=events)
        with self.assertRaisesRegex(DataError, "disagrees"):
            validate_mjai(self.mjai, self.trace_path)

    def test_truncation_and_forged_footer_fail(self):
        self.write(trace=self.trace[:-1])
        with self.assertRaisesRegex(DataError, "missing trace_end"):
            validate_mjai(self.mjai, self.trace_path)
        self.write(trace=self.trace[:2] + [self.trace[-1]])
        with self.assertRaisesRegex(DataError, "unfinished game"):
            validate_mjai(self.mjai, self.trace_path)
        self.write(events=self.events[:-1])
        with self.assertRaisesRegex(DataError, "Missing end_game"):
            validate_mjai(self.mjai, self.trace_path)

    def test_view_audit_detects_hidden_hands_draws_and_unreviewed_fields(self):
        for change in ("hand", "draw", "field"):
            snapshot = copy.deepcopy(self.observation)
            if change == "hand":
                snapshot["hands"][1] = [0]
            elif change == "draw":
                snapshot["events"].append(json.dumps({"type": "tsumo", "actor": 1, "pai": "1m"}))
            else:
                snapshot["future_wall"] = [0]
            with self.subTest(change=change), self.assertRaises(DataError):
                _check_player_view(snapshot, 0, "test")

    def test_wrong_mode_seed_step_and_action_seats_are_rejected(self):
        cases = []
        trace = copy.deepcopy(self.trace)
        trace[0]["rules"]["game_mode"] = "4p-red-east"
        cases.append(trace)
        trace = copy.deepcopy(self.trace)
        trace[0]["seed"] += 1
        cases.append(trace)
        trace = copy.deepcopy(self.trace)
        trace[1]["step"] = 3
        cases.append(trace)
        trace = copy.deepcopy(self.trace)
        trace[1]["actions"] = {}
        cases.append(trace)
        for trace in cases:
            self.write(trace=trace)
            with self.subTest(case=trace[0]), self.assertRaises(DataError):
                validate_mjai(self.mjai, self.trace_path)


if __name__ == "__main__":
    unittest.main()
