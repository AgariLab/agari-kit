"""Resumable evaluation: immutable plan, whole-seed attempts, verified merge.

An interrupted seed is rerun in full in a new attempt directory. Completed
groups are reused only with a sealed receipt; old attempts are never edited.
Independent shards may run concurrently, while merging locks every shard.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path

from .agent import Rules
from .engine import engine_identity
from .evaluation import evaluate, result_report, schedule
from .plugins import agent_provenance
from .provenance import source_fingerprint

ARTIFACTS = ("manifest.json", "games.jsonl", "report.json", "candidate-latencies.json")
CONDITIONS = ("rules", "lineup", "lineup_provenance", "engine", "code", "python")


def _bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _atomic(path: Path, content: bytes):
    """Only derived merge outputs are replaceable; attempts and plans are not."""
    descriptor, temporary = tempfile.mkstemp(prefix=".merge-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(path):
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Evaluation shard is already running") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _identity(lineup):
    return {"rules": asdict(Rules()), "lineup": lineup,
            "lineup_provenance": [agent_provenance(name) for name in lineup],
            "engine": engine_identity(), "code": source_fingerprint(), "python": sys.version}


def _plan(output, seeds, lineup, shard_seed_count, resume):
    list(schedule(seeds, lineup))
    if type(shard_seed_count) is not int or shard_seed_count < 1:
        raise ValueError("shard-seeds must be a positive integer")
    identity = _identity(lineup)
    expected = {"schema_version": 1, **identity, "seeds": seeds,
                "shard_seed_count": shard_seed_count,
                "shards": [seeds[i:i + shard_seed_count] for i in range(0, len(seeds), shard_seed_count)]}
    if resume:
        content = (output / "plan.json").read_bytes()
        if (output / "plan.sha256").read_text().strip() != _sha(content):
            raise ValueError("Evaluation plan hash mismatch")
        if json.loads(content) != expected:
            raise ValueError("Evaluation plan or runtime identity differs; use a new campaign")
    else:
        output.mkdir(parents=True, exist_ok=False)
        content = _bytes(expected)
        (output / "plan.json").write_bytes(content)
        (output / "plan.sha256").write_text(_sha(content) + "\n")
    return expected, _sha(content)


def _load_group(attempt, plan, seed):
    if any((attempt / name).is_symlink() or not (attempt / name).is_file() for name in ARTIFACTS):
        raise ValueError("Evaluation artifacts must be regular files")
    contents = {name: (attempt / name).read_bytes() for name in ARTIFACTS}
    manifest = json.loads(contents["manifest.json"])
    if (any(manifest.get(key) != plan[key] for key in CONDITIONS)
            or manifest.get("status") != "complete" or manifest.get("seeds") != [seed]
            or manifest.get("games_expected") != 4 or manifest.get("games_completed") != 4):
        raise ValueError("Evaluation group identity or completion mismatch")
    rows = [json.loads(line) for line in contents["games.jsonl"].splitlines() if line.strip()]
    if len(rows) != 4 or [row.get("rotation") for row in rows] != list(range(4)):
        raise ValueError("Evaluation group must contain exactly four ordered rotations")
    for row in rows:
        rotation = row["rotation"]
        scores = row.get("scores")
        if (row.get("seed") != seed or type(row.get("seed")) is not int
                or row.get("seats") != [plan["lineup"][(seat - rotation) % 4] for seat in range(4)]
                or row.get("illegal_actions") != 0 or not isinstance(scores, list)
                or len(scores) != 4 or any(type(score) is not int for score in scores)):
            raise ValueError("Invalid evaluation result")
        order = sorted(range(4), key=lambda seat: (-scores[seat], seat))
        if row.get("ranks") != [order.index(seat) + 1 for seat in range(4)]:
            raise ValueError("Evaluation ranks disagree with scores")
    latencies = json.loads(contents["candidate-latencies.json"])
    if (not isinstance(latencies, list) or not latencies
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in latencies)
            or len(latencies) != sum(row["candidate_decisions"] for row in rows)):
        raise ValueError("Invalid candidate latency samples")
    if json.loads(contents["report.json"]) != result_report(rows, latencies):
        raise ValueError("Evaluation group summary disagrees with raw results")
    return rows, latencies, {name: _sha(value) for name, value in contents.items()}


def _partial_hashes(attempt):
    hashes = {}
    for name in ARTIFACTS:
        path = attempt / name
        if path.is_symlink():
            raise ValueError("Evaluation artifacts cannot be symlinks")
        if path.exists():
            if not path.is_file():
                raise ValueError("Evaluation artifacts must be regular files")
            hashes[name] = _sha(path.read_bytes())
    return hashes


def _completed(output, plan, plan_sha):
    completed = {}
    known = {f"seed-{seed:020d}": seed for seed in plan["seeds"]}
    groups = output / "groups"
    if not groups.exists():
        return completed
    for group in sorted(groups.iterdir()):
        if group.name not in known or not group.is_dir() or group.is_symlink():
            raise ValueError("Unexpected evaluation seed directory")
        seed = known[group.name]
        for attempt in sorted(group.iterdir()):
            if (not attempt.name.startswith("attempt-") or not attempt.name[8:].isdigit()
                    or not attempt.is_dir() or attempt.is_symlink()):
                raise ValueError("Unexpected evaluation attempt directory")
            receipt_path = attempt / "receipt.json"
            failure_path = attempt / "failure.json"
            if failure_path.exists():
                failure = json.loads(failure_path.read_bytes())
                if (not isinstance(failure.get("error_type"), str)
                        or failure != {"schema_version": 1, "plan_sha256": plan_sha, "seed": seed,
                                       "error_type": failure.get("error_type"), "sha256": _partial_hashes(attempt)}
                        or receipt_path.exists()):
                    raise ValueError("Failed evaluation attempt hash mismatch")
            if not receipt_path.exists():
                # Failed / interrupted / unsealed attempts stay as evidence.
                continue
            receipt = json.loads(receipt_path.read_bytes())
            rows, latencies, hashes = _load_group(attempt, plan, seed)
            if receipt != {"schema_version": 1, "plan_sha256": plan_sha, "seed": seed, "sha256": hashes}:
                raise ValueError("Evaluation group receipt hash mismatch")
            if seed in completed:
                raise ValueError("Duplicate completed evaluation seed")
            completed[seed] = (rows, latencies, {"path": str(attempt.relative_to(output)),
                                               "receipt_sha256": _sha(receipt_path.read_bytes()),
                                               "sha256": hashes})
    return completed


def _merge(output, plan, plan_sha, completed, *, verify_only=False):
    if set(completed) != set(plan["seeds"]):
        raise ValueError("Cannot merge an incomplete evaluation campaign")
    rows = [row for seed in plan["seeds"] for row in completed[seed][0]]
    latencies = [value for seed in plan["seeds"] for value in completed[seed][1]]
    attempts = []
    for attempt in sorted((output / "groups").glob("*/attempt-*")):
        hashes = {}
        for path in sorted(attempt.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ValueError("Evaluation attempt evidence must be regular files")
            hashes[path.name] = _sha(path.read_bytes())
        status = "complete" if "receipt.json" in hashes else "failed" if "failure.json" in hashes else "unsealed"
        attempts.append({"path": str(attempt.relative_to(output)), "status": status, "sha256": hashes})
    manifest = {"schema_version": 2, **{key: plan[key] for key in CONDITIONS},
                "seeds": plan["seeds"], "games_expected": len(rows), "games_completed": len(rows),
                "status": "complete", "campaign_plan_sha256": plan_sha,
                "groups": [completed[seed][2] for seed in plan["seeds"]], "attempts": attempts}
    report = result_report(rows, latencies)
    report["status"] = "complete"
    report["attempts"] = {"total": len(attempts),
                          "failed": sum(item["status"] == "failed" for item in attempts),
                          "unsealed": sum(item["status"] == "unsealed" for item in attempts)}
    outputs = {"manifest.json": _bytes(manifest), "report.json": _bytes(report),
               "games.jsonl": b"".join((json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode() for row in rows),
               "candidate-latencies.json": _bytes(latencies)}
    receipt = {"schema_version": 1, "plan_sha256": plan_sha,
               "sha256": {name: _sha(content) for name, content in outputs.items()}}
    if (output / "completion.json").exists():
        if (json.loads((output / "completion.json").read_bytes()) != receipt
                or any((output / name).read_bytes() != content for name, content in outputs.items())):
            raise ValueError("Merged evaluation hash mismatch")
        return report
    if verify_only:
        raise ValueError("Evaluation campaign has no sealed completion")
    for name, content in outputs.items():
        _atomic(output / name, content)
    _atomic(output / "completion.json", _bytes(receipt))
    return report


def verify_campaign(output: Path) -> dict:
    """Verify frozen campaign evidence without loading agents or writing files."""
    content = (output / "plan.json").read_bytes()
    plan_sha = _sha(content)
    if (output / "plan.sha256").read_text().strip() != plan_sha:
        raise ValueError("Evaluation plan hash mismatch")
    plan = json.loads(content)
    return _merge(output, plan, plan_sha, _completed(output, plan, plan_sha), verify_only=True)


def evaluate_campaign(seeds: list[int], lineup: list[str], output: Path, *, resume=False,
                      shard_seed_count=25, shard_index=None, merge_only=False,
                      max_wall_seconds=None) -> dict:
    """Run or resume complete seed groups; --merge-only never starts games."""
    if type(shard_seed_count) is not int or shard_seed_count < 1:
        raise ValueError("shard-seeds must be a positive integer")
    if max_wall_seconds is not None and (type(max_wall_seconds) not in (int, float)
                                        or not math.isfinite(max_wall_seconds) or max_wall_seconds <= 0):
        raise ValueError("max-wall-seconds must be positive and finite")
    if merge_only and (not resume or shard_index is not None):
        raise ValueError("merge-only requires resume and all shards")
    if shard_index is not None and (type(shard_index) is not int or shard_index < 0
                                    or shard_index >= (len(seeds) + shard_seed_count - 1) // shard_seed_count):
        raise ValueError("Invalid zero-based shard index")
    plan, plan_sha = _plan(output, seeds, lineup, shard_seed_count, resume)
    indices = list(range(len(plan["shards"]))) if shard_index is None else [shard_index]
    started = time.monotonic()
    with ExitStack() as stack:
        for index in indices:
            stack.enter_context(_lock(output / f"shard-{index:06d}.lock"))
        completed = _completed(output, plan, plan_sha)
        if (output / "completion.json").exists() or merge_only:
            return _merge(output, plan, plan_sha, completed)
        for index in indices:
            for seed in plan["shards"][index]:
                if seed in completed:
                    continue
                if max_wall_seconds is not None and time.monotonic() - started >= max_wall_seconds:
                    return {"status": "paused", "reason": "wall_time_budget", "games": len(completed) * 4,
                            "games_expected": len(seeds) * 4, "plan_sha256": plan_sha}
                if _identity(lineup) != {key: plan[key] for key in CONDITIONS}:
                    raise ValueError("Evaluation runtime changed during campaign")
                group = output / "groups" / f"seed-{seed:020d}"
                group.mkdir(parents=True, exist_ok=True)
                numbers = [int(path.name[8:]) for path in group.iterdir()]
                attempt = group / f"attempt-{max(numbers, default=0) + 1:06d}"
                try:
                    evaluate([seed], lineup, attempt)
                    rows, latencies, hashes = _load_group(attempt, plan, seed)
                except BaseException as exc:
                    # Also retain failures before evaluate creates its manifest.
                    attempt.mkdir(exist_ok=True)
                    failure = {"schema_version": 1, "plan_sha256": plan_sha, "seed": seed,
                               "error_type": type(exc).__name__, "sha256": _partial_hashes(attempt)}
                    _atomic(attempt / "failure.json", _bytes(failure))
                    raise
                receipt = {"schema_version": 1, "plan_sha256": plan_sha, "seed": seed, "sha256": hashes}
                _atomic(attempt / "receipt.json", _bytes(receipt))
                completed[seed] = (rows, latencies,
                                   {"path": str(attempt.relative_to(output)),
                                    "receipt_sha256": _sha(_bytes(receipt)), "sha256": hashes})
        # A selected shard never merges while another worker might be writing.
        if shard_index is not None:
            return {"status": "shard_complete", "shard_index": shard_index,
                    "games": len(completed) * 4, "games_expected": len(seeds) * 4,
                    "plan_sha256": plan_sha}
        # Recheck receipts and hashes before publishing the merged report.
        return _merge(output, plan, plan_sha, _completed(output, plan, plan_sha))
