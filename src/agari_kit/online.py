"""riichi.dev protocol state, independent of credentials and socket transport.

Raw MJAI notifications are never sent to agents. Only unique request_action
observations reach the decision worker, which owns the agent's event state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from .agent import Context, Rules, ensure_legal, is_tsumogiri

ENDPOINTS = {"validate": "wss://game.riichi.dev/ws/validate",
             "ranked": "wss://game.riichi.dev/ws/ranked"}
FIELDS = {
    "dahai": ("type", "actor", "pai", "tsumogiri"),
    "chi": ("type", "actor", "target", "pai", "consumed"),
    "pon": ("type", "actor", "target", "pai", "consumed"),
    "daiminkan": ("type", "actor", "target", "pai", "consumed"),
    "ankan": ("type", "actor", "consumed"),
    "kakan": ("type", "actor", "pai", "consumed"),
    "reach": ("type", "actor"),
    "hora": ("type", "actor", "target", "pai"),
    "ryukyoku": ("type", "actor"),
    "none": ("type",),
}
ACK_STATUSES = {"accepted", "rejected", "unparseable", "stale", "defaulted"}
NOTIFICATIONS = {"start_kyoku", "tsumo", "dahai", "chi", "pon", "daiminkan", "ankan",
                 "kakan", "reach", "reach_accepted", "dora", "hora", "ryukyoku", "end_kyoku"}


class ProtocolError(RuntimeError):
    """Message text is a fixed diagnostic code, never remote content."""


class DecisionTimeout(RuntimeError):
    pass


class WorkerError(RuntimeError):
    pass


def _integer(value, code: str, minimum=0) -> int:
    if type(value) is not int or value < minimum:
        raise ProtocolError(code)
    return value


def _tile(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?:[1-9][mps]|5[mps]r|[ESWNPFC])", value))


def clean_action(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or value["type"] not in FIELDS:
        raise ProtocolError("unsupported_action")
    kind = value["type"]
    result = {key: value[key] for key in FIELDS[kind] if key in value}
    for key in ("actor", "target"):
        if key in result and (type(result[key]) is not int or result[key] not in range(4)):
            raise ProtocolError("invalid_action_seat")
    if "pai" in result and not _tile(result["pai"]):
        raise ProtocolError("invalid_action_tile")
    if kind in ("dahai", "chi", "pon", "daiminkan", "kakan") and "pai" not in result:
        raise ProtocolError("missing_action_tile")
    count = {"chi": 2, "pon": 2, "daiminkan": 3, "ankan": 4, "kakan": 3}.get(kind)
    if count is not None:
        consumed = result.get("consumed")
        if (not isinstance(consumed, list) or len(consumed) != count
                or not all(_tile(tile) for tile in consumed)):
            raise ProtocolError("invalid_consumed_tiles")
        result["consumed"] = list(consumed)
    if "tsumogiri" in result and type(result["tsumogiri"]) is not bool:
        raise ProtocolError("invalid_tsumogiri_flag")
    return result


def matches_possible(response: dict, possible: dict) -> bool:
    if response["type"] != possible["type"]:
        return False
    for key in ("actor", "target", "pai", "tsumogiri", "consumed"):
        if key not in possible:
            continue
        # hora permits actor alone; the server infers the winning tile/target.
        if response["type"] == "hora" and key in ("pai", "target") and key not in response:
            continue
        if key == "consumed":
            if sorted(response.get(key, [])) != sorted(possible[key]):
                return False
        elif response.get(key) != possible[key]:
            return False
    return True


def response_for_action(action, observation, possible: list[dict], seat: int) -> dict:
    """Round-trip through the pinned engine, then intersect with server legality."""
    action = ensure_legal(action, observation)
    response = clean_action(json.loads(action.to_mjai()))
    if response["type"] != "none":
        response["actor"] = seat
    if response["type"] == "dahai":
        response["tsumogiri"] = is_tsumogiri(action, observation)
    if response["type"] in ("chi", "pon", "daiminkan"):
        for raw in reversed(observation.events):
            event = json.loads(raw) if isinstance(raw, str) else raw
            if event.get("type") == "start_kyoku":
                break
            if event.get("type") == "dahai":
                response["target"] = event["actor"]
                break
        if "target" not in response or response["target"] == seat:
            raise ProtocolError("missing_call_target")
    if observation.select_action_from_mjai(response) is None:
        raise ProtocolError("action_roundtrip_failed")
    if not any(matches_possible(response, item) for item in possible):
        raise ProtocolError("engine_server_actions_disagree")
    return response


@dataclass(frozen=True)
class Request:
    request_id: int
    observation: str
    possible_actions: list[dict]
    deadline: float
    fingerprint: str

    @classmethod
    def parse(cls, message: dict, received_at: float, reserve_ms: int) -> Request:
        request_id = _integer(message.get("request_id"), "invalid_request_id")
        budget = message.get("time")
        if not isinstance(budget, dict):
            raise ProtocolError("missing_time_budget")
        grace = _integer(budget.get("grace_ms"), "invalid_grace_budget")
        bank = _integer(budget.get("bank_ms"), "invalid_bank_budget")
        deadline = _integer(budget.get("deadline_ms"), "invalid_deadline_budget")
        observation = message.get("observation")
        if not isinstance(observation, str) or not observation or len(observation) > 2**20:
            raise ProtocolError("invalid_observation_envelope")
        possible = message.get("possible_actions")
        if not isinstance(possible, list) or not 1 <= len(possible) <= 512:
            raise ProtocolError("invalid_possible_actions")
        possible = [clean_action(action) for action in possible]
        fingerprint = hashlib.sha256(json.dumps([observation, possible], sort_keys=True).encode()).hexdigest()
        return cls(request_id, observation, possible,
                   received_at + max(0, min(grace + bank, deadline) - reserve_ms) / 1000,
                   fingerprint)


class OnlineSession:
    def __init__(self, *, mode: str, runner, emit: Callable[[dict], None],
                 seed: int = 0, reserve_ms: int = 100):
        if mode not in ENDPOINTS or type(reserve_ms) is not int or reserve_ms < 0:
            raise ValueError("Invalid mode or timing reserve")
        self.mode, self.runner, self.emit = mode, runner, emit
        self.seed, self.reserve_ms = seed, reserve_ms
        self.seat = None
        self.last_request = None
        self.last_fingerprint = None
        self.acknowledged = set()
        self.request_ids = set()
        self.reply_deadline = None
        self.scores = None
        self.validation_passed = None
        self.ended = False
        self.done = False
        self.degraded = False
        self.counts = Counter()

    async def handle(self, message: dict, received_at: float | None = None) -> dict | None:
        received_at = time.monotonic() if received_at is None else received_at
        if not isinstance(message, dict):
            raise ProtocolError("message_must_be_object")
        kind = message.get("type")
        if "error" in message:
            raise ProtocolError("server_reported_error")
        if not isinstance(kind, str):
            raise ProtocolError("invalid_message_type")
        if self.done:
            return None
        if kind == "start_game":
            seat = _integer(message.get("id"), "invalid_player_id")
            if seat not in range(4):
                raise ProtocolError("unsupported_player_count")
            if self.seat is not None:
                if seat != self.seat:
                    raise ProtocolError("seat_changed_during_game")
                self.counts["duplicate_start"] += 1
                return None
            self.seat = seat
            self.emit({"event": "start_game", "seat": seat})
        elif kind == "request_action":
            if self.seat is None or self.ended:
                raise ProtocolError("request_outside_game")
            request = Request.parse(message, received_at, self.reserve_ms)
            if self.last_request is not None and request.request_id <= self.last_request:
                if request.request_id == self.last_request and request.fingerprint != self.last_fingerprint:
                    raise ProtocolError("request_id_reused_with_different_state")
                self.counts["duplicate_requests" if request.request_id == self.last_request else "stale_requests"] += 1
                return None
            self.last_request, self.last_fingerprint = request.request_id, request.fingerprint
            self.request_ids.add(request.request_id)
            self.counts["requests"] += 1
            context = Context(self.seat, self.seed,
                              Rules(game_mode="4p-red-east" if self.mode == "validate" else "4p-red-half"),
                              request.deadline)
            remaining = request.deadline - time.monotonic()
            if self.degraded:
                self.counts["suppressed_after_timeout"] += 1
                return None
            if remaining <= 0:
                await self._timeout(request.request_id)
                return None
            try:
                action = await asyncio.wait_for(self.runner.decide(request, context), remaining)
            except (asyncio.TimeoutError, DecisionTimeout):
                await self._timeout(request.request_id)
                return None
            if time.monotonic() >= request.deadline:
                await self._timeout(request.request_id)
                return None
            response = clean_action(action)
            if response["type"] != "none" and response.get("actor") != self.seat:
                raise ProtocolError("worker_returned_wrong_actor")
            if not any(matches_possible(response, possible) for possible in request.possible_actions):
                raise ProtocolError("worker_returned_illegal_action")
            response["request_id"] = request.request_id
            self.reply_deadline = request.deadline + self.reserve_ms / 1000
            self.counts["replies_prepared"] += 1
            self.emit({"event": "decision", "request_id": request.request_id,
                       "latency_ms": (time.monotonic() - received_at) * 1000,
                       "action_type": response["type"]})
            return response
        elif kind == "action_ack":
            request_id = _integer(message.get("request_id"), "invalid_ack_request_id")
            status = message.get("status")
            if status not in ACK_STATUSES:
                self.counts["unknown_ack_status"] += 1
                return None
            if request_id not in self.request_ids:
                raise ProtocolError("ack_for_unknown_request")
            key = (request_id, status)
            if key in self.acknowledged:
                self.counts["duplicate_acks"] += 1
                return None
            self.acknowledged.add(key)
            self.counts[f"ack_{status}"] += 1
            record = {"event": "action_ack", "request_id": request_id, "status": status}
            for field in ("elapsed_ms", "bank_consumed_ms", "bank_ms"):
                value = message.get(field)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    record[field] = value
            self.emit(record)
        elif kind == "end_game":
            scores = message.get("scores")
            if self.seat is None or not isinstance(scores, list) or len(scores) != 4 or any(type(v) is not int for v in scores):
                raise ProtocolError("invalid_end_game")
            self.scores = list(scores)
            self.ended = True
            self.done = self.mode == "ranked" or self.validation_passed is not None
            self.emit({"event": "end_game", "scores": self.scores})
        elif kind == "validation_result":
            if self.mode != "validate" or type(message.get("passed")) is not bool:
                raise ProtocolError("invalid_validation_result")
            self.validation_passed = message["passed"]
            self.done = self.ended
            self.emit({"event": "validation_result", "passed": self.validation_passed})
        elif kind not in NOTIFICATIONS:
            self.counts["unknown_messages"] += 1
        return None

    async def _timeout(self, request_id: int) -> None:
        self.counts["local_timeouts"] += 1
        self.degraded = True
        await self.runner.close()
        # The server defaults the action. Restarting an event-driven agent here
        # would lose the already consumed event state, so remain degraded.
        self.emit({"event": "inference_timeout", "request_id": request_id})

    def report(self) -> dict:
        status = "complete" if self.done else "interrupted"
        if self.mode == "validate" and self.ended and self.validation_passed is None:
            status = "validation_unconfirmed"
        return {"mode": self.mode, "status": status, "seat": self.seat,
                "scores": self.scores, "validation_passed": self.validation_passed,
                "degraded": self.degraded, "counts": dict(self.counts)}
