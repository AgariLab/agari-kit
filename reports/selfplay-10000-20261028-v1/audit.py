"""Archive-only audit. Does not construct/replay an engine or inspect splits."""
from __future__ import annotations

import collections
import gzip
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

ROOT = Path("/Volumes/新加卷/AgariLab")
OUT = ROOT / "runs/selfplay-10000-v1-audit"
DATASET = "selfplay-10000-v1"
META = ROOT / "datasets/manifests" / DATASET
RAW = ROOT / "datasets/raw" / DATASET
NORMAL = ROOT / "datasets/mjai" / DATASET
QUARANTINE = ROOT / "datasets/quarantine" / DATASET
REPO = Path("/Users/zhou/MyCodes/AgariLab/agari-kit")
SOURCE = "e1991979d79a373ca54050786616f57ded571a8d20de326a6e66982244dd1979"
ENGINE = "169c8c2c53bdace01e4ca319f54050565fe728a5b1b2fc6b7f3046738887071d"
COMMIT = "9e3e866a046e40fecf94d03d315c9cc000d015b1"
EXPECTED = 10000
SEED_START = 500000
CHECKS = {"action_legality", "all_mjai_events", "complete_event_structure", "final_scores",
          "observation_hashes", "player_information_boundary", "tile_counts"}

def check(condition, message):
    if not condition:
        raise ValueError(message)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(data):
    return hashlib.sha256(data).hexdigest()

