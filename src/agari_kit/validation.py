"""Verify local self-play archives by independently replaying a seeded action trace.

RiichiEnv's MJAI event ingestion is useful for replay, but is not an execution
validator (for example, applying a ``hora`` does not settle its score deltas).
Only a complete trace replay earns ``engine-verified`` here. Third-party MJAI
without such a trace remains explicitly structural-only until an independent
external-log verifier is implemented.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from .agent import Rules
from .engine import new_environment, require_engine
from .mjai import DataError, canonical_json, inspect_game, read_events

METHOD = "seeded-action-trace-v1"
_OBSERVATION_FIELDS = {
    "player_id", "hands", "melds", "discards", "dora_indicators", "scores",
    "riichi_declared", "_legal_actions", "events", "honba", "riichi_sticks",
    "round_wind", "oya", "kyoku_index", "waits", "is_tenpai", "tsumogiri_flags",
    "riichi_sutehais", "last_tedashis", "last_discard", "drawn_tile", "forced_tedashi",
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4)
def _binary_sha256(path: str, modified_ns: int, size: int) -> str:
    # The stat arguments invalidate the cache if the installed wheel changes.
    # Avoid re-reading the same native binary for every game in a dataset.
    return _file_sha256(Path(path))


def _trace_records(path: Path) -> Iterator[tuple[int, dict]]:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rb") as stream:
            number = 0
            while raw := stream.readline(1024 * 1024 + 1):
                number += 1
                if len(raw) > 1024 * 1024:
                    raise DataError(f"{path}:{number}: trace record exceeds size limit")
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise DataError(f"{path}:{number}: invalid trace JSON") from exc
                if not isinstance(record, dict):
                    raise DataError(f"{path}:{number}: expected trace object")
                yield number, record
    except (OSError, EOFError) as exc:
        raise DataError(f"{path}: unreadable or truncated trace") from exc


def _check_player_view(snapshot: dict, player_id: int, where: str) -> None:
    """Audit the actual serialized input whose digest is recorded in the trace."""
    if set(snapshot) != _OBSERVATION_FIELDS:
        raise DataError(f"{where}: unreviewed Observation serialization fields")
    if snapshot["player_id"] != player_id:
        raise DataError(f"{where}: Observation belongs to a different player")
    hands = snapshot["hands"]
    if not isinstance(hands, list) or len(hands) != 4:
        raise DataError(f"{where}: expected four player hand slots")
    if any(hand for seat, hand in enumerate(hands) if seat != player_id):
        raise DataError(f"{where}: opponent hidden hand in policy Observation")
    own = hands[player_id]
    if any(type(tile) is not int or not 0 <= tile < 136 for tile in own) or len(set(own)) != len(own):
        raise DataError(f"{where}: invalid or duplicate physical hand tiles")
    if len(own) + 3 * len(snapshot["melds"][player_id]) not in (13, 14):
        raise DataError(f"{where}: inconsistent player hand tile count")
    visible = set(own) | set(snapshot["dora_indicators"])
    for river in snapshot["discards"]:
        visible.update(river)
    for melds in snapshot["melds"]:
        for meld in melds:
            visible.update(meld["tiles"])
    drawn_tile = snapshot["drawn_tile"]
    if drawn_tile is not None and drawn_tile not in visible:
        raise DataError(f"{where}: hidden draw in policy Observation")
    for raw in snapshot["events"]:
        event = json.loads(raw)
        if event.get("type") == "start_kyoku":
            event_hands = event.get("tehais", [])
            if len(event_hands) != 4:
                raise DataError(f"{where}: invalid starting hand event")
            for seat, hand in enumerate(event_hands):
                if seat != player_id and any(tile != "?" for tile in hand):
                    raise DataError(f"{where}: opponent hidden hand in policy event")
        if (event.get("type") == "tsumo" and event.get("actor") != player_id
                and event.get("pai") != "?"):
            raise DataError(f"{where}: opponent hidden draw in policy event")


def validate_mjai(path: Path, trace_path: Path | None = None, *, max_steps: int = 100000) -> dict:
    """Return a content-bound validation receipt; raise DataError on corruption.

