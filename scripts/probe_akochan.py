#!/usr/bin/env python3
"""Probe an original Akochan build with one real, player-visible observation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from agari_kit.agent import is_tsumogiri
from agari_kit.engine import new_environment
from agari_kit.mjai import canonical_json, player_event
from agari_kit.storage import Storage


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", required=True, help="New directory relative to artifact storage")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    storage = Storage.from_env()
    storage.check()
    checkout = args.checkout.resolve()
    if storage.root.resolve() not in checkout.parents:
        raise ValueError("Keep the third-party checkout on configured artifact storage")
    if args.threads < 1 or args.timeout <= 0:
        raise ValueError("threads and timeout must be positive")
    output = storage.path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    environment = new_environment(args.seed)
    observations = environment.reset()
    observation = observations[0]
    snapshot = json.loads(base64.b64decode(observation.serialize_to_base64()))
    events = [player_event(json.loads(raw), 0) for raw in snapshot["events"]]
    for event in events:
        event["can_act"] = False
        if event["type"] == "start_game":
            # Akochan's pipe expects rule fields supplied by its TCP adapter.
            event.update({"kyoku_first": 0, "aka_flag": True})
    if events[-1]["type"] != "tsumo" or events[-1]["actor"] != 0:
        raise RuntimeError("Probe requires the initial dealer draw")
    events[-1]["can_act"] = True
    payload = "".join(canonical_json(event) + "\n" for event in events)
    (output / "input.jsonl").write_text(payload)
    binary = checkout / "system.exe"
    settings = checkout / "setup_mjai.json"
    env = os.environ.copy()
    env.update({"OMP_NUM_THREADS": str(args.threads), "OMP_THREAD_LIMIT": str(args.threads),
                "TMPDIR": str(storage.path("tmp"))})
    argv = [str(binary), "pipe", str(settings), "0"]
    started = time.monotonic()
    with (output / "stdout.jsonl").open("w") as stdout, (output / "stderr.log").open("w") as stderr:
        result = subprocess.run(argv, input=payload, text=True, cwd=checkout, env=env,
                                stdout=stdout, stderr=stderr, timeout=args.timeout)
    elapsed = time.monotonic() - started
    if result.returncode:
        raise RuntimeError(f"Akochan exited with code {result.returncode}; see {output}")
    lines = (output / "stdout.jsonl").read_text().splitlines()
    if len(lines) != 1:
        raise RuntimeError(f"Expected one pipe response, received {len(lines)} lines; see {output}")
    moves = json.loads(lines[0])
    if not isinstance(moves, list) or not moves or not all(isinstance(move, dict) for move in moves):
        raise RuntimeError("Akochan pipe must return a nonempty action array")
    matched = []
    for action in observation.legal_actions():
        native = json.loads(action.to_mjai())
        if native["type"] == "dahai":
            native["tsumogiri"] = is_tsumogiri(action, observation)
        if native == moves[0]:
            matched.append(action.to_dict())
    if not matched:
        raise RuntimeError(f"First action does not exactly match native legal MJAI: {moves[0]}")
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    report = {"schema_version": 1, "status": "passed", "seed": args.seed, "player": 0,
              "rules": {"game_mode": "4p-red-half", "preset": "tenhou"}, "kyoku_first": 0,
              "threads": args.threads, "command": argv, "binary_sha256": digest(binary),
              "settings_sha256": digest(settings), "input_sha256": digest(output / "input.jsonl"),
              "elapsed_seconds_including_process_startup": elapsed,
              "peak_child_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
              "returned_moves": moves, "strict_native_matches": matched,
              "limitations": "One initial dealer decision; not a complete-game adapter or strength result"}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
