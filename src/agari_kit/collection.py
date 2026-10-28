"""Resumable self-play acquisition, raw preservation, validation and frozen datasets."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .agent import Context, Rules, ensure_legal, managed_agents
from .dataset import Catalog, freeze_split, make_split
from .engine import new_environment, require_engine, engine_identity
from .mjai import DataError, canonical_json, inspect_game, read_events
from .plugins import agent_provenance
from .provenance import source_fingerprint
from .storage import Storage


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: dict) -> None:
    """Replace a small manifest atomically within its existing filesystem."""
    fd, temporary = tempfile.mkstemp(prefix=".agari-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(data) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def compressed_writer(path: Path):
    fd, temporary = tempfile.mkstemp(prefix=".agari-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as stream:
                    yield stream
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_events(path: Path, events) -> None:
    with compressed_writer(path) as stream:
        for event in events:
            stream.write(canonical_json(event) + "\n")


def record_game(*, seed: int, agents: list[str], raw_path: Path, mjai_path: Path,
                trace_path: Path, max_steps: int = 100000,
                producer_code: dict | None = None, provenance: list[dict] | None = None) -> dict:
    """One bounded game at a time; policies never receive the environment."""
    if len(agents) != 4 or type(seed) is not int or not 0 <= seed < 2**64:
        raise DataError("Use four agents and an unsigned 64-bit seed")
    producer_code = source_fingerprint() if producer_code is None else producer_code
    provenance = [agent_provenance(name) for name in agents] if provenance is None else provenance
    env = new_environment(seed)
    with managed_agents(agents) as policies:
        if [agent_provenance(name) for name in agents] != provenance:
            raise DataError("Agent configuration, weights or source changed while loading")
        contexts = [Context(seat, seed) for seat in range(4)]
        for policy, context in zip(policies, contexts):
            policy.reset(context)
        observations = env.reset()
        step = decisions = 0
        with compressed_writer(trace_path) as stream:
            stream.write(canonical_json({"schema_version": 1, "type": "agari_trace", "seed": seed,
                                         "rules": asdict(Rules()), "agents": agents,
                                         "agent_provenance": provenance,
                                         "code": producer_code}) + "\n")
            while not env.done():
                if not observations or step >= max_steps:
                    raise DataError("Self-play stalled or exceeded the step limit")
                observation_hashes = {str(seat): hashlib.sha256(obs.serialize_to_base64().encode()).hexdigest()
                                      for seat, obs in observations.items()}
                selected = {seat: ensure_legal(policies[seat].act(obs, contexts[seat]), obs)
                            for seat, obs in observations.items()}
                stream.write(canonical_json({"step": step,
                                             "actions": {str(seat): action.to_dict() for seat, action in selected.items()},
                                             "observations_sha256": observation_hashes}) + "\n")
                observations = env.step(selected)
                step += 1
                decisions += len(selected)
            scores = list(env.scores())
            stream.write(canonical_json({"type": "trace_end", "steps": step, "scores": scores}) + "\n")
        events = list(env.mjai_log)
        # Preserve the engine log verbatim, then normalize end-game scores from
        # the terminal engine state. The raw file is never overwritten by conversion.
        write_events(raw_path, events)
        if not events or events[-1].get("type") != "end_game":
            raise DataError("Engine did not emit end_game")
        events[-1] = dict(events[-1], scores=scores)
        write_events(mjai_path, events)
        return {"steps": step, "decisions": decisions, "scores": scores}


def verify_record(storage: Storage, record: dict, *, replay: bool = True) -> dict:
    """Bind catalog metadata to actual artifacts, optionally re-running the engine."""
    from .validation import validate_mjai

    for field in ("mjai_path", "trace_path", "validation_receipt", "validation_receipt_sha256"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise DataError(f"Missing verified artifact field: {field}")
    mjai = storage.path(record["mjai_path"])
    trace = storage.path(record["trace_path"])
    receipt_path = storage.path(record["validation_receipt"])
    if file_sha256(receipt_path) != record["validation_receipt_sha256"]:
        raise DataError(f"Validation receipt changed: {record['game_id']}")
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("validation") != "engine-verified" or receipt.get("sha256") != record["sha256"]
            or receipt.get("mjai_file_sha256") != file_sha256(mjai)
            or receipt.get("trace_sha256") != file_sha256(trace)):
        raise DataError(f"Artifact hashes disagree: {record['game_id']}")
    if inspect_game(read_events(mjai))["sha256"] != record["sha256"]:
        raise DataError(f"Canonical MJAI hash changed: {record['game_id']}")
    if replay:
        checked = validate_mjai(mjai, trace_path=trace)
        if checked["validation"] != "engine-verified" or checked["sha256"] != record["sha256"]:
            raise DataError(f"Engine verification failed: {record['game_id']}")
    return receipt


def verified_split(storage: Storage, catalog_path: Path, output_path: Path) -> dict:
    with Catalog(catalog_path) as catalog:
        games = catalog.games()
    for game in games:
        verify_record(storage, game, replay=True)
    manifest = make_split(games)
    freeze_split(output_path, manifest)
    return {"output": str(output_path), "games": len(games), "dataset_sha256": manifest["dataset_sha256"]}


def export_metadata(catalog_path: Path, output_path: Path) -> None:
    import polars as pl

    with Catalog(catalog_path) as catalog:
        records = catalog.games()
    fd, temporary = tempfile.mkstemp(prefix=".agari-parquet-", dir=output_path.parent)
    os.close(fd)
    try:
        pl.from_dicts(records).write_parquet(temporary, compression="zstd")
        os.replace(temporary, output_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect_selfplay(storage: Storage, *, name: str, games: int, seed_start: int,
                     agents: list[str], progress=None, max_wall_seconds: float | None = None) -> dict:
    from .validation import validate_mjai

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
        raise DataError("Dataset name must be a simple path-safe identifier")
    if type(games) is not int or games < 1 or seed_start < 0 or seed_start + games > 2**64:
        raise DataError("Invalid game count or seed range")
    if len(agents) != 4:
        raise DataError("Four agent names are required")
    if max_wall_seconds is not None and (type(max_wall_seconds) not in (int, float)
            or not math.isfinite(max_wall_seconds) or max_wall_seconds <= 0):
        raise DataError("max_wall_seconds must be finite and positive")
    started = time.monotonic()
    require_engine()
    storage.initialize()
    raw_dir = storage.path(f"datasets/raw/{name}")
    mjai_dir = storage.path(f"datasets/mjai/{name}")
    meta_dir = storage.path(f"datasets/manifests/{name}")
    quarantine = storage.path(f"datasets/quarantine/{name}")
    for path in (raw_dir, mjai_dir, meta_dir, quarantine):
        path.mkdir(parents=True, exist_ok=True)
    config = {"schema_version": 1, "source": "agarilab-selfplay", "games": games,
              "seed_start": seed_start, "agents": agents, "rules": asdict(Rules()),
              "code": source_fingerprint(),
              "agent_provenance": [agent_provenance(name) for name in agents],
              "engine": engine_identity()}
    catalog_path = meta_dir / "catalog.sqlite"
    run_path = meta_dir / "acquisition.json"
    added = reused = 0
    with (meta_dir / ".collection.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataError("Another collector is already writing this dataset") from exc
        if run_path.exists():
            run = json.loads(run_path.read_text())
            if run["config"] != config:
                raise DataError("Dataset configuration changed; select a new dataset version")
        else:
            run = {"config": config, "started_at": datetime.now(timezone.utc).isoformat()}
        run["status"] = "running"
        run.pop("failure", None)
        run.pop("completed_at", None)
        write_json(run_path, run)
        try:
            with Catalog(catalog_path) as catalog:
                for index in range(games):
                    # The execution budget is not part of the dataset identity.
                    # Finish and verify an entire game before stopping; a later
                    # invocation revalidates existing games and resumes the rest.
                    if max_wall_seconds is not None and time.monotonic() - started >= max_wall_seconds:
                        run["status"] = "paused"
                        break
                    seed = seed_start + index
                    stem = f"game-{index:06d}"
                    raw_path, mjai_path = raw_dir / f"{stem}.jsonl.gz", mjai_dir / f"{stem}.jsonl.gz"
                    trace_path = raw_dir / f"{stem}.trace.jsonl.gz"
                    receipt_path, metadata_path = meta_dir / f"{stem}.verification.json", meta_dir / f"{stem}.json"
                    storage.check()
                    if source_fingerprint() != config["code"]:
                        raise DataError("Producer source changed during collection; use a new dataset version")
                    if engine_identity() != config["engine"]:
                        raise DataError("Engine binary changed during collection")
                    if [agent_provenance(name) for name in agents] != config["agent_provenance"]:
                        raise DataError("Agent configuration, weights or source changed during collection")
                    if metadata_path.exists():
                        record = json.loads(metadata_path.read_text())
                        verify_record(storage, record, replay=True)
                        catalog.register(**record)
                        reused += 1
                    else:
                        played_at = datetime.now(timezone.utc).isoformat()
                        record_game(seed=seed, agents=agents, raw_path=raw_path,
                                    mjai_path=mjai_path, trace_path=trace_path,
                                    producer_code=config["code"], provenance=config["agent_provenance"])
                        receipt = validate_mjai(mjai_path, trace_path=trace_path)
                        if receipt["validation"] != "engine-verified":
                            raise DataError("Self-play did not receive full engine verification")
                        write_json(receipt_path, receipt)
                        relative = lambda path: path.relative_to(storage.root).as_posix()
                        record = {"game_id": "selfplay:" + receipt["sha256"], "sha256": receipt["sha256"],
                                  "played_at": played_at, "source": "agarilab-selfplay", "rules": "4p-red-half/tenhou",
                                  "usage_basis": "Locally generated self-play; no third-party replay redistribution",
                                  "validation": "engine-verified", "mjai_path": relative(mjai_path),
                                  "trace_path": relative(trace_path), "validation_receipt": relative(receipt_path),
                                  "validation_receipt_sha256": file_sha256(receipt_path)}
                        write_json(metadata_path, record)
                        catalog.register(**record)
                        added += 1
                    if progress is not None and ((index + 1) % 10 == 0 or index + 1 == games):
                        progress({"dataset": name, "completed": index + 1, "games": games})
            if added + reused:
                export_metadata(catalog_path, meta_dir / "metadata.parquet")
            if run["status"] != "paused":
                run["status"] = "complete"
                run["completed_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            run["status"] = "failed"
            run["failure"] = {"index": locals().get("index"), "error_type": type(exc).__name__, "reason": str(exc)}
            failure_dir = quarantine / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            failure_dir.mkdir()
            for path in (locals().get("raw_path"), locals().get("mjai_path"), locals().get("trace_path")):
                if path is not None and path.exists():
                    # Preserve the input and isolate copies for investigation.
                    prefix = "normalized-" if path.parent == mjai_dir else "raw-"
                    shutil.copyfile(path, failure_dir / (prefix + path.name))
            write_json(failure_dir / "failure.json", run["failure"])
            raise
        finally:
            run["added"], run["reused"] = added, reused
            run["last_invocation"] = {"max_wall_seconds": max_wall_seconds,
                                      "elapsed_seconds": time.monotonic() - started,
                                      "verified_games": added + reused}
            write_json(run_path, run)
    return {"dataset": name, "games": games, "added": added, "reused": reused,
            "catalog": str(catalog_path), "status": run["status"]}
