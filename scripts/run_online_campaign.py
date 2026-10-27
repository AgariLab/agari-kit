#!/usr/bin/env python3
"""Run a bounded ranked campaign, checking each completed game before requeueing."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import signal
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agari_kit.connect import connect_games
from agari_kit.engine import engine_identity
from agari_kit.plugins import agent_provenance
from agari_kit.provenance import source_fingerprint
from agari_kit.storage import Storage


BAD_COUNTS = (
    "local_timeouts", "send_timeouts", "ack_rejected", "ack_unparseable",
    "ack_defaulted", "ack_stale", "unknown_ack_status", "suppressed_after_timeout",
)
COUNT_FIELDS = BAD_COUNTS + (
    "requests", "replies_prepared", "replies_sent", "ack_accepted", "duplicate_start",
    "duplicate_requests", "stale_requests", "duplicate_acks", "unknown_messages",
)


def identity_snapshot(agent_name: str) -> dict:
    return {"agent_provenance": agent_provenance(agent_name),
            "engine": engine_identity(), "code": source_fingerprint(),
            "launcher_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def _load_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ValueError("token_file_unreadable") from None
    if not token or any(character.isspace() for character in token):
        raise ValueError("token_file_invalid")
    return token


def _latencies(directory: Path) -> dict:
    values = []
    path = directory / "events.jsonl"
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for raw in stream:
                event = json.loads(raw)
                if not isinstance(event, dict) or event.get("event") != "decision":
                    continue
                value = event.get("latency_ms")
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    values.append(value)
    values.sort()
    return {"count": len(values),
            "mean_ms": statistics.fmean(values) if values else None,
            "p50_ms": statistics.median(values) if values else None,
            "p95_ms": values[math.ceil(len(values) * .95) - 1] if values else None,
            "max_ms": values[-1] if values else None}


def _game_record(directory: Path, number: int) -> dict:
    """Copy only typed, safe fields from the existing transport's run records."""
    rows = (directory / "games.jsonl").read_text(encoding="utf-8").splitlines()
    if len(rows) != 1:
        raise ValueError("invalid_game_record")
    report = json.loads(rows[0])
    if not isinstance(report, dict):
        raise ValueError("invalid_game_record")
    counts = report.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("invalid_game_counts")
    safe_counts = {}
    for name in COUNT_FIELDS:
        value = counts.get(name, 0)
        if type(value) is not int or value < 0:
            raise ValueError("invalid_game_counts")
        safe_counts[name] = value
    scores, seat = report.get("scores"), report.get("seat")
    if scores is not None and (not isinstance(scores, list) or len(scores) != 4
                               or any(type(value) is not int for value in scores)):
        raise ValueError("invalid_game_scores")
    if seat is not None and (type(seat) is not int or seat not in range(4)):
        raise ValueError("invalid_game_seat")
    if type(report.get("degraded")) is not bool:
        raise ValueError("invalid_degraded_flag")
    status = report.get("status")
    if status not in ("complete", "interrupted", "validation_unconfirmed"):
        status = "invalid"
    failures = []
    if status != "complete":
        failures.append("game_incomplete")
    if report.get("failure"):
        failures.append("game_failed")
    if report["degraded"]:
        failures.append("game_degraded")
    if status == "complete" and (seat is None or scores is None):
        failures.append("missing_game_result")
    failures.extend(name for name in BAD_COUNTS if safe_counts[name])
    requests = safe_counts["requests"]
    if requests == 0 or requests != safe_counts["replies_sent"] or requests != safe_counts["ack_accepted"]:
        failures.append("action_receipts_mismatch")
    try:
        latency = _latencies(directory)
    except Exception:
        # Optional timing data must not erase an already completed game result.
        latency = {"count": 0}
        failures.append("latency_unavailable")
    return {"game_number": number, "directory": directory.name, "status": status,
            "healthy": not failures, "errors": failures, "seat": seat, "scores": scores,
            "degraded": report["degraded"], "counts": safe_counts,
            "latency": latency}


def _failed_record(directory: Path, number: int, error: str) -> dict:
    try:
        record = _game_record(directory, number)
    except Exception:
        record = {"game_number": number, "directory": directory.name, "status": "interrupted",
                  "healthy": False, "errors": [], "seat": None, "scores": None,
                  "degraded": False, "counts": {}, "latency": {"count": 0}}
    record["healthy"] = False
    record["errors"].append(error)
    return record


