"""Offline presentation of MJAI events; never a policy input or legality validator.

The installed, pinned RiichiEnv viewer supplies the table and navigation. Its
notebook wrapper / MetadataInjector are deliberately not used: display must not
silently replace recorded settlement deltas with newly calculated game results.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from importlib import metadata, resources
from pathlib import Path
from string import Template

from .mjai import DataError, VISIBLE_FIELDS, canonical_json, inspect_game, player_event, read_events


VIEWER_VERSION = "0.4.10"
TILES = {f"{number}{suit}" for number in range(1, 10) for suit in "mps"} | {
    "5mr", "5pr", "5sr", "E", "S", "W", "N", "P", "F", "C", "?"
}
ACTOR_EVENTS = {"tsumo", "dahai", "chi", "pon", "daiminkan", "ankan", "kakan",
                "reach", "reach_accepted", "hora"}
CONSUMED_COUNTS = {"chi": 2, "pon": 2, "daiminkan": 3, "ankan": 4, "kakan": 3}


def _validate_display_event(event: dict, number: int) -> None:
    """Reject unsupported display shapes, without claiming Mahjong legality."""
    kind = event["type"]

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise DataError(f"event {number}: {message}")

    require(kind in VISIBLE_FIELDS, f"unsupported replay event {kind!r}")
    if kind in ACTOR_EVENTS:
        require(type(event.get("actor")) is int and event["actor"] in range(4), "invalid actor")
    if kind in {"chi", "pon", "daiminkan", "hora"}:
        require(type(event.get("target")) is int and event["target"] in range(4), "invalid target")
    for key in ("pai", "dora_marker"):
        if key in event:
            require(isinstance(event[key], str) and event[key] in TILES, f"invalid {key}")
    if kind in {"tsumo", "dahai", "chi", "pon", "daiminkan", "kakan"}:
        require("pai" in event, "missing pai")
    if kind == "dora":
        require("dora_marker" in event, "missing dora_marker")
    if kind in CONSUMED_COUNTS:
        tiles = event.get("consumed")
        require(isinstance(tiles, list) and len(tiles) == CONSUMED_COUNTS[kind]
                and all(isinstance(t, str) and t in TILES for t in tiles), "invalid consumed")
    for key in ("scores", "deltas"):
        if key in event:
            require(isinstance(event[key], list) and len(event[key]) == 4
                    and all(type(v) is int for v in event[key]), f"expected four integer {key}")
    if kind == "start_kyoku":
        require("scores" in event and "dora_marker" in event, "missing round scores/dora_marker")
        require(event.get("bakaze") in ("E", "S", "W", "N"), "invalid round wind")
        for key in ("kyoku", "honba", "kyotaku", "oya"):
            require(type(event.get(key)) is int, f"missing/integer {key} required")
        require(event["kyoku"] in range(1, 5) and event["oya"] in range(4)
                and event["honba"] >= 0 and event["kyotaku"] >= 0, "invalid round counters")
        require(all(isinstance(t, str) and t in TILES for hand in event["tehais"] for t in hand),
                "invalid starting tile")
    if kind in {"hora", "ryukyoku"}:
        require("scores" in event or "deltas" in event, "settlement needs scores or deltas")


def _project_events(events: list[dict], player: int | None = None) -> list[dict]:
    """Keep source fields separate from scores computed for the renderer.

    Player exports contain no opponent initial hands/draws or arbitrary analysis
    metadata. They are conservative views, not serialized Agent Observations.
    """
    if player is not None and (type(player) is not int or player not in range(4)):
        raise DataError("player must be 0..3")
    inspect_game(events)
    result = []
    for number, original in enumerate(events, 1):
        _validate_display_event(original, number)
        kind = original["type"]
        if player is None:
            event = json.loads(canonical_json({k: original[k] for k in VISIBLE_FIELDS[kind]
                                               if k in original}))
        else:
            event = player_event(original, player)
        # Names are rendered as text by the pinned viewer. Never retain avatar
        # URLs, arbitrary metadata, future annotations, walls or post-game hands.
        if kind == "start_game" and "names" in original:
            names = original["names"]
            if (not isinstance(names, list) or len(names) != 4
                    or not all(isinstance(n, str) for n in names)):
                raise DataError(f"event {number}: expected four player names")
            event["names"] = list(names)
        result.append(event)
    return result


def _score_data(events: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Causal display scores plus offline comparisons, never inferred repairs.

    Only the immediate settlement / end_kyoku / start_kyoku boundary is compared.
    end_game can distribute a remaining riichi pool, so it cannot establish the
    preceding settlement's scores. Comparisons belong only to the HTML replay,
    not to the shared policy projection or the original MJAI stream.
    """
    displayed = json.loads(canonical_json(events))
    steps, conflicts = [], []
    scores, basis = None, "unavailable"
    for index, event in enumerate(displayed):
        kind = event["type"]
        if kind == "start_kyoku":
            if (index >= 2 and events[index - 1]["type"] == "end_kyoku"
                    and events[index - 2]["type"] in {"hora", "ryukyoku"}
                    and scores != event["scores"]):
                conflicts.append({
                    "settlement_event": index - 1, "next_round_event": index + 1,
                    "settlement_scores": list(scores),
                    "next_round_scores": list(event["scores"]),
                })
            scores, basis = list(event["scores"]), "recorded"
        elif kind == "reach_accepted":
            scores[event["actor"]] -= 1000
            basis = "derived"
        elif kind in {"hora", "ryukyoku"}:
            if "scores" in event:
                scores, basis = list(event["scores"]), "recorded"
            else:
                scores = [s + d for s, d in zip(scores, event["deltas"], strict=True)]
                basis = "derived"
                event["scores"] = list(scores)
        elif kind == "end_game" and "scores" in event:
            scores, basis = list(event["scores"]), "recorded"
        steps.append({"basis": basis, "scores": list(scores) if scores is not None else None})
    return displayed, steps, conflicts