def read_stable(path):
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    check((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
          f"File changed while reading: {path}")
    return data

def record_bytes(path, data):
    return {"path": path.relative_to(ROOT).as_posix(), "bytes": len(data), "sha256": digest(data)}

def load_lines(data, label):
    plain = gzip.decompress(data)
    check(plain.endswith(b"\n"), f"Missing terminal newline: {label}")
    lines = plain.splitlines()
    check(lines and all(lines), f"Empty event/trace lines: {label}")
    return lines, len(plain)

def main():
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    # Never overwrite a prior audit, partial manifest, or result.
    for name in ("report.json", "failure.json", "artifacts.jsonl", "raw-sha256.jsonl", "started.json"):
        check(not (OUT / name).exists(), f"Audit output already exists: {name}")
    script_hash = digest(Path(__file__).read_bytes())
    (OUT / "started.json").write_text(json.dumps({
        "started_at": started_at, "audit_script_sha256": script_hash, "dataset": DATASET,
        "mode": "archive consistency only; no native replay; no split contents read"
    }, indent=2) + "\n")
    totals = collections.Counter()
    try:
        inputs = {name: read_stable(META / name)
                  for name in ("acquisition.json", "catalog.sqlite", "metadata.parquet")}
        input_records = {name: record_bytes(META / name, data) for name, data in inputs.items()}
        acquisition = json.loads(inputs["acquisition.json"])
        config = acquisition["config"]
        expected_code = {"package": "agari-kit", "version": "0.1.0", "source_sha256": SOURCE}
        expected_engine = {"version": "0.4.10", "binary_sha256": ENGINE}
        expected_rules = {"engine_version": "0.4.10", "game_mode": "4p-red-half", "preset": "tenhou"}
        expected_provenance = [{"kind": "builtin", "name": "efficiency"}] * 4
        check(acquisition["status"] == "complete" and acquisition["added"] == EXPECTED
              and acquisition["reused"] == 0, "Acquisition is not the expected completed run")
        check(config["games"] == EXPECTED and config["seed_start"] == SEED_START
              and config["code"] == expected_code and config["engine"] == expected_engine
              and config["rules"] == expected_rules and config["agents"] == ["efficiency"] * 4
              and config["agent_provenance"] == expected_provenance, "Acquisition identity mismatch")
        revision = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
        check(revision == COMMIT, "Kit checkout is not the authorized producer commit")
        source_digest = hashlib.sha256()
        for path in sorted((REPO / "src/agari_kit").rglob("*.py")):
            source_digest.update(path.relative_to(REPO / "src/agari_kit").as_posix().encode() + b"\0")
            source_digest.update(path.read_bytes() + b"\0")
        check(source_digest.hexdigest() == SOURCE, "Current kit source differs from producer identity")
        validator_sha = digest((REPO / "src/agari_kit/validation.py").read_bytes())

        connection = sqlite3.connect("file:" + str(META / "catalog.sqlite") + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        check(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failure")
        rows = [dict(row) for row in connection.execute("SELECT * FROM games ORDER BY game_id")]
        connection.close()
        parquet_rows = pl.read_parquet(META / "metadata.parquet").to_dicts()
        check(len(rows) == len(parquet_rows) == EXPECTED, "Catalog/Parquet row count mismatch")
        check(sorted(parquet_rows, key=lambda row: row["game_id"]) == rows,
              "SQLite and Parquet differ in one or more fields")
        columns = list(rows[0])
        check(all(set(row) == set(columns) for row in rows), "Inconsistent catalog fields")
        for field in ("game_id", "sha256", "mjai_path", "trace_path", "validation_receipt"):
            check(len({row[field] for row in rows}) == EXPECTED, f"Nonunique catalog field: {field}")

        stems = {f"game-{index:06d}" for index in range(EXPECTED)}
        raw_names = {p.name for p in RAW.iterdir() if p.is_file() and p.name.endswith(".jsonl.gz")
                     and not p.name.endswith(".trace.jsonl.gz")}
        trace_names = {p.name for p in RAW.iterdir() if p.is_file() and p.name.endswith(".trace.jsonl.gz")}
        normal_names = {p.name for p in NORMAL.iterdir() if p.is_file()}
        metadata_names = {p.name for p in META.glob("game-*.json") if not p.name.endswith(".verification.json")}
        receipt_names = {p.name for p in META.glob("game-*.verification.json")}
        check(raw_names == {stem + ".jsonl.gz" for stem in stems}, "Raw inventory mismatch")
        check(trace_names == {stem + ".trace.jsonl.gz" for stem in stems}, "Trace inventory mismatch")
        check(normal_names == {stem + ".jsonl.gz" for stem in stems}, "Normalized inventory mismatch")
        check(metadata_names == {stem + ".json" for stem in stems}, "Metadata inventory mismatch")
        check(receipt_names == {stem + ".verification.json" for stem in stems}, "Receipt inventory mismatch")
        quarantine_entries = [p.relative_to(QUARANTINE).as_posix() for p in QUARANTINE.rglob("*")]
        check(not quarantine_entries, "Quarantine is not empty")

        hashes_seen = {kind: set() for kind in ("raw", "trace", "normalized")}
        receipt_identities = collections.Counter()
        score_sums = collections.Counter()
        score_totals = [0] * 4
        event_types = collections.Counter()
        seen_indices = set()
        rows.sort(key=lambda row: row["mjai_path"])
        with (OUT / "artifacts.jsonl").open("x") as artifacts, (OUT / "raw-sha256.jsonl").open("x") as raw_manifest:
            for number, row in enumerate(rows, 1):
                match = re.fullmatch(r"datasets/mjai/selfplay-10000-v1/game-(\d{6})\.jsonl\.gz", row["mjai_path"])
                check(match is not None, "Unexpected MJAI path")
                index = int(match[1])
                check(0 <= index < EXPECTED and index not in seen_indices, "Duplicate/out-of-range index")
                seen_indices.add(index)
                stem = f"game-{index:06d}"
                paths = {"raw": RAW / (stem + ".jsonl.gz"), "trace": RAW / (stem + ".trace.jsonl.gz"),
                         "normalized": NORMAL / (stem + ".jsonl.gz"),
                         "receipt": META / (stem + ".verification.json"), "metadata": META / (stem + ".json")}
                check(row["trace_path"] == paths["trace"].relative_to(ROOT).as_posix()
                      and row["validation_receipt"] == paths["receipt"].relative_to(ROOT).as_posix(),
                      f"{stem}: catalog path mismatch")
                data = {kind: read_stable(path) for kind, path in paths.items()}
                files = {kind: record_bytes(paths[kind], value) for kind, value in data.items()}
                receipt = json.loads(data["receipt"])
                check(json.loads(data["metadata"]) == row, f"{stem}: metadata differs from catalog")
                check(row["game_id"] == "selfplay:" + row["sha256"]
                      and row["source"] == "agarilab-selfplay"
                      and row["rules"] == "4p-red-half/tenhou" and row["validation"] == "engine-verified",
                      f"{stem}: catalog identity mismatch")
                check(files["receipt"]["sha256"] == row["validation_receipt_sha256"],
                      f"{stem}: receipt file hash mismatch")
                check(receipt["sha256"] == row["sha256"]
                      and receipt["mjai_file_sha256"] == files["normalized"]["sha256"]
                      and receipt["trace_sha256"] == files["trace"]["sha256"], f"{stem}: content binding mismatch")
                check(receipt["schema_version"] == 1 and receipt["seed"] == SEED_START + index
                      and receipt["validation"] == "engine-verified" and receipt["method"] == "seeded-action-trace-v1"
                      and receipt["rules"] == expected_rules and receipt["producer_code"] == expected_code
                      and receipt["engine_version"] == expected_engine["version"]
                      and receipt["engine_binary_sha256"] == ENGINE
                      and receipt["validator_source_sha256"] == validator_sha
                      and set(receipt["checks"]) == CHECKS
                      and all(value is True for value in receipt["checks"].values()),
                      f"{stem}: verification identity/check mismatch")

                raw_lines, raw_plain_bytes = load_lines(data["raw"], stem + " raw")
                norm_lines, norm_plain_bytes = load_lines(data["normalized"], stem + " normalized")
                check(len(raw_lines) == len(norm_lines) and raw_lines[:-1] == norm_lines[:-1],
                      f"{stem}: conversion altered a nonterminal event")
                raw_end, norm_end = json.loads(raw_lines[-1]), json.loads(norm_lines[-1])
                check(raw_end.get("type") == norm_end.get("type") == "end_game"
                      and "scores" not in raw_end
                      and norm_end == dict(raw_end, scores=receipt["scores"]),
                      f"{stem}: conversion differs beyond terminal score addition")
                canonical_digest = hashlib.sha256()
                rounds = ends = 0
                in_round = False
                for event_index, line in enumerate(norm_lines):
                    event = json.loads(line)
                    check(isinstance(event, dict) and isinstance(event.get("type"), str),
                          f"{stem}: invalid event shape")
                    canonical_line = canonical(event).encode()
                    check(line == canonical_line, f"{stem}: noncanonical event encoding")
                    canonical_digest.update(canonical_line + b"\n")
                    kind = event["type"]
                    event_types[kind] += 1
                    if event_index == 0:
                        check(kind == "start_game", f"{stem}: missing start_game")
                    elif kind == "start_game":
                        raise ValueError(f"{stem}: duplicate start_game")
                    if kind == "start_kyoku":
                        check(not in_round, f"{stem}: overlapping rounds")
                        rounds += 1
                        in_round = True
                    elif kind == "end_kyoku":
                        check(in_round, f"{stem}: orphan end_kyoku")
                        ends += 1
                        in_round = False
                    elif kind == "end_game":
                        check(event_index == len(norm_lines) - 1 and rounds and not in_round,
                              f"{stem}: incomplete/trailing game events")
                check(canonical_digest.hexdigest() == row["sha256"]
                      and len(norm_lines) == receipt["events"] and rounds == ends == receipt["rounds"],
                      f"{stem}: canonical digest/event statistics mismatch")
                scores = receipt["scores"]
                check(isinstance(scores, list) and len(scores) == 4 and all(type(s) is int for s in scores),
                      f"{stem}: invalid terminal scores")

                trace_lines, trace_plain_bytes = load_lines(data["trace"], stem + " trace")
                header, footer = json.loads(trace_lines[0]), json.loads(trace_lines[-1])
                check(header == {"agent_provenance": expected_provenance, "agents": ["efficiency"] * 4,
                                 "code": expected_code, "rules": expected_rules, "schema_version": 1,
                                 "seed": SEED_START + index, "type": "agari_trace"},
                      f"{stem}: trace producer/rule/seed mismatch")
                decisions = 0
                for step, line in enumerate(trace_lines[1:-1]):
                    record = json.loads(line)
                    actions, observations = record.get("actions"), record.get("observations_sha256")
                    check(record.get("step") == step and isinstance(actions, dict) and actions
                          and isinstance(observations, dict) and set(actions) == set(observations)
                          and set(actions) <= {"0", "1", "2", "3"},
                          f"{stem}: noncontiguous or inconsistent trace step")
                    check(all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                              for value in observations.values()), f"{stem}: malformed observation hash")
                    decisions += len(actions)
                steps = len(trace_lines) - 2
                check(footer == {"scores": scores, "steps": steps, "type": "trace_end"}
                      and steps == receipt["steps"] and decisions == receipt["decisions"],
                      f"{stem}: trace footer/counts/scores mismatch")

                for kind, file_record in files.items():
                    totals[kind + "_files"] += 1
                    totals[kind + "_bytes"] += file_record["bytes"]
                    if kind in hashes_seen:
                        hashes_seen[kind].add(file_record["sha256"])
                totals.update(games=1, rounds=rounds, events=len(norm_lines), steps=steps, decisions=decisions,
                              raw_conversion_passed=1, receipt_binding_passed=1,
                              raw_uncompressed_bytes=raw_plain_bytes, normalized_uncompressed_bytes=norm_plain_bytes,
                              trace_uncompressed_bytes=trace_plain_bytes)
                score_sums[sum(scores)] += 1
                score_totals = [a + b for a, b in zip(score_totals, scores)]
                receipt_identities[canonical({key: receipt[key] for key in (
                    "engine_version", "engine_binary_sha256", "producer_code", "rules", "validator_source_sha256",
                    "method", "validation")})] += 1
                game_report = {"index": index, "seed": SEED_START + index, "game_id": row["game_id"],
                               "canonical_mjai_sha256": row["sha256"], "rounds": rounds, "events": len(norm_lines),
                               "steps": steps, "decisions": decisions, "scores": scores, "files": files,
                               "raw_to_normalized": "identical prefix; terminal end_game adds receipt scores only"}
                artifacts.write(canonical(game_report) + "\n")
                raw_manifest.write(canonical({"game_id": row["game_id"], "seed": SEED_START + index,
                                              **files["raw"]}) + "\n")
                if number % 500 == 0:
                    artifacts.flush()
                    raw_manifest.flush()
                    print(canonical({"audited": number, "games": EXPECTED,
                                     "elapsed_seconds": round(time.monotonic() - started, 3)}), flush=True)

        check(seen_indices == set(range(EXPECTED)), "Incomplete index coverage")
        check(all(len(values) == EXPECTED for values in hashes_seen.values()), "Duplicate archive content")
        check(len(receipt_identities) == 1, "Mixed engine/producer/validator identity")
        for name, initial_data in inputs.items():
            check(digest(read_stable(META / name)) == digest(initial_data), f"Input changed during audit: {name}")
        check(digest(Path(__file__).read_bytes()) == script_hash, "Audit script changed during run")
        report = {"schema_version": 1, "status": "passed", "dataset": DATASET, "started_at": started_at,
                  "completed_at": datetime.now(timezone.utc).isoformat(),
                  "elapsed_seconds": time.monotonic() - started, "audit_script_sha256": script_hash,
                  "scope": {"native_replay_performed": False, "split_contents_read": False,
                            "training_performed": False,
                            "note": "All-game archival QA. Parent separately performs official native replay and split freeze."},
                  "producer_commit_checked": revision, "acquisition": acquisition, "inputs": input_records,
                  "sqlite_integrity": "ok", "sqlite_parquet_all_fields_equal": True,
                  "catalog_fields": columns, "unique_records": EXPECTED, "unique_game_ids": EXPECTED,
                  "unique_canonical_mjai_hashes": EXPECTED,
                  "unique_compressed_hashes": {kind: len(values) for kind, values in hashes_seen.items()},
                  "seed_range": [SEED_START, SEED_START + EXPECTED - 1],
                  "totals": dict(totals), "quarantine_entries": quarantine_entries,
                  "quarantine_count": len(quarantine_entries),
                  "receipt_identity_counts": [{"identity": json.loads(identity), "games": count}
                                              for identity, count in receipt_identities.items()],
                  "score_sum_distribution": dict(score_sums), "final_scores_total_by_seat": score_totals,
                  "event_type_counts": dict(event_types),
                  "raw_conversion_rule": "Every nonterminal raw line equals normalized bytes; only last end_game gains receipt/trace-end scores.",
                  "evidence": {name: record_bytes(OUT / name, (OUT / name).read_bytes())
                               for name in ("artifacts.jsonl", "raw-sha256.jsonl")}}
        with (OUT / "report.json").open("x") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(canonical({"status": "passed", "elapsed_seconds": report["elapsed_seconds"],
                         "totals": dict(totals), "report": str(OUT / "report.json")}), flush=True)
    except BaseException as exc:
        failure = {"status": "failed", "exception": type(exc).__name__, "message": str(exc),
                   "elapsed_seconds": time.monotonic() - started, "partial_totals": dict(totals),
                   "traceback": traceback.format_exc(), "audit_script_sha256": script_hash}
        with (OUT / "failure.json").open("x") as stream:
            stream.write(json.dumps(failure, ensure_ascii=False, indent=2) + "\n")
        raise

if __name__ == "__main__":
    main()
