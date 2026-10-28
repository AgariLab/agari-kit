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

from .agent import Context, Rules, ensure_legal, managed_agents
from .engine import new_environment, engine_identity
from .plugins import agent_provenance
from .provenance import source_fingerprint


DIAGNOSTIC_COUNTS = ("hands", "wins", "tsumo_wins", "ron_wins", "deal_ins",
                     "deal_in_payments", "open_hands", "riichi_hands", "meld_events",
                     "kan_events", "riichi_declarations")


def diagnostic_rates(counts: dict) -> dict:
    """Rates use hands, not decisions; multi-ron counts one deal-in hand."""
    result = {key: counts.get(key, 0) for key in DIAGNOSTIC_COUNTS}
    for name, count in (("win_rate", "wins"), ("deal_in_rate", "deal_ins"),
                        ("open_hand_rate", "open_hands"), ("riichi_rate", "riichi_hands")):
        result[name] = result[count] / result["hands"] if result["hands"] else None
    return result


def candidate_diagnostics(events: list[dict], seat: int) -> dict:
    """Derive candidate-only hand statistics from the completed engine log.

    Ankan is a kan but not an open hand. Riichi means reach_accepted. A double
    ron against the candidate contributes two payments and one deal-in hand.
    These retrospective statistics are never passed to an Agent.
    """
    counts = Counter()
    hand = None

    def finish():
        if hand is not None:
            counts["hands"] += 1
            counts.update({key: int(value) for key, value in hand.items()})

    for event in events:
        kind, actor = event.get("type"), event.get("actor")
        if kind == "start_kyoku":
            finish()
            hand = dict.fromkeys(("wins", "deal_ins", "open_hands", "riichi_hands"), False)
        elif kind in ("end_kyoku", "end_game"):
            finish()
            hand = None
        elif hand is not None:
            if kind == "hora":
                if actor == seat:
                    hand["wins"] = True
                    counts["tsumo_wins" if event.get("target") == seat else "ron_wins"] += 1
                elif event.get("target") == seat:
                    hand["deal_ins"] = True
                    counts["deal_in_payments"] += 1
            elif actor == seat:
                if kind in ("chi", "pon", "daiminkan", "kakan"):
                    hand["open_hands"] = True
                    counts["meld_events"] += 1
                if kind in ("ankan", "daiminkan", "kakan"):
                    counts["kan_events"] += 1
                if kind == "reach":
                    counts["riichi_declarations"] += 1
                if kind == "reach_accepted":
                    hand["riichi_hands"] = True
    finish()
    return diagnostic_rates(counts)


def result_report(results: list[dict], candidate_latencies) -> dict:
    report = summarize(results)
    report.update(_candidate_latency(candidate_latencies))
    report["latency_scope"] = "agent.act only; candidate seat isolated; excludes loading and transport"
    event_counts, diagnostics = Counter(), Counter()
    for row in results:
        event_counts.update(row["event_counts"])
        diagnostics.update({key: row["candidate_diagnostics"][key] for key in DIAGNOSTIC_COUNTS})
    report["event_counts"] = dict(event_counts)
    report["candidate_diagnostics"] = diagnostic_rates(diagnostics)
    report["illegal_actions"] = sum(row["illegal_actions"] for row in results)
    report["decisions"] = sum(row["decisions"] for row in results)
    report["elapsed_seconds"] = sum(row["elapsed_seconds"] for row in results)
    return report


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
    engine = engine_identity()

    def check_unchanged():
        if (source_fingerprint() != code or engine_identity() != engine
                or [agent_provenance(name) for name in lineup] != provenance):
            raise ValueError("Evaluation agent artifacts or source changed during the run")

    # Resolve factories / engine before creating an experiment directory.
    with managed_agents(lineup):
        check_unchanged()
    from .engine import require_engine
    require_engine()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "rules": asdict(Rules()), "lineup": lineup,
                "lineup_provenance": provenance,
                "engine": engine,
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
                with managed_agents(seats) as agents:
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
                    row["candidate_diagnostics"] = candidate_diagnostics(env.mjai_log, rotation)
                    row.update(_candidate_latency(candidate_game_latencies))
                    stream.write(json.dumps(row) + "\n")
                    stream.flush()
                    results.append(row)
                    candidate_latencies.extend(candidate_game_latencies)
        report = result_report(results, candidate_latencies)
        # Retain exact samples so sharded reports never average percentiles.
        (output / "candidate-latencies.json").write_text(json.dumps(list(candidate_latencies)) + "\n")
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
