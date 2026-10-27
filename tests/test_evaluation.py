import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agari_kit.evaluation import evaluate, schedule, summarize


class OneDecisionEnvironment:
    def __init__(self, seed):
        self.finished = False
        self.mjai_log = [{"type": "start_game"}, {"type": "end_game"}]

    def reset(self):
        return {seat: object() for seat in range(4)}

    def done(self):
        return self.finished

    def step(self, actions):
        self.finished = True
        return {}

    def scores(self):
        return [40000, 30000, 20000, 10000]


class EvaluationTests(unittest.TestCase):
    def test_seed_groups_rotate_every_agent_through_every_seat(self):
        rows = list(schedule([1, 2], ["a", "b", "c", "d"]))
        self.assertEqual(len(rows), 8)
        for seed in (1, 2):
            for seat in range(4):
                self.assertEqual({agents[seat] for s, _, agents in rows if s == seed}, set("abcd"))

    def test_cluster_bootstrap_and_incomplete_group_rejection(self):
        rows = []
        for seed in (1, 2, 3):
            for rotation in range(4):
                rows.append({"seed": seed, "rotation": rotation,
                             "ranks": [1, 2, 3, 4], "scores": [40000, 30000, 20000, 10000]})
        report = summarize(rows, bootstrap_samples=100)
        self.assertEqual(report["candidate"]["mean_rank"], 2.5)
        self.assertEqual(report["candidate"]["rank_rates"], [.25] * 4)
        self.assertEqual(report["ci95"]["mean_rank"], [2.5, 2.5])
        self.assertEqual(report["ci95"]["rank_1_rate"], [.25, .25])
        self.assertFalse(report["promotion_sample_size_met"])
        self.assertIsNone(summarize(rows[:4], bootstrap_samples=100)["ci95"]["mean_rank"])
        with self.assertRaises(ValueError):
            summarize(rows[:-1])

    def test_invalid_seed_lists(self):
        for seeds in ([], [0, 0], [-1], [2**64]):
            with self.subTest(seeds=seeds), self.assertRaises(ValueError):
                list(schedule(seeds, ["random"] * 4))

    def fake_runtime(self, stack, make_agent):
        stack.enter_context(patch("agari_kit.evaluation.make_agent", side_effect=make_agent))
        stack.enter_context(patch("agari_kit.evaluation.new_environment", side_effect=OneDecisionEnvironment))
        stack.enter_context(patch("agari_kit.evaluation.ensure_legal", side_effect=lambda action, obs: action))
        stack.enter_context(patch("agari_kit.engine.require_engine", return_value="0.4.10"))

    def test_candidate_latency_excludes_opponents_and_tracks_rotated_seat(self):
        clock = [0.0]

        class TimedAgent:
            def __init__(self, name):
                self.milliseconds = 1 if name == "efficiency" else 10

            def reset(self, context):
                pass

            def act(self, observation, context):
                clock[0] += self.milliseconds / 1000
                return object()

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            self.fake_runtime(stack, TimedAgent)
            stack.enter_context(patch("agari_kit.evaluation.time.perf_counter", side_effect=lambda: clock[0]))
            path = Path(temporary) / "evaluation"
            report = evaluate([0], ["efficiency", "random", "random", "random"], path)
            manifest = json.loads((path / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["lineup_provenance"][0], {"kind": "builtin", "name": "efficiency"})
            self.assertEqual(report["candidate_decisions"], 4)
            self.assertAlmostEqual(report["candidate_latency_ms_p95"], 1)
            self.assertAlmostEqual(report["candidate_latency_ms_mean"], 1)
            self.assertEqual(report["decisions"], 16)
            for row in map(json.loads, (path / "games.jsonl").read_text().splitlines()):
                self.assertEqual(row["candidate_decisions"], 1)
                self.assertAlmostEqual(row["candidate_latency_ms_max"], 1)
                self.assertAlmostEqual(row["latency_ms_max"], 10)

    def test_artifact_or_source_change_during_load_stops_before_play(self):
        for changed in ("artifact", "source"):
            calls = [0]

            class Agent:
                def reset(self, context):
                    raise AssertionError("Changed provenance must stop before playing")

            def load(name):
                calls[0] += 1
                return Agent()

            def provenance(name):
                return {"name": name, "sha256": "changed" if changed == "artifact" and calls[0] > 4 else "initial"}

            def source():
                return {"source_sha256": "changed" if changed == "source" and calls[0] > 4 else "initial"}

            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                self.fake_runtime(stack, load)
                stack.enter_context(patch("agari_kit.evaluation.agent_provenance", side_effect=provenance))
                stack.enter_context(patch("agari_kit.evaluation.source_fingerprint", side_effect=source))
                path = Path(temporary) / "evaluation"
                with self.assertRaisesRegex(ValueError, "artifacts or source changed"):
                    evaluate([0], ["efficiency", "random", "random", "random"], path)
                manifest = json.loads((path / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["games_completed"], 0)
                self.assertFalse((path / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
