#!/usr/bin/env python3
"""Small native half-game probe, including initialization and exact trace replay."""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import statistics
import time

from agari_kit.agent import Context, Rules, ensure_legal, managed_agents
from agari_kit.collection import compressed_writer, write_events
from agari_kit.engine import engine_identity, new_environment
from agari_kit.mjai import canonical_json
from agari_kit.plugins import agent_provenance
from agari_kit.provenance import source_fingerprint
from agari_kit.storage import Storage
from agari_kit.validation import validate_mjai


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--games", type=int, choices=range(1, 17), default=4)
    args = parser.parse_args()
    storage = Storage.from_env()
    storage.check(probe=True)
    output = storage.path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    records = []
    report = {"schema_version": 1, "status": "running", "games": records,
              "engine": engine_identity(), "code_before": source_fingerprint(),
              "rules": asdict(Rules()), "teacher_provenance": agent_provenance("@" + str(args.agent.resolve())),
              "purpose": "Protocol/throughput probe, not a strength estimate"}
    try:
        for index in range(args.games):
            seed, seat = args.seed + index, index % 4
            names = ["efficiency"] * 4
            names[seat] = "@" + str(args.agent.resolve())
            provenance = [agent_provenance(name) for name in names]
            before = time.monotonic()
            with managed_agents(names) as agents:
                constructed = time.monotonic()
                contexts = [Context(player, seed) for player in range(4)]
                for agent, context in zip(agents, contexts):
                    agent.reset(context)
                reset = time.monotonic()
                env = new_environment(seed)
                observations = env.reset()
                latencies = []
                steps = decisions = 0
                trace = output / f"game-{index:03d}.trace.jsonl.gz"
                with compressed_writer(trace) as stream:
                    stream.write(canonical_json({"schema_version": 1, "type": "agari_trace", "seed": seed,
                                                 "rules": asdict(Rules()), "agents": names,
                                                 "agent_provenance": provenance,
                                                 "code": source_fingerprint()}) + "\n")
                    while not env.done():
                        if not observations or steps >= 100000:
                            raise RuntimeError("Native game stalled")
                        selected = {}
                        for player, observation in observations.items():
                            act_started = time.perf_counter()
                            selected[player] = ensure_legal(agents[player].act(observation, contexts[player]), observation)
                            if player == seat:
                                latencies.append((time.perf_counter() - act_started) * 1000)
                        stream.write(canonical_json({"step": steps,
                            "actions": {str(player): action.to_dict() for player, action in selected.items()},
                            "observations_sha256": {str(player): hashlib.sha256(obs.serialize_to_base64().encode()).hexdigest()
                                                     for player, obs in observations.items()}}) + "\n")
                        observations = env.step(selected)
                        steps += 1
                        decisions += len(selected)
                    scores = list(env.scores())
                    stream.write(canonical_json({"type": "trace_end", "steps": steps, "scores": scores}) + "\n")
                played = time.monotonic()
                events = list(env.mjai_log)
                write_events(output / f"game-{index:03d}.raw.jsonl.gz", events)
                events[-1] = dict(events[-1], scores=scores)
                mjai = output / f"game-{index:03d}.mjai.jsonl.gz"
                write_events(mjai, events)
                verified = validate_mjai(mjai, trace_path=trace)
                latencies.sort()
                records.append({"seed": seed, "seat": seat, "scores": scores, "decisions": decisions,
                    "candidate_decisions": len(latencies), "illegal_actions": 0,
                    "event_counts": dict(Counter(event["type"] for event in events)),
                    "construction_seconds": constructed - before, "reset_seconds": reset - constructed,
                    "game_seconds": played - reset, "total_seconds_with_validation": time.monotonic() - before,
                    "worker_initialization_seconds": agents[seat].initialization_seconds,
                    "artifact_and_reset_timings": agents[seat].timings,
                    "candidate_latency_ms_mean": statistics.fmean(latencies),
                    "candidate_latency_ms_p95": latencies[min(len(latencies)-1, int(.95 * len(latencies)))],
                    "candidate_latency_ms_max": max(latencies), "validation": verified,
                    "worker_stderr": agents[seat].stderr_path})
            records[-1]["total_seconds_with_cleanup"] = time.monotonic() - before
            print(json.dumps({"game": index, "status": "passed", "scores": scores,
                              "seconds": records[-1]["total_seconds_with_cleanup"]}), flush=True)
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        report["code_after"] = source_fingerprint()
        report["teacher_provenance_after"] = agent_provenance("@" + str(args.agent.resolve()))
        if (report["code_before"] != report["code_after"]
                or report["teacher_provenance"] != report["teacher_provenance_after"]):
            report["status"] = "failed"
            report["failure"] = {"type": "IdentityChanged", "message": "Code or teacher identity changed during probe"}
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
