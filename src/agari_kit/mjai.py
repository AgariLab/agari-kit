"""Streaming structural inspection and conservative player-visible event projection.

Structural inspection is deliberately NOT an engine legality or score validator.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Iterable, Iterator


class DataError(ValueError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_events(path: Path, *, max_line_bytes: int = 1024 * 1024) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rb") as stream:
            line_number = 0
            while True:
                raw = stream.readline(max_line_bytes + 1)
                if not raw:
                    break
                line_number += 1
                if len(raw) > max_line_bytes:
                    raise DataError(f"{path}:{line_number}: event exceeds size limit")
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise DataError(f"{path}:{line_number}: invalid JSON event") from exc
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise DataError(f"{path}:{line_number}: expected an object with an event type")
                yield event
    except (OSError, EOFError) as exc:
        raise DataError(f"{path}: unreadable or truncated stream: {exc}") from exc


def inspect_game(events: Iterable[dict]) -> dict:
    digest = hashlib.sha256()
    rounds = count = 0
    started = ended = in_round = False
    scores = None
    for count, event in enumerate(events, 1):
        kind = event["type"]
        digest.update((canonical_json(event) + "\n").encode())
        if ended:
            raise DataError(f"event {count}: data after end_game")
        if not started and kind != "start_game":
            raise DataError(f"event {count}: missing start_game")
        if kind == "start_game":
            if started:
                raise DataError(f"event {count}: multiple games in one input")
            started = True
        elif kind == "start_kyoku":
            if in_round:
                raise DataError(f"event {count}: previous kyoku has not ended")
            hands = event.get("tehais", [])
            if len(hands) != 4 or any(not isinstance(hand, list) or len(hand) != 13 for hand in hands):
                raise DataError(f"event {count}: expected four 13-tile starting hands")
            rounds += 1
            in_round = True
        elif kind == "end_kyoku":
            if not in_round:
                raise DataError(f"event {count}: end_kyoku outside a kyoku")
            in_round = False
        elif kind == "end_game":
            if in_round or not rounds:
                raise DataError(f"event {count}: incomplete game")
            ended = True
            scores = event.get("scores")
            if scores is not None and (
                not isinstance(scores, list) or len(scores) != 4
                or any(type(score) is not int for score in scores)
            ):
                raise DataError(f"event {count}: expected four integer final scores")
        elif not in_round:
            raise DataError(f"event {count}: {kind} outside a kyoku")
    if not ended:
        raise DataError("Missing end_game (empty or truncated game)")
    return {"sha256": digest.hexdigest(), "events": count, "rounds": rounds,
            "scores": scores, "validation": "structural-only"}


# Explicit field allowlists keep unknown annotations / post-game hidden state out
# of policy event streams. An unsupported event fails closed until reviewed.
VISIBLE_FIELDS = {
    "start_game": ("type", "id"),
    "start_kyoku": ("type", "bakaze", "dora_marker", "kyoku", "honba", "kyotaku", "oya", "scores", "tehais"),
    "tsumo": ("type", "actor", "pai"),
    "dahai": ("type", "actor", "pai", "tsumogiri"),
    "chi": ("type", "actor", "target", "pai", "consumed"),
    "pon": ("type", "actor", "target", "pai", "consumed"),
    "daiminkan": ("type", "actor", "target", "pai", "consumed"),
    "ankan": ("type", "actor", "consumed"),
    "kakan": ("type", "actor", "pai", "consumed"),
    "reach": ("type", "actor"),
    "reach_accepted": ("type", "actor"),
    "dora": ("type", "dora_marker"),
    "hora": ("type", "actor", "target", "deltas", "scores"),
    "ryukyoku": ("type", "deltas", "scores"),
    "end_kyoku": ("type",),
    "end_game": ("type", "scores"),
    "none": ("type",),
}


def player_event(event: dict, player_id: int) -> dict:
    if player_id not in range(4):
        raise DataError("player_id must be 0..3")
    kind = event.get("type")
    if kind not in VISIBLE_FIELDS:
        raise DataError(f"Unreviewed policy event type: {kind}")
    # Round-trip also prevents callers mutating the original nested event lists.
    result = json.loads(canonical_json({key: event[key] for key in VISIBLE_FIELDS[kind] if key in event}))
    if kind == "start_game":
        result["id"] = player_id
    elif kind == "start_kyoku":
        hands = result.get("tehais", [])
        if len(hands) != 4:
            raise DataError("Expected four hands")
        result["tehais"] = [hand if seat == player_id else ["?"] * len(hand)
                             for seat, hand in enumerate(hands)]
    elif kind == "tsumo" and result.get("actor") != player_id:
        result["pai"] = "?"
    return result
