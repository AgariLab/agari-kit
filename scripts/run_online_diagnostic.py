#!/usr/bin/env python3
"""Run one ranked game with numeric, credential-free transport diagnostics."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
import json
import math
from pathlib import Path
import signal
import socket
import ssl
import sys
import time

import websockets
from websockets.asyncio import client as ws_client
from websockets import exceptions as ws_errors
from websockets.frames import Close
from websockets.uri import get_proxy, parse_uri


SCRIPT = Path(__file__).resolve()
SUPERVISOR = SCRIPT.with_name("run_online_campaign.py")
SUPERVISOR_SHA256 = "cd570e8abc1b5b028661f6dfa7881ebdea7c650db11b7d5a0c7fd53adbe4aede"
WRAPPER_SHA256 = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
RANKED_ENDPOINT = "wss://game.riichi.dev/ws/ranked"
CONFIG = {"ping_interval": 20, "ping_timeout": 20, "open_timeout": 15,
          "close_timeout": 2, "max_size": 2**21, "max_queue": 16,
          "write_limit": 2**15, "compression": "deflate"}
_ACTIVE = False


class DiagnosticFailure(RuntimeError):
    """Messages are fixed local codes, never upstream exception text."""


def check_identity() -> None:
    try:
        matches = (hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == WRAPPER_SHA256
                   and hashlib.sha256(SUPERVISOR.read_bytes()).hexdigest() == SUPERVISOR_SHA256)
    except OSError:
        matches = False
    if not matches:
        raise DiagnosticFailure("diagnostic_identity_changed")


check_identity()
_SPEC = importlib.util.spec_from_file_location("_diagnostic_original_campaign", SUPERVISOR)
campaign = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(campaign)
check_identity()


def exception_category(exc: BaseException | None) -> str | None:
    """Never inspect exception text, module/class names, headers, or bodies."""
    if exc is None:
        return None
    for cls, label in (
        (asyncio.CancelledError, "cancelled"),
        (ws_errors.ConnectionClosedOK, "connection_closed_ok"),
        (ws_errors.ConnectionClosedError, "connection_closed_error"),
        (ws_errors.ConnectionClosed, "connection_closed"),
        (ws_errors.InvalidProxyStatus, "proxy_http_status"),
        (ws_errors.InvalidProxyMessage, "proxy_invalid_response"),
        (ws_errors.ProxyError, "proxy_error"),
        (ws_errors.InvalidProxy, "invalid_proxy"),
        (ws_errors.InvalidStatus, "handshake_http_status"),
        (ws_errors.InvalidMessage, "handshake_invalid_response"),
        (ws_errors.SecurityError, "handshake_security_error"),
        (ws_errors.InvalidHandshake, "handshake_error"),
        (ws_errors.InvalidURI, "invalid_uri"),
        (ws_errors.PayloadTooBig, "payload_too_big"),
        (ws_errors.ProtocolError, "websocket_protocol_error"),
        (ssl.SSLCertVerificationError, "tls_certificate_error"),
        (ssl.SSLError, "tls_error"),
        (socket.gaierror, "dns_error"),
        (TimeoutError, "timeout"),
        (ConnectionResetError, "connection_reset"),
        (ConnectionRefusedError, "connection_refused"),
        (ConnectionAbortedError, "connection_aborted"),
        (BrokenPipeError, "broken_pipe"),
        (EOFError, "eof"),
        (OSError, "os_error"),
        (DiagnosticFailure, "diagnostic_error"),
    ):
        if isinstance(exc, cls):
            return label
    return "other_error"


def close_facts(received=None, sent=None, received_first=None) -> dict:
    def code(frame):
        if isinstance(frame, Close) and isinstance(frame.code, int) and not isinstance(frame.code, bool):
            return int(frame.code) if 1000 <= frame.code <= 4999 else None
        return None

    sent_code, received_code = code(sent), code(received)
    # Compare only the library's exact local timeout signature; never persist a
    # reason. A peer-initiated close echoing this phrase isn't a local timeout.
    timeout = (sent_code == 1011 and received_first is not True
               and type(sent.reason) is str and sent.reason == "keepalive ping timeout")
    return {"received_close_code": received_code, "sent_close_code": sent_code,
            "received_close_first": received_first if type(received_first) is bool else None,
            "local_keepalive_timeout_observed": bool(timeout),
            "abnormal_no_close_frames": received is None and sent is None}


def exception_facts(exc: BaseException | None) -> dict:
    facts = {"exception_category": exception_category(exc),
             "cause_category": exception_category(exc.__cause__) if exc is not None else None}
    if isinstance(exc, ws_errors.ConnectionClosed):
        facts.update(close_facts(exc.rcvd, exc.sent, exc.rcvd_then_sent))
    if isinstance(exc, (ws_errors.InvalidStatus, ws_errors.InvalidProxyStatus)):
        status = exc.response.status_code
        facts["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
    return facts


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


class Recorder:
    def __init__(self, output: Path, *, proxy: bool | None):
        self.output = output
        self.directory = output / "diagnostic"
        self.proxy = proxy
        self.started = time.monotonic()
        self.stream = None
        self.ready = False
        self.failure = None
        self.attempts = 0
        self.connections = 0
        self.events = 0

    def ensure_ready(self):
        if self.ready:
            self.assert_healthy()
            return
        if self.failure:
            raise DiagnosticFailure("diagnostic_unavailable")
        try:
            check_identity()
            # The original supervisor exclusively creates output. Never create
            # it here or reuse another run's diagnostics, including after failure.
            self.directory.mkdir(exist_ok=False)
            selected_proxy = get_proxy(parse_uri(RANKED_ENDPOINT)) if self.proxy is True else None
            manifest = {
                "schema_version": 1, "mode": "ranked", "games_requested": 1,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "wrapper_sha256": WRAPPER_SHA256, "supervisor_sha256": SUPERVISOR_SHA256,
                "websockets_version": websockets.__version__,
                "python_version": list(sys.version_info[:3]),
                "runtime_variant": "parent_transport_observer",
                "same_runtime_as_uninstrumented": False,
                "proxy_policy": "environment" if self.proxy is True else "disabled",
                "network_path": "proxy" if selected_proxy is not None else "direct",
                "transport_config": CONFIG,
                "automatic_reconnect": False,
                "timeout_observation": "local_1011_exact_library_signature",
                "recv_count_unit": "successfully_returned_application_messages",
            }
            with (self.directory / "manifest.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(manifest, indent=2) + "\n")
            self.stream = (self.directory / "events.jsonl").open("x", encoding="utf-8")
            self.ready = True
            self.emit("diagnostic_ready")
            self.assert_healthy()
        except BaseException:
            self.failure = "diagnostic_initialization_failed"
            raise

    def emit(self, event: str, **facts):
        # Used from asyncio callbacks too: don't let an I/O exception escape to
        # asyncio's default handler, which could format protocol objects.
        if not self.ready or self.failure:
            return
        try:
            row = {"event": event, "elapsed_seconds": time.monotonic() - self.started, **facts}
            self.stream.write(json.dumps(row, allow_nan=False) + "\n")
            self.stream.flush()
            self.events += 1
        except Exception:
            self.failure = "diagnostic_write_failed"

    def assert_healthy(self):
        if self.failure:
            raise DiagnosticFailure("diagnostic_unavailable")

    def finish(self, campaign_status: str, failure: str | None):
        if not self.ready:
            self.ensure_ready()
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        result = {"schema_version": 1,
                  "status": "failed" if failure or self.failure else "finalized",
                  "error": failure or self.failure,
                  "campaign_status": campaign_status,
                  "connection_attempts": self.attempts, "connections_created": self.connections,
                  "events_recorded": self.events,
                  "elapsed_seconds": time.monotonic() - self.started}
        with (self.directory / "result.json").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
        return result


def connection_class(recorder: Recorder):
    class ObservedConnection(ws_client.ClientConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            recorder.connections += 1
            self.diagnostic_id = recorder.connections
            self.received_count = 0
            self.last_received = None
            recorder.emit("connection_created", connection_id=self.diagnostic_id)

        def snapshot(self):
            now = time.monotonic()
            facts = {"connection_id": self.diagnostic_id,
                    "recv_count": self.received_count,
                    "seconds_since_last_recv": None if self.last_received is None else now - self.last_received,
                    "library_latency_seconds": _number(self.latency),
                    **close_facts(self.protocol.close_rcvd, self.protocol.close_sent,
                                  self.protocol.close_rcvd_then_sent)}
            # An open connection normally has no close frames; only label it an
            # abnormal close after the library has finished closing it.
            facts["abnormal_no_close_frames"] &= self.protocol.state.name == "CLOSED"
            return facts

        async def recv(self, decode=None):
            try:
                value = await super().recv(decode)
            except asyncio.CancelledError:
                recorder.emit("recv_cancelled", **self.snapshot())
                raise
            except Exception as exc:
                facts = self.snapshot()
                facts.update(exception_facts(exc))
                recorder.emit("recv_failed", **facts)
                raise
            self.received_count += 1
            self.last_received = time.monotonic()
            recorder.assert_healthy()
            return value

        async def send(self, message, text=None):
            try:
                return await super().send(message, text=text)
            except Exception as exc:
                facts = self.snapshot()
                facts.update(exception_facts(exc))
                recorder.emit("send_failed", **facts)
                raise

        def connection_lost(self, exc):
            super().connection_lost(exc)
            facts = self.snapshot()
            facts.update(exception_facts(exc))
            facts["receive_exception_category"] = exception_category(self.recv_exc)
            recorder.emit("connection_lost", **facts)

    return ObservedConnection


class ObservedContext:
    def __init__(self, inner, recorder):
        self.inner, self.recorder, self.connection = inner, recorder, None

    async def __aenter__(self):
        try:
            self.connection = await self.inner.__aenter__()
        except BaseException as exc:
            # websockets aborts failed handshakes before raising, but its final
            # asyncio callback can still be pending. Drain that callback before
            # finalizing the diagnostic files.
            failed_connection = getattr(self.inner, "connection", None)
            if failed_connection is not None:
                await failed_connection.wait_closed()
            self.recorder.emit("handshake_failed", **exception_facts(exc))
            raise
        try:
            check_identity()
            self.recorder.assert_healthy()
            self.recorder.emit("handshake_complete", **self.connection.snapshot())
            self.recorder.assert_healthy()
        except BaseException:
            await self.inner.__aexit__(None, None, None)
            raise
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self.inner.__aexit__(exc_type, exc, traceback)
        finally:
            facts = self.connection.snapshot()
            facts.update(exception_facts(exc))
            self.recorder.emit("context_exit", **facts)
            check_identity()
            self.recorder.assert_healthy()


@contextmanager
def observe_transport(recorder: Recorder):
    global _ACTIVE
    if _ACTIVE:
        raise DiagnosticFailure("diagnostic_already_active")
    check_identity()
    original = ws_client.connect
    defaults = inspect.signature(original).parameters
    if (websockets.__version__ != "15.0.1" or sys.version_info[:2] != (3, 12)
            or any(defaults[name].default != CONFIG[name]
                   for name in ("ping_interval", "ping_timeout", "write_limit", "compression"))):
        raise DiagnosticFailure("diagnostic_unsupported_runtime")
    observed_class = connection_class(recorder)

    def observed_connect(uri, **kwargs):
        check_identity()
        recorder.ensure_ready()
        recorder.assert_healthy()
        expected = {"open_timeout": 15, "close_timeout": 2, "max_size": 2**21, "max_queue": 16}
        if (uri != RANKED_ENDPOINT or set(kwargs) != set(expected) | {"additional_headers", "proxy", "logger"}
                or any(kwargs.get(key) != value for key, value in expected.items())
                or kwargs.get("proxy") is not recorder.proxy or recorder.attempts):
            recorder.failure = "diagnostic_unexpected_connection"
            raise DiagnosticFailure("diagnostic_unexpected_connection")
        logger = kwargs["logger"]
        if not logger.disabled or logger.propagate:
            recorder.failure = "diagnostic_unsafe_logger"
            raise DiagnosticFailure("diagnostic_unsafe_logger")
        recorder.attempts += 1
        recorder.emit("connection_attempt", attempt=recorder.attempts)
        recorder.assert_healthy()
        try:
            inner = original(uri, **kwargs, create_connection=observed_class)
        except BaseException as exc:
            recorder.emit("handshake_failed", **exception_facts(exc))
            raise
        return ObservedContext(inner, recorder)

    _ACTIVE = True
    ws_client.connect = observed_connect
    try:
        yield
    finally:
        ws_client.connect = original
        _ACTIVE = False


async def run_diagnostic(*, agent_name: str, token_file: Path, output: Path,
                         games: int = 1, reserve_ms: int = 100,
                         idle_timeout: float = 1800, proxy: bool | None = True) -> dict:
    if type(games) is not int or games != 1:
        raise ValueError("diagnostic_requires_one_game")
    if proxy is not True and proxy is not None:
        raise ValueError("diagnostic_invalid_proxy_policy")
    if type(reserve_ms) is not int or reserve_ms < 0 or not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError("diagnostic_invalid_limits")
    if output.exists() or output.is_symlink():
        raise FileExistsError("diagnostic_run_exists")
    recorder = Recorder(output, proxy=proxy)
    owned = False
    status, failure = "failed", None
    try:
        with observe_transport(recorder):
            try:
                result = await campaign.run_campaign(
                    agent_name=agent_name, token_file=token_file, output=output,
                    games=1, reserve_ms=reserve_ms, idle_timeout=idle_timeout, proxy=proxy)
            except FileExistsError:
                raise
            except BaseException:
                # Directory existence doesn't prove our mkdir won. Only an
                # initialized observer proves the verified supervisor advanced
                # past its exclusive claim. Earlier cancellation keeps only the
                # supervisor's own records, without a diagnostic completion file.
                owned = recorder.ready
                raise
            owned = True
            status = result.get("status")
            if status not in ("complete", "stopped", "failed", "interrupted"):
                status = "failed"
            check_identity()
            recorder.assert_healthy()
            if status == "complete" and recorder.attempts != 1:
                raise DiagnosticFailure("diagnostic_missing_connection")
        return result
    except asyncio.CancelledError:
        status, failure = "interrupted", "cancelled"
        raise
    except BaseException:
        failure = "diagnostic_failed"
        raise
    finally:
        if owned:
            try:
                recorder.finish(status, failure)
            finally:
                if recorder.stream is not None:
                    recorder.stream.close()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--agent", required=True)
    result.add_argument("--token-file", required=True, type=Path)
    result.add_argument("--run", required=True, help="New relative directory below artifact runs")
    result.add_argument("--games", type=int, choices=(1,), default=1)
    result.add_argument("--reserve-ms", type=int, default=100)
    result.add_argument("--idle-timeout", type=float, default=1800)
    result.add_argument("--direct", action="store_true", help="Disable environment proxy")
    return result


async def _run_cli(args, output):
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await run_diagnostic(agent_name=args.agent, token_file=args.token_file,
                                    output=output, games=args.games, reserve_ms=args.reserve_ms,
                                    idle_timeout=args.idle_timeout, proxy=None if args.direct else True)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        check_identity()
        if not args.run or Path(args.run).is_absolute() or ".." in Path(args.run).parts:
            raise ValueError("invalid_run_path")
        storage = campaign.Storage.from_env()
        storage.check(probe=True)
        result = asyncio.run(_run_cli(args, storage.path("runs/" + args.run)))
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["status"] == "complete" else 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("online_diagnostic: interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("online_diagnostic: failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
