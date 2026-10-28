"""Diagnostic boundary tests: synthetic credentials and loopback only."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import inspect
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from websockets.asyncio import client as ws_client
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidStatus
from websockets.frames import Close, Opcode
from websockets.http11 import Response
from websockets.datastructures import Headers

import test_online_campaign as fixtures


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_online_diagnostic.py"
SPEC = importlib.util.spec_from_file_location("run_online_diagnostic_test", SCRIPT)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)
SECRET = "diagnostic-synthetic-Bearer-Token-never-log"


def quiet_logger():
    logger = logging.Logger("diagnostic.local.fixture")
    logger.disabled = True
    logger.propagate = False
    return logger


def production_kwargs(proxy=True):
    return {"additional_headers": {"Authorization": "Bearer " + SECRET},
            "proxy": proxy, "logger": quiet_logger(), "open_timeout": 15,
            "close_timeout": 2, "max_size": 2**21, "max_queue": 16}


class ClassificationTests(unittest.TestCase):
    def test_close_codes_and_exact_local_timeout_signature(self):
        cases = [
            (Close(1000, SECRET), Close(1000, SECRET), True, False),
            (None, None, None, False),
            (None, Close(1011, "keepalive ping timeout"), None, True),
            (Close(1011, "keepalive ping timeout"), Close(1011, "keepalive ping timeout"), True, False),
            (None, Close(1011, "keepalive ping timeout " + SECRET), None, False),
            (None, Close(1000, "keepalive ping timeout"), None, False),
        ]
        for received, sent, order, timeout in cases:
            with self.subTest(timeout=timeout, order=order):
                facts = diagnostic.close_facts(received, sent, order)
                self.assertEqual(facts["local_keepalive_timeout_observed"], timeout)
                self.assertEqual(facts["received_close_code"], received.code if received else None)
                self.assertEqual(facts["sent_close_code"], sent.code if sent else None)
                self.assertNotIn(SECRET, json.dumps(facts))
                self.assertNotIn("keepalive ping timeout", json.dumps(facts))

    def test_unknown_exception_text_class_name_and_http_bodies_are_not_serialized(self):
        def forbidden_repr(_self):
            raise AssertionError("exception formatting must never run")

        hostile = type(SECRET, (RuntimeError,), {"__str__": forbidden_repr, "__repr__": forbidden_repr})(SECRET)
        hostile.__cause__ = TimeoutError(SECRET)
        facts = diagnostic.exception_facts(hostile)
        self.assertEqual(facts, {"exception_category": "other_error", "cause_category": "timeout"})
        response = Response(403, SECRET, Headers({"Authorization": SECRET}), SECRET.encode())
        facts = diagnostic.exception_facts(InvalidStatus(response))
        self.assertEqual(facts["http_status"], 403)
        self.assertEqual(facts["exception_category"], "handshake_http_status")
        self.assertNotIn(SECRET, json.dumps(facts))
        facts = diagnostic.exception_facts(ConnectionClosedError(None, None))
        self.assertTrue(facts["abnormal_no_close_frames"])
        self.assertIsNone(facts["sent_close_code"])


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "runs" / "fixture"
        self.token_file = self.root / "synthetic-token"
        self.token_file.write_text(SECRET)
        self.original_connect = ws_client.connect
        self.identity = patch.object(diagnostic.campaign, "identity_snapshot", return_value=copy.deepcopy(fixtures.IDENTITY))
        self.identity.start()
        self.addCleanup(self.identity.stop)

    async def run_diagnostic(self, **changes):
        options = {"agent_name": "fixture", "token_file": self.token_file, "output": self.output}
        options.update(changes)
        return await diagnostic.run_diagnostic(**options)

    def artifacts(self):
        return "\n".join(path.read_text() for path in self.output.rglob("*") if path.is_file())

    def rows(self):
        return [json.loads(line) for line in (self.output / "diagnostic" / "events.jsonl").read_text().splitlines()]

    def assert_restored(self):
        self.assertIs(ws_client.connect, self.original_connect)
        self.assertFalse(diagnostic._ACTIVE)

    async def test_multiple_games_are_rejected_before_token_or_output(self):
        with patch.object(diagnostic.campaign, "_load_token") as load:
            for games in (0, 2, 100, True, "1"):
                with self.subTest(games=games), self.assertRaises(ValueError):
                    await self.run_diagnostic(games=games)
        load.assert_not_called()
        self.assertFalse(self.output.exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            diagnostic.parser().parse_args(["--agent", "fixture", "--token-file", "synthetic", "--run", "fixture", "--games", "2"])
        self.assert_restored()

    async def test_old_run_is_untouched(self):
        self.output.mkdir(parents=True)
        marker = self.output / "manifest.json"
        marker.write_text("existing failure must remain")
        with patch.object(diagnostic.campaign, "_load_token") as load, self.assertRaises(FileExistsError):
            await self.run_diagnostic()
        load.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [marker])
        self.assertEqual(marker.read_text(), "existing failure must remain")
        self.assert_restored()

    async def test_source_change_is_rejected_before_launch(self):
        changed = self.root / "changed-script.py"
        changed.write_text("# synthetic replacement\n")
        for name in ("SCRIPT", "SUPERVISOR"):
            with patch.object(diagnostic, name, changed), self.assertRaises(diagnostic.DiagnosticFailure):
                await self.run_diagnostic()
        self.assertFalse(self.output.exists())
        self.assert_restored()

    async def test_worker_failure_retains_supervisor_outputs_and_creates_lazy_diagnostics(self):
        async def failed(**kwargs):
            self.assertTrue(self.output.exists())
            self.assertFalse((self.output / "diagnostic").exists())
            raise RuntimeError(SECRET)

        with patch.object(diagnostic.campaign, "connect_games", new=AsyncMock(side_effect=failed)):
            result = await self.run_diagnostic()
        self.assertEqual(result["status"], "failed")
        details = json.loads((self.output / "diagnostic" / "result.json").read_text())
        self.assertEqual(details["connection_attempts"], 0)
        self.assertEqual(details["campaign_status"], "failed")
        self.assertNotIn(SECRET, self.artifacts())
        self.assert_restored()

    async def test_exclusive_diagnostic_directory_failure_cannot_mark_complete(self):
        async def fixture(**kwargs):
            directory = self.output / "diagnostic"
            directory.mkdir()
            (directory / "marker").write_text("keep failed attempt")
            async with ws_client.connect(diagnostic.RANKED_ENDPOINT, **production_kwargs()):
                self.fail("must fail before networking")

        with patch.object(diagnostic.campaign, "connect_games", new=AsyncMock(side_effect=fixture)):
            with self.assertRaises(diagnostic.DiagnosticFailure):
                await self.run_diagnostic()
        self.assertEqual((self.output / "diagnostic" / "marker").read_text(), "keep failed attempt")
        self.assertFalse((self.output / "diagnostic" / "result.json").exists())
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "failed")
        self.assert_restored()

    async def test_cancellation_before_websocket_keeps_failure_without_diagnostic_marker(self):
        started = asyncio.Event()

        async def fixture(**kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(diagnostic.campaign, "connect_games", new=AsyncMock(side_effect=fixture)):
            task = asyncio.create_task(self.run_diagnostic())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse((self.output / "diagnostic").exists())
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "interrupted")
        self.assert_restored()

    async def test_production_config_changes_are_rejected(self):
        self.output.mkdir(parents=True)
        recorder = diagnostic.Recorder(self.output, proxy=True)
        with diagnostic.observe_transport(recorder):
            altered = production_kwargs()
            altered["ping_interval"] = 1
            with self.assertRaises(diagnostic.DiagnosticFailure):
                ws_client.connect(diagnostic.RANKED_ENDPOINT, **altered)
        recorder.finish("failed", "diagnostic_failed")
        self.assertEqual(recorder.attempts, 0)
        self.assert_restored()

    async def test_second_connection_is_rejected_without_opening_a_socket(self):
        self.output.mkdir(parents=True)
        recorder = diagnostic.Recorder(self.output, proxy=True)
        with diagnostic.observe_transport(recorder):
            # Creating the original connect context doesn't open a socket until
            # it is awaited. The second context must be rejected synchronously.
            ws_client.connect(diagnostic.RANKED_ENDPOINT, **production_kwargs())
            with self.assertRaises(diagnostic.DiagnosticFailure):
                ws_client.connect(diagnostic.RANKED_ENDPOINT, **production_kwargs())
        result = recorder.finish("failed", "diagnostic_failed")
        self.assertEqual(result["connection_attempts"], 1)
        self.assertEqual(result["status"], "failed")
        self.assert_restored()

    async def test_run_creation_race_does_not_write_into_other_owner_directory(self):
        async def competing_owner(**options):
            self.output.mkdir(parents=True)
            (self.output / "marker").write_text("other run")
            raise FileExistsError(SECRET)

        with patch.object(diagnostic.campaign, "run_campaign", side_effect=competing_owner):
            with self.assertRaises(FileExistsError):
                await self.run_diagnostic()
        self.assertEqual([path.name for path in self.output.iterdir()], ["marker"])
        self.assert_restored()

    async def test_non_file_exists_claim_failure_does_not_authorize_foreign_directory(self):
        async def competing_owner(**options):
            self.output.mkdir(parents=True)
            (self.output / "marker").write_text("other run")
            raise OSError(SECRET)

        with patch.object(diagnostic.campaign, "run_campaign", side_effect=competing_owner):
            with self.assertRaises(OSError):
                await self.run_diagnostic()
        self.assertEqual([path.name for path in self.output.iterdir()], ["marker"])
        self.assertEqual((self.output / "marker").read_text(), "other run")
        self.assert_restored()

    async def test_diagnostic_write_failure_is_terminal_and_never_leaks_exception(self):
        self.output.mkdir(parents=True)
        recorder = diagnostic.Recorder(self.output, proxy=True)
        recorder.ensure_ready()
        with patch.object(recorder.stream, "write", side_effect=RuntimeError(SECRET)):
            recorder.emit("connection_attempt", attempt=1)
        with self.assertRaises(diagnostic.DiagnosticFailure):
            recorder.assert_healthy()
        result = recorder.finish("failed", None)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "diagnostic_write_failed")
        self.assertNotIn(SECRET, self.artifacts())

    async def loopback_case(self, mode, *, direct=False):
        """Only this adapter rewrites the target to a bound loopback server.

        The timeout case accelerates server-side fixture time, after production
        argument validation; no production CLI setting permits this override.
        """
        class NoPong(ServerConnection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                original_send_frame = self.protocol.send_frame

                def send_frame(frame):
                    if frame.opcode != Opcode.PONG:
                        original_send_frame(frame)

                self.protocol.send_frame = send_frame

        async def handler(connection):
            await connection.send(SECRET)
            if mode == "abrupt":
                connection.transport.abort()
            elif mode == "timeout":
                await connection.wait_closed()
            elif mode in ("cancel", "latency", "identity"):
                await connection.wait_closed()
            else:
                await connection.close(1000, SECRET)

        def reject(connection, request):
            return Response(403, SECRET, Headers({"Content-Type": "text/plain"}), SECRET.encode())

        kwargs = {"logger": quiet_logger()}
        if mode == "timeout":
            kwargs["create_connection"] = NoPong
        if mode == "handshake":
            kwargs["process_request"] = reject
        async with serve(handler, "127.0.0.1", 0, **kwargs) as server:
            uri = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
            forwarded = []

            def local_connect(target, **options):
                self.assertEqual(target, diagnostic.RANKED_ENDPOINT)
                forwarded.append({key: value for key, value in options.items()
                                  if key not in ("additional_headers", "logger", "create_connection")})
                options["proxy"] = None
                if mode == "timeout":
                    options.update(ping_interval=.02, ping_timeout=.02)
                return self.original_connect(uri, **options)

            local_connect.__signature__ = inspect.signature(self.original_connect)
            received = asyncio.Event()

            async def connect_fixture(**options):
                self.assertEqual(options["games"], 1)
                self.assertEqual(options["mode"], "ranked")
                report = fixtures.healthy_report()
                identity_patch = None
                try:
                    async with ws_client.connect(diagnostic.RANKED_ENDPOINT,
                                                 **production_kwargs(options["proxy"])) as connection:
                        self.assertEqual(await connection.recv(), SECRET)
                        received.set()
                        if mode == "latency":
                            pong = await connection.ping()
                            await pong
                            await connection.close()
                        if mode == "identity":
                            # Detect source drift at context exit while the
                            # original supervisor still owns terminal status.
                            identity_patch = patch.object(diagnostic, "WRAPPER_SHA256", "0" * 64)
                            identity_patch.start()
                            await connection.close()
                        await connection.recv()
                except ConnectionClosed:
                    if mode != "normal":
                        report.update(status="interrupted", scores=None, failure="connection_closed")
                except InvalidStatus:
                    report.update(status="interrupted", scores=None, failure="connection_or_worker_failed")
                finally:
                    if identity_patch is not None:
                        identity_patch.stop()
                return fixtures.write_game(options["output"], report)

            captured = io.StringIO()
            with (patch.object(ws_client, "connect", local_connect),
                  patch.object(diagnostic.campaign, "connect_games", new=AsyncMock(side_effect=connect_fixture)),
                  redirect_stdout(captured), redirect_stderr(captured)):
                if mode == "cancel":
                    task = asyncio.create_task(self.run_diagnostic(proxy=None if direct else True))
                    await received.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    await self.run_diagnostic(proxy=None if direct else True)
                self.assertIs(ws_client.connect, local_connect)
            self.assertEqual(len(forwarded), 1)
            self.assertNotIn("ping_interval", forwarded[0])
            self.assertNotIn("ping_timeout", forwarded[0])
            self.assertEqual(forwarded[0]["proxy"], None if direct else True)
            self.assertNotIn(SECRET, captured.getvalue())
        self.assertNotIn(SECRET, self.artifacts())
        self.assertNotIn(str(self.token_file), self.artifacts())
        self.assert_restored()
        return self.rows()

    async def test_local_normal_close_codes_counts_and_manifest(self):
        rows = await self.loopback_case("normal", direct=True)
        closed = next(row for row in rows if row["event"] == "recv_failed")
        self.assertEqual(closed["received_close_code"], 1000)
        self.assertEqual(closed["sent_close_code"], 1000)
        self.assertEqual(closed["recv_count"], 1)
        self.assertGreaterEqual(closed["seconds_since_last_recv"], 0)
        self.assertFalse(closed["local_keepalive_timeout_observed"])
        manifest = json.loads((self.output / "diagnostic" / "manifest.json").read_text())
        self.assertEqual(manifest["transport_config"]["ping_interval"], 20)
        self.assertEqual(manifest["transport_config"]["ping_timeout"], 20)
        self.assertEqual(manifest["network_path"], "direct")
        self.assertEqual(manifest["wrapper_sha256"], diagnostic.WRAPPER_SHA256)
        self.assertFalse(manifest["same_runtime_as_uninstrumented"])

    async def test_local_abrupt_close_is_retained(self):
        rows = await self.loopback_case("abrupt")
        closed = next(row for row in rows if row["event"] == "recv_failed")
        self.assertTrue(closed["abnormal_no_close_frames"])
        self.assertIsNone(closed["received_close_code"])
        self.assertEqual(closed["exception_category"], "connection_closed_error")

    async def test_local_keepalive_timeout_uses_original_library(self):
        rows = await self.loopback_case("timeout")
        closed = next(row for row in rows if row["event"] == "recv_failed")
        self.assertEqual(closed["sent_close_code"], 1011)
        self.assertTrue(closed["local_keepalive_timeout_observed"])
        self.assertFalse(closed["received_close_first"])

    async def test_local_handshake_failure_records_only_status_and_category(self):
        rows = await self.loopback_case("handshake")
        failure = next(row for row in rows if row["event"] == "handshake_failed")
        self.assertEqual(failure["http_status"], 403)
        self.assertEqual(failure["exception_category"], "handshake_http_status")
        self.assertNotIn("handshake_complete", [row["event"] for row in rows])
        self.assertIn("connection_lost", [row["event"] for row in rows])

    async def test_local_cancellation_restores_wrapper_and_closes_connection(self):
        rows = await self.loopback_case("cancel")
        self.assertIn("context_exit", [row["event"] for row in rows])
        self.assertIn("connection_lost", [row["event"] for row in rows])

    async def test_local_library_latency_is_observed_without_replacement_estimator(self):
        rows = await self.loopback_case("latency")
        closed = next(row for row in rows if row["event"] == "recv_failed")
        self.assertGreater(closed["library_latency_seconds"], 0)

    async def test_local_source_drift_cannot_complete_campaign(self):
        await self.loopback_case("identity")
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
