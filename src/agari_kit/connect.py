"""Authenticated riichi.dev transport and bounded local run records."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict
from pathlib import Path

from .agent import Rules
from .engine import require_engine
from .online import ENDPOINTS, OnlineSession, ProtocolError
from .plugins import agent_provenance
from .provenance import source_fingerprint
from .worker import ProcessRunner


class ConnectionFailure(RuntimeError):
    pass


async def drive_connection(socket, session: OnlineSession, *, idle_timeout: float = 300) -> dict:
    """Timestamp frames on receipt, including those queued during inference."""
    queue = asyncio.Queue(maxsize=128)

    async def receive():
        try:
            while True:
                raw = await socket.recv()
                received_at = time.monotonic()
                if isinstance(raw, bytes):
                    continue
                await queue.put((raw, received_at, None))
        except asyncio.CancelledError:
            raise
        except Exception:
            await queue.put((None, time.monotonic(), "connection_closed"))

    receiver = asyncio.create_task(receive())
    failure = None
    try:
        while not session.done:
            # Validation can send its verdict after end_game, so keep reading
            # until validation_result (or a bounded close/timeout).
            timeout = min(idle_timeout, 30) if session.ended else idle_timeout
            try:
                raw, received_at, error = await asyncio.wait_for(queue.get(), timeout)
            except asyncio.TimeoutError:
                failure = "receive_timeout"
                break
            if error:
                failure = error
                break
            try:
                if not isinstance(raw, str) or len(raw) > 2**21:
                    raise ProtocolError("invalid_frame")
                message = json.loads(raw)
                response = await session.handle(message, received_at)
                if response is not None:
                    # Only request_action can produce a response, already
                    # checked against the worker's and server's legal actions.
                    remaining = session.reply_deadline - time.monotonic()
                    if remaining <= 0:
                        session.counts["send_timeouts"] += 1
                        continue
                    try:
                        await asyncio.wait_for(socket.send(json.dumps(response)), timeout=remaining)
                    except asyncio.TimeoutError:
                        session.counts["send_timeouts"] += 1
                        raise
                    session.counts["replies_sent"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not print exception text, websocket headers or server error
                # bodies: any of those can contain a Bot Token.
                failure = "protocol_error" if isinstance(exc, (ProtocolError, ValueError)) else "client_error"
                break
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await session.runner.close()
    report = session.report()
    if failure:
        report["failure"] = failure
    return report


async def connect_games(*, mode: str, agent_name: str, token: str, games: int,
                        output: Path, reserve_ms: int = 100, idle_timeout: float = 300,
                        proxy: str | bool | None = True) -> dict:
    if mode not in ENDPOINTS or type(games) is not int or games < 1:
        raise ValueError("Choose validate/ranked and a positive game count")
    if mode == "validate" and games != 1:
        raise ValueError("Validation runs one game; activation changes token eligibility")
    if not token or any(char.isspace() for char in token):
        raise ValueError("RIICHI_BOT_TOKEN is missing or malformed")
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError("idle-timeout must be positive")
    if type(reserve_ms) is not int or reserve_ms < 0:
        raise ValueError("reserve-ms must be a nonnegative integer")
    require_engine()
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise ConnectionFailure("Install the locked websockets dependency first") from exc
    # A private disabled logger prevents debug handshake logging of Authorization,
    # even when an embedding application enables DEBUG on the root logger.
    logger = logging.Logger("agari.private.websocket")
    logger.disabled = True
    logger.propagate = False
    provenance = agent_provenance(agent_name)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "endpoint": ENDPOINTS[mode], "mode": mode,
                "agent": agent_name, "agent_provenance": provenance, "code": source_fingerprint(),
                "rules_expected": asdict(Rules(game_mode="4p-red-east" if mode == "validate" else "4p-red-half")),
                "games_requested": games, "reserve_ms": reserve_ms, "status": "running"}
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    reports = []
    try:
        with (output / "events.jsonl").open("x") as events, (output / "games.jsonl").open("x") as results:
            for game_index in range(games):
                if agent_provenance(agent_name) != provenance or source_fingerprint() != manifest["code"]:
                    raise ConnectionFailure("Agent or toolkit changed during the run")
                def emit(record):
                    events.write(json.dumps(dict(record, game_index=game_index)) + "\n")
                    events.flush()

                runner = ProcessRunner(agent_name)
                session = OnlineSession(mode=mode, runner=runner, emit=emit, reserve_ms=reserve_ms,
                                        seed=game_index)
                try:
                    # Load engine/model before entering matchmaking or starting
                    # any decision clock. No token is sent to the agent.
                    await runner.start()
                    if agent_provenance(agent_name) != provenance or source_fingerprint() != manifest["code"]:
                        raise ConnectionFailure("Agent or toolkit changed while loading")
                    async with connect(ENDPOINTS[mode], additional_headers={"Authorization": f"Bearer {token}"},
                                       proxy=proxy, logger=logger, open_timeout=15,
                                       close_timeout=2, max_size=2**21, max_queue=16) as socket:
                        report = await drive_connection(socket, session, idle_timeout=idle_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    report = session.report()
                    report["failure"] = "connection_or_worker_failed"
                finally:
                    await runner.close()
                reports.append(report)
                results.write(json.dumps(dict(report, game_index=game_index)) + "\n")
                results.flush()
                # A dropped game is not resumable via this protocol. Stop rather
                # than count another matchmaking connection as the same game.
                if report["status"] != "complete" or report.get("validation_passed") is False:
                    break
        successful = sum(report["status"] == "complete" for report in reports)
        summary = {"games_requested": games, "games_completed": successful,
                   "validation_passed": reports[-1].get("validation_passed") if reports else None,
                   "degraded_games": sum(report["degraded"] for report in reports),
                   "counts": {key: sum(report["counts"].get(key, 0) for report in reports)
                              for key in sorted({key for report in reports for key in report["counts"]})}}
        manifest["status"] = "complete" if successful == games and summary["validation_passed"] is not False else "incomplete"
        (output / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "interrupted"
        manifest["games_recorded"] = len(reports)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
