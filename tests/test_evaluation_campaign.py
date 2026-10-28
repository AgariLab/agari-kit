import json
import shutil
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agari_kit.evaluation import candidate_diagnostics
from agari_kit.evaluation_campaign import _lock, evaluate_campaign, verify_campaign


LINEUP = ["efficiency", "random", "random", "tsumogiri"]


class CampaignTests(unittest.TestCase):
    def runtime(self, stack, *, fail=None, clock=None):
        calls = Counter()

        class Environment:
            def __init__(self, seed):
                calls[seed] += 1
                if fail is not None and (seed, calls[seed]) == fail:
                    raise RuntimeError("fixture interruption")
                self.finished = False
                self.mjai_log = [{"type": "start_game"}, {"type": "start_kyoku"},
                                 {"type": "hora", "actor": 0, "target": 0},
                                 {"type": "end_kyoku"}, {"type": "end_game"}]

            def reset(self):
                return {seat: object() for seat in range(4)}

            def done(self):
                return self.finished

            def step(self, actions):
                self.finished = True
                return {}

            def scores(self):
                return [40000, 30000, 20000, 10000]

        class Agent:
            def reset(self, context):
                pass

            def act(self, observation, context):
                if clock is not None:
                    clock[0] += 1
                return object()

        stack.enter_context(patch("agari_kit.agent.make_agent", side_effect=lambda name: Agent()))
        stack.enter_context(patch("agari_kit.evaluation.new_environment", side_effect=Environment))
        stack.enter_context(patch("agari_kit.evaluation.ensure_legal", side_effect=lambda action, obs: action))
        stack.enter_context(patch("agari_kit.engine.require_engine", return_value="0.4.10"))
        for module in ("evaluation", "evaluation_campaign"):
            stack.enter_context(patch(f"agari_kit.{module}.engine_identity",
                                      return_value={"version": "0.4.10", "binary_sha256": "fixture"}))
            stack.enter_context(patch(f"agari_kit.{module}.source_fingerprint",
                                      return_value={"source_sha256": "fixture"}))
        if clock is not None:
            stack.enter_context(patch("agari_kit.evaluation_campaign.time.monotonic", side_effect=lambda: clock[0]))
        return calls

    def test_resume_keeps_failed_attempt_and_repeats_entire_incomplete_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with ExitStack() as stack:
                self.runtime(stack, fail=(11, 2))
                with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                    evaluate_campaign([10, 11], LINEUP, output)
            original = {str(path.relative_to(output)): path.read_bytes()
                        for path in output.glob("groups/*/*/*") if path.is_file()}
            with ExitStack() as stack:
                calls = self.runtime(stack)
                report = evaluate_campaign([10, 11], LINEUP, output, resume=True)
                self.assertEqual(calls, {11: 4})
                self.assertEqual(report["games"], 8)
                self.assertEqual(verify_campaign(output), report)
            for relative, content in original.items():
                self.assertEqual((output / relative).read_bytes(), content)
            failed = output / "groups/seed-00000000000000000011/attempt-000001"
            self.assertEqual(json.loads((failed / "manifest.json").read_bytes())["games_completed"], 1)
            self.assertFalse((failed / "receipt.json").exists())
            self.assertEqual(json.loads((failed / "failure.json").read_bytes())["error_type"], "RuntimeError")
            self.assertTrue((failed.parent / "attempt-000002/receipt.json").exists())
            (failed / "games.jsonl").write_bytes((failed / "games.jsonl").read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "Failed evaluation attempt hash"):
                verify_campaign(output)
            shutil.rmtree(failed)
            with self.assertRaisesRegex(ValueError, "Merged evaluation hash"):
                verify_campaign(output)

    def test_shards_require_exact_complete_union_and_repeated_merge_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            calls = self.runtime(stack)
            output = Path(temporary) / "run"
            result = evaluate_campaign([10, 11, 12], LINEUP, output, shard_seed_count=2, shard_index=0)
            self.assertEqual(result["status"], "shard_complete")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                evaluate_campaign([10, 11, 12], LINEUP, output, shard_seed_count=2, resume=True, merge_only=True)
            evaluate_campaign([10, 11, 12], LINEUP, output, shard_seed_count=2, shard_index=1, resume=True)
            report = evaluate_campaign([10, 11, 12], LINEUP, output, shard_seed_count=2, resume=True, merge_only=True)
            self.assertEqual(report["games"], 12)
            self.assertEqual(calls, {10: 4, 11: 4, 12: 4})
            before = (output / "completion.json").stat().st_mtime_ns
            evaluate_campaign([10, 11, 12], LINEUP, output, shard_seed_count=2, resume=True, merge_only=True)
            self.assertEqual((output / "completion.json").stat().st_mtime_ns, before)
            first = output / "groups/seed-00000000000000000010/attempt-000001"
            shutil.copytree(first, first.parent / "attempt-000002")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                verify_campaign(output)

    def test_rejects_changed_runtime_and_tampered_completed_group(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            self.runtime(stack)
            output = Path(temporary) / "run"
            evaluate_campaign([10], LINEUP, output)
            with patch("agari_kit.evaluation_campaign.engine_identity", return_value={"binary_sha256": "changed"}):
                with self.assertRaisesRegex(ValueError, "identity differs"):
                    evaluate_campaign([10], LINEUP, output, resume=True)
            games = output / "groups/seed-00000000000000000010/attempt-000001/games.jsonl"
            games.write_bytes(games.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "receipt hash"):
                verify_campaign(output)

    def test_wall_budget_pauses_only_at_group_boundary_then_resumes(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            clock = [0.0]
            calls = self.runtime(stack, clock=clock)
            output = Path(temporary) / "run"
            result = evaluate_campaign([10, 11], LINEUP, output, max_wall_seconds=10)
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["games"], 4)
            self.assertEqual(calls, {10: 4})
            self.assertFalse((output / "completion.json").exists())
            report = evaluate_campaign([10, 11], LINEUP, output, resume=True, max_wall_seconds=10)
            self.assertEqual(report["games"], 8)
            self.assertEqual(calls, {10: 4, 11: 4})

    def test_lock_prevents_a_second_writer_and_invalid_shard_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            self.runtime(stack)
            output = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                evaluate_campaign([10], LINEUP, output, shard_seed_count=0, shard_index=0)
            self.assertFalse(output.exists())
            evaluate_campaign([10], LINEUP, output)
            with _lock(output / "shard-000000.lock"):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    evaluate_campaign([10], LINEUP, output, resume=True)


class DiagnosticTests(unittest.TestCase):
    def test_candidate_seat_riichi_open_hand_and_multiple_ron_semantics(self):
        events = [
            {"type": "start_kyoku"}, {"type": "pon", "actor": 1}, {"type": "pon", "actor": 2},
            {"type": "kakan", "actor": 2}, {"type": "hora", "actor": 0, "target": 2},
            {"type": "hora", "actor": 3, "target": 2}, {"type": "end_kyoku"},
            {"type": "start_kyoku"}, {"type": "ankan", "actor": 2}, {"type": "reach", "actor": 2},
            {"type": "reach_accepted", "actor": 2}, {"type": "hora", "actor": 2, "target": 2},
            {"type": "end_kyoku"}, {"type": "start_kyoku"}, {"type": "reach", "actor": 2},
            {"type": "hora", "actor": 1, "target": 2}, {"type": "end_kyoku"}, {"type": "end_game"}]
        result = candidate_diagnostics(events, 2)
        self.assertEqual(result["hands"], 3)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["tsumo_wins"], 1)
        self.assertEqual(result["deal_ins"], 2)
        self.assertEqual(result["deal_in_payments"], 3)
        self.assertEqual(result["open_hands"], 1)
        self.assertEqual(result["meld_events"], 2)
        self.assertEqual(result["kan_events"], 2)
        self.assertEqual(result["riichi_hands"], 1)
        self.assertEqual(result["riichi_declarations"], 2)
        self.assertEqual(result["win_rate"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