async def run_campaign(*, agent_name: str, token_file: Path, output: Path,
                       games: int = 100, reserve_ms: int = 100,
                       idle_timeout: float = 1800, proxy: str | bool | None = True) -> dict:
    if type(games) is not int or games < 1:
        raise ValueError("invalid_game_count")
    if type(reserve_ms) is not int or reserve_ms < 0:
        raise ValueError("invalid_reserve")
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError("invalid_idle_timeout")
    # Claim the campaign name before doing work. Existing runs are never reused.
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "mode": "ranked", "agent": agent_name,
                "games_requested": games, "reserve_ms": reserve_ms,
                "idle_timeout": idle_timeout, "started_at": _utc_now(),
                "status": "running", "current_game": None, "error": None}
    summary = {"schema_version": 1, "games_requested": games, "games_attempted": 0,
               "games_completed": 0, "healthy_games": 0, "status": "running",
               "current_game": None, "error": None, "counts": {},
               "latency": {"count": 0, "mean_ms": None, "max_ms": None}}
    records = []
    active = None
    started = time.monotonic()

    def persist() -> None:
        manifest.update(status=summary["status"], current_game=summary["current_game"],
                        error=summary["error"], games_recorded=len(records), updated_at=_utc_now())
        summary["elapsed_seconds"] = time.monotonic() - started
        _write_json(output / "manifest.json", manifest)
        _write_json(output / "summary.json", summary)

    def record_game(record: dict) -> None:
        # Append every attempt, including the stopping game; never filter failures.
        with (output / "games.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
        records.append(record)
        summary["games_completed"] += record["status"] == "complete"
        summary["healthy_games"] += record["healthy"]
        for name, value in record["counts"].items():
            summary["counts"][name] = summary["counts"].get(name, 0) + value
        samples = [(item["latency"].get("count", 0), item["latency"].get("mean_ms"),
                    item["latency"].get("max_ms")) for item in records]
        count = sum(item[0] for item in samples)
        summary["latency"] = {
            "count": count,
            "mean_ms": sum(n * mean for n, mean, _ in samples if n) / count if count else None,
            "max_ms": max((maximum for n, _, maximum in samples if n), default=None),
        }

    try:
        persist()
        # Neither the token nor its path is written to campaign records.
        token = _load_token(token_file)
        identity = identity_snapshot(agent_name)
        manifest.update(identity)
        (output / "games.jsonl").touch(exist_ok=False)
        persist()
        for number in range(1, games + 1):
            if identity_snapshot(agent_name) != identity:
                summary.update(status="stopped", error="identity_changed")
                break
            directory = output / f"game-{number:06d}"
            if directory.exists():
                summary.update(status="stopped", error="game_directory_exists")
                break
            active = (directory, number, time.monotonic())
            summary.update(games_attempted=number, current_game=number)
            persist()
            # Cancellation propagates into connect_games so its existing finally
            # blocks close the websocket and reap the agent process group.
            result = await connect_games(mode="ranked", agent_name=agent_name, token=token,
                                         games=1, output=directory, reserve_ms=reserve_ms,
                                         idle_timeout=idle_timeout, proxy=proxy)
            record = _game_record(directory, number)
            transport_manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if result.get("games_completed") != 1 or transport_manifest.get("status") != "complete":
                record["errors"].append("transport_incomplete")
            if (identity_snapshot(agent_name) != identity
                    or any(transport_manifest.get(key) != identity[key]
                           for key in ("agent_provenance", "engine", "code"))):
                record["errors"].append("identity_changed")
            record["healthy"] = not record["errors"]
            record["elapsed_seconds"] = time.monotonic() - active[2]
            record_game(record)
            active = None
            if not record["healthy"]:
                summary.update(status="stopped", error=record["errors"][0])
                break
            persist()
        else:
            summary.update(status="complete", current_game=None)
    except asyncio.CancelledError:
        summary.update(status="interrupted", error="cancelled")
        if active is not None:
            record = _failed_record(active[0], active[1], "cancelled")
            record["elapsed_seconds"] = time.monotonic() - active[2]
            record_game(record)
            active = None
        raise
    except Exception:
        summary.update(status="failed", error="campaign_failed")
        if active is not None:
            record = _failed_record(active[0], active[1], "campaign_failed")
            record["elapsed_seconds"] = time.monotonic() - active[2]
            record_game(record)
            active = None
    finally:
        if summary["status"] == "running":
            summary.update(status="interrupted", error="campaign_interrupted")
        persist()
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--agent", required=True, help="Builtin name or @agent-descriptor.json")
    result.add_argument("--token-file", required=True, type=Path)
    result.add_argument("--games", type=int, default=100)
    result.add_argument("--run", required=True, help="New relative directory below artifact runs")
    result.add_argument("--idle-timeout", type=float, default=1800)
    result.add_argument("--reserve-ms", type=int, default=100)
    result.add_argument("--direct", action="store_true", help="Disable environment proxy")
    return result


async def _run_cli(args, output: Path) -> dict:
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await run_campaign(agent_name=args.agent, token_file=args.token_file,
                                  output=output, games=args.games, idle_timeout=args.idle_timeout,
                                  reserve_ms=args.reserve_ms, proxy=None if args.direct else True)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        storage = Storage.from_env()
        storage.check(probe=True)
        if not args.run or Path(args.run).is_absolute() or ".." in Path(args.run).parts:
            raise ValueError("invalid_run_path")
        output = storage.path("runs/" + args.run)
        result = asyncio.run(_run_cli(args, output))
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["status"] == "complete" else 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("online_campaign: interrupted", file=sys.stderr)
        return 130
    except Exception:
        # Exception text can contain credential files, remote bodies or headers.
        print("online_campaign: startup_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
