"""Campaign supervision tests use local fixtures and never open a socket."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_online_campaign.py"
SPEC = importlib.util.spec_from_file_location("run_online_campaign", SCRIPT)
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)

IDENTITY = {
    "agent_provenance": {"kind": "builtin", "name": "fixture"},
    "engine": {"name": "riichienv", "version": "0.4.10"},
    "code": {"source_sha256": "1" * 64},
    "launcher_sha256": "2" * 64,
}
SECRET = "campaign-fixture-secret-never-log"
BAD_COUNTS = (
    "local_timeouts", "send_timeouts", "ack_rejected", "ack_unparseable",
    "ack_defaulted", "ack_stale", "unknown_ack_status", "suppressed_after_timeout",
)


def healthy_report():
    return {
        "mode": "ranked", "status": "complete", "seat": 2,
        "scores": [26000, 22000, 28000, 24000], "validation_passed": None,
        "degraded": False,
        "counts": {"requests": 4, "replies_prepared": 4, "replies_sent": 4,
                   "ack_accepted": 4},
    }


def write_game(output, report):
    """Reproduce connect_games' local outputs without its networking code."""
    output.mkdir(parents=True, exist_ok=False)
    complete = report["status"] == "complete"
    summary = {
        "games_requested": 1, "games_completed": int(complete),
        "validation_passed": None, "degraded_games": int(report["degraded"]),
        "counts": dict(report["counts"]),
    }
    (output / "games.jsonl").write_text(json.dumps(dict(report, game_index=0)) + "\n")
    (output / "report.json").write_text(json.dumps(summary) + "\n")
    (output / "manifest.json").write_text(json.dumps({
        "status": "complete" if complete else "incomplete", **IDENTITY,
    }) + "\n")
    events = [
        {"event": "start_game", "seat": report["seat"], "game_index": 0},
        {"event": "decision", "request_id": 1, "latency_ms": 10,
         "action_type": "dahai", "game_index": 0},
        {"event": "decision", "request_id": 2, "latency_ms": 30,
         "action_type": "none", "game_index": 0},
    ]
    if complete:
        events.append({"event": "end_game", "scores": report["scores"], "game_index": 0})
    (output / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    return summary


class CampaignTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token_file = self.root / "fixture-token"
        self.token_file.write_text(SECRET + "\n")
        self.output = self.root / "runs" / "campaign"
        self.identity = patch.object(campaign, "identity_snapshot", return_value=copy.deepcopy(IDENTITY))
        self.identity.start()
        self.addCleanup(self.identity.stop)

    async def run_campaign(self, **changes):
        options = {"agent_name": "fixture", "token_file": self.token_file,
                   "games": 3, "output": self.output}
        options.update(changes)
        return await campaign.run_campaign(**options)

    def rows(self):
        return [json.loads(line) for line in (self.output / "games.jsonl").read_text().splitlines()]

    def persisted_summary(self):
        return json.loads((self.output / "summary.json").read_text())

    async def test_healthy_then_unhealthy_preserves_both_and_stops(self):
        reports = [healthy_report(), healthy_report()]
        reports[1]["counts"]["ack_rejected"] = 1
        reports[1]["counts"]["ack_accepted"] = 3

        async def connect(**kwargs):
            self.assertEqual(kwargs["mode"], "ranked")
            self.assertEqual(kwargs["games"], 1)
            self.assertEqual(kwargs["token"], SECRET)
            return write_game(kwargs["output"], reports[connector.await_count - 1])

        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
            summary = await self.run_campaign()
        self.assertEqual(connector.await_count, 2)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["games_attempted"], 2)
        self.assertEqual(summary["games_completed"], 2)
        self.assertEqual(summary["healthy_games"], 1)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue((self.output / "game-000001" / "games.jsonl").is_file())
        self.assertTrue((self.output / "game-000002" / "games.jsonl").is_file())
        self.assertFalse((self.output / "game-000003").exists())
        self.assertEqual(self.persisted_summary(), summary)
        self.assertEqual(json.loads((self.output / "manifest.json").read_text())["status"], "stopped")

    async def test_disconnect_is_retained_and_stops(self):
        report = healthy_report()
        report.update(status="interrupted", scores=None, failure="connection_closed")

        async def connect(**kwargs):
            return write_game(kwargs["output"], report)

        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
            summary = await self.run_campaign()
        self.assertEqual(connector.await_count, 1)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["games_attempted"], 1)
        self.assertEqual(summary["games_completed"], 0)
        self.assertEqual(summary["healthy_games"], 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse((self.output / "game-000002").exists())

    async def test_every_bad_counter_degradation_and_missing_receipt_stop(self):
        variants = []
        for key in BAD_COUNTS:
            report = healthy_report()
            report["counts"][key] = 1
            variants.append((key, report))
        report = healthy_report()
        report["degraded"] = True
        variants.append(("degraded", report))
        report = healthy_report()
        report["counts"]["ack_accepted"] = 3
        variants.append(("missing_receipt", report))
        report = healthy_report()
        report["counts"] = {"requests": 0, "replies_sent": 0, "ack_accepted": 0}
        variants.append(("no_decisions", report))
        for label, report in variants:
            with self.subTest(label=label):
                output = self.root / "runs" / label

                async def connect(**kwargs):
                    return write_game(kwargs["output"], report)

                with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
                    summary = await self.run_campaign(output=output)
                self.assertEqual(connector.await_count, 1)
                self.assertEqual(summary["status"], "stopped")
                self.assertEqual(summary["healthy_games"], 0)

    async def test_existing_campaign_is_never_overwritten(self):
        self.output.mkdir(parents=True)
        marker = self.output / "manifest.json"
        marker.write_text("keep existing results\n")
        with patch.object(campaign, "connect_games", new=AsyncMock()) as connector:
            with self.assertRaises(FileExistsError):
                await self.run_campaign()
        connector.assert_not_awaited()
        self.assertEqual(marker.read_text(), "keep existing results\n")
        self.assertEqual(list(self.output.iterdir()), [marker])

    async def test_healthy_completion_keeps_secrets_out_of_campaign_artifacts(self):
        async def connect(**kwargs):
            return write_game(kwargs["output"], healthy_report())

        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
            summary = await self.run_campaign(games=1)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["healthy_games"], 1)
        self.assertEqual(summary["games_completed"], 1)
        self.assertEqual(connector.call_args.kwargs["idle_timeout"], 1800)
        self.assertEqual(connector.call_args.kwargs["reserve_ms"], 100)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text())
                self.assertNotIn(str(self.token_file), path.read_text())

    async def test_exception_text_is_not_persisted_and_status_is_terminal(self):
        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=RuntimeError(SECRET))) as connector:
            summary = await self.run_campaign()
        self.assertEqual(connector.await_count, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertTrue(summary["error"])
        self.assertEqual(self.persisted_summary()["status"], "failed")
        self.assertEqual(json.loads((self.output / "manifest.json").read_text())["status"], "failed")
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text())

    async def test_identity_drift_stops_before_another_connection(self):
        async def connect(**kwargs):
            return write_game(kwargs["output"], healthy_report())

        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
            def identity(_agent):
                result = copy.deepcopy(IDENTITY)
                if connector.await_count:
                    result["code"]["source_sha256"] = "3" * 64
                return result

            with patch.object(campaign, "identity_snapshot", side_effect=identity):
                summary = await self.run_campaign()
        self.assertEqual(connector.await_count, 1)
        self.assertNotIn(summary["status"], ("running", "complete"))
        self.assertTrue(summary["error"])
        self.assertTrue((self.output / "game-000001" / "games.jsonl").exists())
        self.assertFalse((self.output / "game-000002").exists())

    async def test_transport_manifest_must_match_campaign_identity(self):
        async def connect(**kwargs):
            result = write_game(kwargs["output"], healthy_report())
            path = kwargs["output"] / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["engine"] = {"name": "riichienv", "version": "different"}
            path.write_text(json.dumps(manifest) + "\n")
            return result

        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
            summary = await self.run_campaign()
        self.assertEqual(connector.await_count, 1)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["error"], "identity_changed")
        self.assertEqual(summary["games_completed"], 1)
        self.assertEqual(summary["healthy_games"], 0)
        self.assertEqual(len(self.rows()), 1)

    async def test_broken_latency_log_keeps_completed_game_result(self):
        async def connect(**kwargs):
            result = write_game(kwargs["output"], healthy_report())
            (kwargs["output"] / "events.jsonl").write_text("incomplete event\n")
            return result

        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
            summary = await self.run_campaign()
        self.assertEqual(connector.await_count, 1)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["error"], "latency_unavailable")
        self.assertEqual(summary["games_completed"], 1)
        self.assertEqual(self.rows()[0]["scores"], healthy_report()["scores"])

    async def test_cancellation_waits_for_connection_cleanup_and_persists_interruption(self):
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def connect(**kwargs):
            kwargs["output"].mkdir(parents=True, exist_ok=False)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        with patch.object(campaign, "connect_games", new=AsyncMock(side_effect=connect)) as connector:
            task = asyncio.create_task(self.run_campaign())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(connector.await_count, 1)
        self.assertEqual(self.persisted_summary()["status"], "interrupted")
        self.assertEqual(json.loads((self.output / "manifest.json").read_text())["status"], "interrupted")
        self.assertFalse((self.output / "game-000002").exists())


if __name__ == "__main__":
    unittest.main()
