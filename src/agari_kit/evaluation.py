"""Seat-rotated evaluation with uncertainty computed over complete seed groups."""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from array import array
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .agent import Context, Rules, ensure_legal, make_agent
from .engine import new_environment
from .plugins import agent_provenance
from .provenance import source_fingerprint


def schedule(seeds: list[int], lineup: list[str]):
    if len(lineup) != 4 or not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("Use four agents and at least one unique seed")
    if any(type(seed) is not int or seed < 0 or seed >= 2**64 for seed in seeds):
        raise ValueError("Seeds must be uint64 integers")
    for seed in seeds:
        for rotation in range(4):
            yield seed, rotation, [lineup[(seat - rotation) % 4] for seat in range(4)]


def summarize(results: list[dict], *, bootstrap_samples: int = 2000) -> dict:
    if not results or bootstrap_samples < 100:
        raise ValueError("Need results and at least 100 bootstrap samples")
    groups: dict[int, list[dict]] = {}
    for result in results:
        groups.setdefault(result["seed"], []).append(result)
    if any(len(group) != 4 or {r["rotation"] for r in group} != set(range(4)) for group in groups.values()):
        raise ValueError("Confidence intervals require all four seat rotations per seed")
    ranks = [row["ranks"][row["rotation"]] for row in results]
    deltas = [row["scores"][row["rotation"]] - 25000 for row in results]
    metrics = {"mean_rank": statistics.mean(ranks),
               "rank_rates": [ranks.count(rank) / len(ranks) for rank in range(1, 5)],
               "mean_point_delta": statistics.mean(deltas)}
    metric_names = ["mean_rank", "mean_point_delta"] + [f"rank_{rank}_rate" for rank in range(1, 5)]
    estimates = {key: [] for key in metric_names}
    group_means = [(
        statistics.mean(r["ranks"][r["rotation"]] for r in group),
        statistics.mean(r["scores"][r["rotation"]] - 25000 for r in group),
        *(statistics.mean(r["ranks"][r["rotation"]] == rank for r in group) for rank in range(1, 5))
    ) for group in groups.values()]
    if len(groups) > 1:
        rng = random.Random(0)
        for _ in range(bootstrap_samples):
            sampled = rng.choices(group_means, k=len(group_means))
            for index, key in enumerate(metric_names):
                estimates[key].append(statistics.mean(row[index] for row in sampled))
    intervals = {}
    for key, values in estimates.items():
        values.sort()
        intervals[key] = [values[int(len(values) * .025)], values[min(len(values)-1, int(len(values)*.975))]] if values else None
    return {"games": len(results), "seed_groups": len(groups), "candidate": metrics,
            "ci95": intervals, "ci_method": "seed-group-percentile-bootstrap",
            "bootstrap_seed": 0, "bootstrap_samples": bootstrap_samples,
            "promotion_sample_size_met": len(results) >= 10000}


def _candidate_latency(values) -> dict:
    ordered = sorted(values)
    if not ordered:
        raise RuntimeError("Evaluation produced no candidate decisions")
    return {"candidate_decisions": len(ordered), "candidate_latency_ms_mean": statistics.fmean(ordered),
            "candidate_latency_ms_p95": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
            "candidate_latency_ms_max": ordered[-1]}


def evaluate(seeds: list[int], lineup: list[str], output: Path, *, max_steps: int = 100000) -> dict:
    fixtures = list(schedule(seeds, lineup))
    provenance = [agent_provenance(name) for name in lineup]
    code = source_fingerprint()

    def check_unchanged():
        if source_fingerprint() != code or [agent_provenance(name) for name in lineup] != provenance:
            raise ValueError("Evaluation agent artifacts or source changed during the run")

    # Resolve factories / engine before creating an experiment directory.
    for name in lineup:
        make_agent(name)
    check_unchanged()
    from .engine import require_engine
    require_engine()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "rules": asdict(Rules()), "lineup": lineup,
                "lineup_provenance": provenance,
                "seeds": seeds, "games_expected": len(fixtures), "status": "running",
                "code": code, "python": sys.version}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    results = []
    candidate_latencies = array("d")
    try:
        with (output / "games.jsonl").open("x") as stream:
            for seed, rotation, seats in fixtures:
                check_unchanged()
                env = new_environment(seed)
                agents = [make_agent(name) for name in seats]
                check_unchanged()
                contexts = [Context(player_id=seat, seed=seed) for seat in range(4)]
                for agent, context in zip(agents, contexts):
                    agent.reset(context)
                observations = env.reset()
                latencies = []
                candidate_game_latencies = []
                started = time.monotonic()
                steps = 0
                while not env.done():
                    if steps >= max_steps or not observations:
                        raise RuntimeError("Engine stalled or exceeded step limit")
                    actions = {}
                    for seat, observation in observations.items():
                        before = time.perf_counter()
                        action = agents[seat].act(observation, contexts[seat])
                        elapsed_ms = (time.perf_counter() - before) * 1000
                        latencies.append(elapsed_ms)
                        if seat == rotation:
                            candidate_game_latencies.append(elapsed_ms)
                        actions[seat] = ensure_legal(action, observation)
                    observations = env.step(actions)
                    steps += 1
                game_elapsed = time.monotonic() - started
                check_unchanged()
                scores = list(env.scores())
                if len(scores) != 4:
                    raise RuntimeError("Unexpected final score shape")
                # Tenhou ties favor the earlier initial seat.
                order = sorted(range(4), key=lambda seat: (-scores[seat], seat))
                ranks = [order.index(seat) + 1 for seat in range(4)]
                latencies.sort()
                row = {"seed": seed, "rotation": rotation, "seats": seats, "scores": scores,
                       "ranks": ranks, "decisions": len(latencies), "steps": steps,
                       "elapsed_seconds": game_elapsed,
                       "latency_ms_p95": latencies[min(len(latencies)-1, int(len(latencies)*.95))],
                       "latency_ms_max": max(latencies), "illegal_actions": 0,
                       "event_counts": dict(Counter(event["type"] for event in env.mjai_log))}
                row.update(_candidate_latency(candidate_game_latencies))
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                results.append(row)
                candidate_latencies.extend(candidate_game_latencies)
        report = summarize(results)
        report.update(_candidate_latency(candidate_latencies))
        report["latency_scope"] = "agent.act only; candidate seat isolated; excludes loading and transport"
        event_counts = Counter()
        for row in results:
            event_counts.update(row["event_counts"])
        report["event_counts"] = dict(event_counts)
        report["illegal_actions"] = sum(row["illegal_actions"] for row in results)
        report["decisions"] = sum(row["decisions"] for row in results)
        report["elapsed_seconds"] = sum(row["elapsed_seconds"] for row in results)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        manifest["status"] = "complete"
        return report
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        raise
    finally:
        manifest["games_completed"] = len(results)
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
