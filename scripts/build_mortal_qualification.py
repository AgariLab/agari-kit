#!/usr/bin/env python3
"""Bind real S42 protocol probes to one teacher/runtime/engine identity."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from agari_kit.agent import Rules, managed_agents
from agari_kit.engine import engine_identity
from agari_kit.mjai import canonical_json
from agari_kit.plugins import agent_provenance
from agari_kit.provenance import source_fingerprint
from agari_kit.storage import Storage

REQUIRED_CASES = {
    "test_ankan_dora_rinshan", "test_kakan_delayed_dora_chankan", "test_kyushu_decision_and_new_hand",
    "test_reach_followup_discard", "test_red_five_chi_followup_discard",
    "test_red_five_pon_followup_discard", "test_two_ron_events_then_next_hand",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--native-report", required=True, help="Storage-relative JSON")
    parser.add_argument("--games-report", required=True, help="Storage-relative JSON")
    parser.add_argument("--output", required=True, help="New storage-relative directory")
    args = parser.parse_args()
    storage = Storage.from_env()
    storage.check(probe=True)
    name = "@" + str(args.agent.resolve())
    with managed_agents([name]):
        pass  # Includes full runtime file verification, without starting the model.
    teacher = agent_provenance(name)
    engine, rules = engine_identity(), asdict(Rules())
    native_path = storage.path(args.native_report)
    games_path = storage.path(args.games_report)
    native, games = (json.loads(path.read_text()) for path in (native_path, games_path))
    for report in (native, games):
        if (report.get("status") != "passed" or report.get("teacher_provenance") != teacher
                or report.get("teacher_provenance_after") != teacher
                or report.get("engine") != engine or report.get("rules") != rules):
            raise ValueError("Probe failed or teacher/runtime/rules identity differs")
    if set(native["case_status"]) != REQUIRED_CASES or not all(value is True for value in native["case_status"].values()):
        raise ValueError("Missing required real-runtime boundary scenarios")
    kit = Path(__file__).resolve().parents[1]
    for field, path in (("fixture_sha256", kit / "tests/integration/test_rules.py"),
                        ("test_sha256", kit / "tests/integration/test_mortal_runtime_native.py")):
        if native[field] != digest(path):
            raise ValueError("Native test source differs from executed probe")
    if (games.get("code_before") != source_fingerprint() or games.get("code_after") != source_fingerprint()
            or len(games["games"]) != 4 or {row["seat"] for row in games["games"]} != set(range(4))):
        raise ValueError("Four-seat probe is incomplete or code changed")
    checks = []
    def evidence(label, path):
        checks.append({"name": label, "status": "passed",
                       "evidence_path": str(path.relative_to(storage.root)), "evidence_sha256": digest(path)})
    evidence("four-seat-native-half-games", games_path)
    for case in sorted(REQUIRED_CASES):
        evidence(case, native_path)
    for index, row in enumerate(games["games"]):
        receipt = row["validation"]
        if row["illegal_actions"] or receipt["validation"] != "engine-verified":
            raise ValueError("Four-seat game lacks successful native verification")
        for suffix, field in (("mjai.jsonl.gz", "mjai_file_sha256"), ("trace.jsonl.gz", "trace_sha256")):
            path = games_path.parent / f"game-{index:03d}.{suffix}"
            if digest(path) != receipt[field]:
                raise ValueError("Four-seat native evidence changed")
            evidence(f"seat-{row['seat']}-{suffix}", path)
    report = {"schema_version": 1, "kind": "mortal-protocol-qualification-v1", "status": "passed",
              "teacher_provenance": teacher,
              "teacher_provenance_sha256": hashlib.sha256(canonical_json(teacher).encode()).hexdigest(),
              "engine": engine, "rules": rules, "checks": checks,
              "limits": ["Protocol and rule compatibility only; not a playing-strength qualification",
                         "Pinned Tenhou rules only; no model or runtime policy patch",
                         "Full dependency content checked at construction; dependency stats checked at game boundaries",
                         "Per-decision identity guard covers the published runtime bundle, not all dependency files"]}
    output = storage.path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    target = output / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": "passed", "report": str(target), "sha256": digest(target), "checks": len(checks)}))


if __name__ == "__main__":
    main()
