"""Agents receive player observations, never the environment or hidden state."""

from __future__ import annotations

import json
import base64
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from riichienv import Action, Observation

AGENT_NAMES = ("random", "tsumogiri", "efficiency")


@dataclass(frozen=True)
class Rules:
    game_mode: str = "4p-red-half"
    preset: str = "tenhou"
    engine_version: str = "0.4.10"

    def __post_init__(self) -> None:
        if (self.game_mode not in ("4p-red-half", "4p-red-east")
                or self.preset != "tenhou" or self.engine_version != "0.4.10"):
            raise ValueError("This release supports RiichiEnv 0.4.10 / Tenhou 4p red half or east")


@dataclass(frozen=True)
class Context:
    player_id: int
    seed: int
    rules: Rules = field(default_factory=Rules)
    deadline: float | None = None  # time.monotonic(), not wall time

    def __post_init__(self) -> None:
        if self.player_id not in range(4):
            raise ValueError("player_id must be 0..3")

    def remaining_seconds(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())


class Agent(Protocol):
    def reset(self, context: Context) -> None: ...
    def act(self, observation: Observation, context: Context) -> Action: ...


def action_key(action: Action) -> str:
    return json.dumps(action.to_dict(), sort_keys=True, separators=(",", ":"))


def ensure_legal(action: Action, observation: Observation) -> Action:
    key = action_key(action)
    for legal in observation.legal_actions():
        if action_key(legal) == key:
            return legal
    raise ValueError(f"Agent returned illegal action for player {observation.player_id}")


def drawn_tile_id(observation: Observation) -> int | None:
    """Read the player's current physical draw, respecting forced hand discards."""
    snapshot = json.loads(base64.b64decode(observation.serialize_to_base64(), validate=True))
    return None if snapshot.get("forced_tedashi", False) else snapshot.get("drawn_tile")


def is_tsumogiri(action: Action, observation: Observation) -> bool:
    """Use physical draw identity, not the upstream mapper's permissive fallback."""
    drawn = drawn_tile_id(observation)
    return drawn is not None and action.tile == drawn


class RandomAgent:
    def __init__(self) -> None:
        self._rng = random.Random(0)

    def reset(self, context: Context) -> None:
        self._rng.seed((context.seed << 2) + context.player_id)

    def act(self, observation: Observation, context: Context) -> Action:
        legal = observation.legal_actions()
        if not legal:
            raise ValueError("No decision is available")
        return self._rng.choice(legal)


class TsumogiriAgent:
    """Take wins, decline calls, discard the draw; forced discards use engine order."""

    def reset(self, context: Context) -> None:
        pass

    def act(self, observation: Observation, context: Context) -> Action:
        for message in ({"type": "hora", "actor": context.player_id}, {"type": "none"}):
            action = observation.select_action_from_mjai(message)
            if action is not None:
                return action
        legal = observation.legal_actions()
        if not legal:
            raise ValueError("No decision is available")
        drawn = drawn_tile_id(observation)
        for action in legal:
            if json.loads(action.to_mjai())["type"] == "dahai" and drawn is not None and action.tile == drawn:
                return action
        for action in legal:
            if json.loads(action.to_mjai())["type"] == "dahai":
                return action
        return legal[0]


def make_agent(name: str) -> Agent:
    if name.startswith("@"):
        from .plugins import load_agent
        return load_agent(name[1:])
    from .efficiency import EfficiencyAgent
    factories = {"random": RandomAgent, "tsumogiri": TsumogiriAgent, "efficiency": EfficiencyAgent}
    try:
        return factories[name]()
    except KeyError as exc:
        raise ValueError(f"Unknown agent: {name}") from exc