def display_events(events: list[dict], player: int | None = None) -> list[dict]:
    """Renderer events with causal scores; these are not the original events."""
    return _score_data(_project_events(events, player))[0]


def _viewer_assets() -> tuple[bytes, str]:
    try:
        distribution = metadata.distribution("riichienv")
        if distribution.version != VIEWER_VERSION:
            raise DataError(f"Offline replay requires riichienv=={VIEWER_VERSION}")
        asset = resources.files("riichienv").joinpath("visualizer/assets/viewer.js.gz").read_bytes()
        licenses = [p for p in distribution.files or ()
                    if str(p).endswith(".dist-info/licenses/LICENSE")]
        if len(licenses) != 1:
            raise DataError("RiichiEnv distribution license is missing; cannot export its viewer")
        license_text = distribution.locate_file(licenses[0]).read_text(encoding="utf-8")
    except (ModuleNotFoundError, metadata.PackageNotFoundError, OSError) as exc:
        raise DataError("Install pinned riichienv with its viewer assets and license") from exc
    if not asset.startswith(b"\x1f\x8b"):
        raise DataError("RiichiEnv viewer asset is not gzip data")
    return asset, license_text


def export_replay(source: Path, output: Path, *, player: int | None = None) -> dict:
    if output.suffix.lower() != ".html":
        raise DataError("Replay output must end in .html")
    if output.exists():
        raise DataError(f"Replay output already exists: {output}")
    events = list(read_events(source))
    summary = inspect_game(events)
    projected = _project_events(events, player)
    displayed, score_steps, score_conflicts = _score_data(projected)
    asset, license_text = _viewer_assets()
    manifest = {
        "schema_version": 2, "artifact": "offline-mjai-replay", "validation": "structural-only",
        "source_sha256": summary["sha256"], "events": summary["events"],
        "rounds": summary["rounds"], "final_scores": summary["scores"],
        "view": "full-information-research" if player is None else f"player-{player}-visible",
        "player": player, "viewer": f"riichienv=={VIEWER_VERSION}",
        "viewer_gzip_sha256": hashlib.sha256(asset).hexdigest(),
        "projected_events_sha256": inspect_game(projected)["sha256"],
        "display_events_sha256": inspect_game(displayed)["sha256"],
        "score_policy": "recorded-at-event-otherwise-derived",
        "score_conflicts": len(score_conflicts),
    }
    payload = base64.b64encode(
        canonical_json({"manifest": manifest, "events": projected,
                        "score_steps": score_steps, "score_conflicts": score_conflicts}).encode()
    ).decode()
    template = resources.files("agari_kit").joinpath("replay.html").read_text(encoding="utf-8")
    document = Template(template).substitute(
        title=html.escape(source.name), payload=payload,
        viewer=base64.b64encode(asset).decode(), license=html.escape(license_text),
    )
    # Exclusive creation also works on external filesystems without hardlinks.
    # A caught write failure removes this export, never a pre-existing target.
    # This is not atomic publication: a process/volume failure can leave a partial
    # file, which must be removed or given a new output name before retrying.
    created = False
    try:
        with output.open("x", encoding="utf-8") as stream:
            created = True
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return {**manifest, "output": str(output.resolve()), "bytes": output.stat().st_size}