The canonical ``sha256`` is compatible with ``inspect_game``. Raw-file digests
bind a receipt to its exact archive bytes, including the trace. A receipt is not
a signature: callers granting training eligibility must rerun this validator,
not trust a user-supplied ``validation`` string or JSON document.
"""
    path = Path(path)
    summary = inspect_game(read_events(path))
    summary.update({"schema_version": 1, "mjai_file_sha256": _file_sha256(path),
                    "validator_source_sha256": _file_sha256(Path(__file__))})
    if trace_path is None:
        summary.update({"method": "structural-only", "trace_sha256": None,
                        "checks": {"complete_event_structure": True, "action_legality": False,
                                   "tile_counts": False, "final_scores": False,
                                   "player_information_boundary": False},
                        "limitations": ["No seeded action trace; engine legality and scores are unverified"]})
        return summary
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("max_steps must be positive")
    trace_path = Path(trace_path)
    engine_version = require_engine()
    trace_hash_before = _file_sha256(trace_path)
    records = iter(_trace_records(trace_path))
    try:
        _, header = next(records)
    except StopIteration as exc:
        raise DataError(f"{trace_path}: empty trace") from exc
    expected_rules = asdict(Rules())
    if (header.get("type") != "agari_trace" or type(header.get("schema_version")) is not int
            or header["schema_version"] != 1
            or header.get("rules") != expected_rules):
        raise DataError(f"{trace_path}: unsupported trace header or rules")
    seed = header.get("seed")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise DataError(f"{trace_path}: invalid uint64 seed")
    agents = header.get("agents")
    if not isinstance(agents, list) or len(agents) != 4 or not all(isinstance(a, str) and a for a in agents):
        raise DataError(f"{trace_path}: trace must identify four agents")
    if not isinstance(header.get("code"), dict):
        raise DataError(f"{trace_path}: missing producer code provenance")
    engine = new_environment(seed)
    observations = engine.reset()
    steps = decisions = 0
    footer = None
    for line_number, record in records:
        where = f"{trace_path}:{line_number}"
        if record.get("type") == "trace_end":
            footer = record
            try:
                next(records)
            except StopIteration:
                break
            raise DataError(f"{where}: records after trace_end")
        if engine.done():
            raise DataError(f"{where}: decisions after completed game")
        if steps >= max_steps:
            raise DataError(f"{where}: maximum replay steps exceeded")
        if type(record.get("step")) is not int or record["step"] != steps:
            raise DataError(f"{where}: non-contiguous step index (expected {steps})")
        submitted, fingerprints = record.get("actions"), record.get("observations_sha256")
        seats = {str(seat) for seat in observations}
        if not seats or not isinstance(submitted, dict) or set(submitted) != seats:
            raise DataError(f"{where}: action seats do not match active players")
        if not isinstance(fingerprints, dict) or set(fingerprints) != seats:
            raise DataError(f"{where}: Observation hashes do not match active players")
        actions = {}
        for player_id, observation in observations.items():
            encoded = observation.serialize_to_base64()
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            if fingerprints[str(player_id)] != digest:
                raise DataError(f"{where}: Observation hash mismatch for player {player_id}")
            snapshot = json.loads(base64.b64decode(encoded, validate=True))
            _check_player_view(snapshot, player_id, where)
            candidate = submitted[str(player_id)]
            if not isinstance(candidate, dict):
                raise DataError(f"{where}: action must be an object")
            candidate_key = canonical_json(candidate)
            legal = {canonical_json(action.to_dict()): action for action in observation.legal_actions()}
            if candidate_key not in legal:
                raise DataError(f"{where}: illegal action for player {player_id}")
            actions[player_id] = legal[candidate_key]
        try:
            observations = engine.step(actions)
        except Exception as exc:
            raise DataError(f"{where}: engine rejected step {steps}") from exc
        steps += 1
        decisions += len(actions)
    if footer is None or not engine.done():
        raise DataError(f"{trace_path}: missing trace_end or unfinished game")
    scores = list(engine.scores())
    if type(footer.get("steps")) is not int or footer["steps"] != steps:
        raise DataError(f"{trace_path}: trace_end step count mismatch")
    footer_scores = footer.get("scores")
    if (not isinstance(footer_scores, list) or len(footer_scores) != 4
            or any(type(score) is not int for score in footer_scores) or footer_scores != scores):
        raise DataError(f"{trace_path}: trace_end scores disagree with replay")
    expected_events = engine.mjai_log
    if not expected_events or expected_events[-1].get("type") != "end_game":
        raise DataError("Engine completed without an end_game event")
    # Native logs omit scores at end_game. Our archive convention records them
    # so the terminal result is independently checked along with every delta.
    expected_events[-1] = dict(expected_events[-1], scores=scores)
    actual_events = iter(read_events(path))
    compared_digest = hashlib.sha256()
    for index, expected in enumerate(expected_events, 1):
        try:
            actual = next(actual_events)
        except StopIteration as exc:
            raise DataError(f"{path}: missing event {index} during engine comparison") from exc
        if canonical_json(actual) != canonical_json(expected):
            raise DataError(f"{path}:{index}: MJAI event disagrees with seeded engine replay")
        compared_digest.update((canonical_json(actual) + "\n").encode())
    try:
        next(actual_events)
    except StopIteration:
        pass
    else:
        raise DataError(f"{path}: extra events after engine game")
    if compared_digest.hexdigest() != summary["sha256"] or len(expected_events) != summary["events"]:
        raise DataError("MJAI content changed between structural inspection and engine validation")
    if _file_sha256(path) != summary["mjai_file_sha256"] or _file_sha256(trace_path) != trace_hash_before:
        raise DataError("Input artifacts changed during validation")
    from riichienv import _riichienv as native_engine
    binary = Path(native_engine.__file__).resolve()
    binary_stat = binary.stat()
    summary.update({"validation": "engine-verified", "method": METHOD,
                    "engine_version": engine_version, "rules": expected_rules,
                    "engine_binary_sha256": _binary_sha256(str(binary), binary_stat.st_mtime_ns, binary_stat.st_size),
                    "trace_sha256": trace_hash_before, "seed": seed, "steps": steps,
                    "decisions": decisions, "scores": scores, "producer_code": header["code"],
                    "checks": {"complete_event_structure": True, "action_legality": True,
                               "tile_counts": True, "final_scores": True,
                               "all_mjai_events": True, "observation_hashes": True,
                               "player_information_boundary": True}})
    return summary
