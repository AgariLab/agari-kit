"""Strict player-observation bridge to the original Akochan ``pipe`` executable.

The binary and its privately obtained parameters stay outside this package.
An artifact manifest binds every parameter, config and native runtime file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from ..agent import Context, is_tsumogiri
from ..mjai import canonical_json, player_event
from ..online import FIELDS, clean_action


class AkochanError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AkochanAgent:
    def __init__(self, *, binary_path: str, config_path: str, manifest_path: str,
                 threads: int = 2, timeout_seconds: float = 30):
        if type(threads) is not int or threads < 1 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Invalid Akochan thread count or timeout")
        self.binary = Path(binary_path).resolve()
        self.config = Path(config_path).resolve()
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest_sha256 = _sha256(self.manifest_path)
        self.manifest = json.loads(self.manifest_path.read_text())
        self.threads, self.timeout_seconds = threads, timeout_seconds
        self._process = None
        self._owns_group = False
        self._context = None
        self._pending = None
        self._awaiting = None
        self._buffer = b""
        self._seen_start = False
        self._verify_artifacts()

    def _resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Akochan manifest paths must be relative")
        path = (self.manifest_path.parent / relative).resolve()
        if self.manifest_path.parent not in path.parents:
            raise ValueError("Akochan manifest path escapes artifact directory")
        return path

    def _verify_artifacts(self) -> None:
        manifest = self.manifest
        if _sha256(self.manifest_path) != self.manifest_sha256:
            raise ValueError("Akochan manifest changed")
        if (manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict)
                or type(manifest.get("compiled_threads")) is not int
                or not 1 <= self.threads <= manifest["compiled_threads"]):
            raise ValueError("Invalid Akochan artifact manifest or compiled thread limit")
        self.checkout = self._resolve(manifest["checkout"])
        if self.binary != self._resolve(manifest["binary"]) or self.config != self._resolve(manifest["config"]):
            raise ValueError("Akochan binary/config disagree with manifest")
        declared = {}
        for relative, expected in manifest["files"].items():
            path = self._resolve(relative)
            if path in declared or not isinstance(expected, str) or _sha256(path) != expected:
                raise ValueError(f"Akochan artifact hash mismatch: {relative}")
            declared[path] = expected
        params = {p.resolve() for p in (self.checkout / "params").rglob("*") if p.is_file()}
        listed_params = {p for p in declared if self.checkout / "params" in p.parents}
        if not params or params != listed_params or self.binary not in declared or self.config not in declared:
            raise ValueError("Akochan manifest must bind binary, config and every parameter")

    def reset(self, context: Context) -> None:
        self.close()
        self._verify_artifacts()
        self._context = context
        self._pending = self._awaiting = None
        self._buffer = b""
        self._seen_start = False

    def _start(self) -> None:
        # A managed worker owns the whole descendant group. Standalone use owns
        # a separate group so close() can also reap the opponent's descendants.
        inherited = os.environ.get("AGARILAB_AGENT_PROCESS_GROUP") == str(os.getpgrp())
        self._owns_group = not inherited
        environment = os.environ.copy()
        environment.pop("RIICHI_BOT_TOKEN", None)
        environment.update(OMP_NUM_THREADS=str(self.threads), OMP_THREAD_LIMIT=str(self.threads))
        self._process = subprocess.Popen(
            [str(self.binary), "pipe", str(self.config), str(self._context.player_id)],
            cwd=self.checkout, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=self._owns_group,
        )
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)

    @staticmethod
    def _wait(file, event: int, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Akochan decision timed out")
        with selectors.DefaultSelector() as selector:
            selector.register(file, event)
            if not selector.select(remaining):
                raise TimeoutError("Akochan decision timed out")

    def _write(self, events: list[dict], deadline: float) -> None:
        try:
            unexpected = os.read(self._process.stdout.fileno(), 65536)
        except BlockingIOError:
            pass
        else:
            raise AkochanError("Akochan sent unsolicited output" if unexpected else "Akochan process exited")
        payload = memoryview("".join(canonical_json(event) + "\n" for event in events).encode())
        while payload:
            self._wait(self._process.stdin, selectors.EVENT_WRITE, deadline)
            try:
                payload = payload[os.write(self._process.stdin.fileno(), payload):]
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise AkochanError("Akochan exited while receiving events") from exc

    def _read(self, deadline: float) -> list[dict]:
        while b"\n" not in self._buffer:
            self._wait(self._process.stdout, selectors.EVENT_READ, deadline)
            try:
                chunk = os.read(self._process.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise AkochanError("Akochan exited without a decision")
            self._buffer += chunk
            if len(self._buffer) > 2**20:
                raise AkochanError("Akochan response is too large")
        line, self._buffer = self._buffer.split(b"\n", 1)
        if self._buffer:
            raise AkochanError("Akochan sent unsolicited extra output")
        try:
            moves = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise AkochanError("Akochan returned invalid JSON") from exc
        if not isinstance(moves, list) or not 1 <= len(moves) <= 2:
            raise AkochanError("Akochan must return one action or a call/reach plus discard")
        for move in moves:
            if isinstance(move, dict) and move.get("type") == "ryukyoku" and "reason" in move:
                if move["reason"] != "kyushukyuhai":
                    raise AkochanError("Akochan requested an unsupported abortive draw")
                move.pop("reason")  # The native legal action itself is KyushuKyuhai.
            if isinstance(move, dict) and move.get("type") == "none" and "actor" in move:
                if type(move["actor"]) is not int or move["actor"] != self._context.player_id:
                    raise AkochanError("Akochan pass belongs to another player")
                move.pop("actor")  # Original pipe includes actor on pass.
            if not isinstance(move, dict) or move.get("type") not in FIELDS or set(move) - set(FIELDS[move["type"]]):
                raise AkochanError("Akochan returned unsupported action fields")
            clean_action(move)
        if len(moves) == 2 and (moves[0]["type"] not in ("reach", "chi", "pon") or moves[1]["type"] != "dahai"):
            raise AkochanError("Akochan returned an invalid action sequence")
        return moves

    @staticmethod
    def _normalize(move: dict) -> dict:
        move = dict(move)
        if "consumed" in move:
            move["consumed"] = sorted(move["consumed"])
        return move

    def _match(self, move: dict, observation, events: list[dict]):
        """Match all MJAI fields to actual legal actions, including red identity."""
        seat = self._context.player_id
        trigger = next((event for event in reversed(events)
                        if event["type"] in ("tsumo", "dahai", "kakan", "ankan")), None)
        wanted = self._normalize(move)
        for action in observation.legal_actions():
            native = json.loads(action.to_mjai())
            kind = native["type"]
            if kind != move.get("type"):
                continue
            if kind == "none":
                native = {"type": "none"}
            else:
                native["actor"] = seat
            if kind == "dahai":
                native["tsumogiri"] = is_tsumogiri(action, observation)
            elif kind in ("chi", "pon", "daiminkan"):
                if trigger is None or trigger["type"] != "dahai" or trigger["actor"] == seat:
                    continue
                native["target"] = trigger["actor"]
            elif kind == "hora":
                if trigger is None or trigger["type"] not in ("tsumo", "dahai", "kakan"):
                    continue
                native.update(target=trigger["actor"], pai=trigger["pai"])
            elif kind == "ankan":
                # RiichiEnv may carry a redundant physical tile for ankan;
                # MJAI's canonical ankan contains only its four consumed tiles.
                native.pop("pai", None)
            if self._normalize(native) == wanted:
                return action
        raise AkochanError("Akochan action disagrees with native legal actions")

    def act(self, observation, context: Context):
        try:
            return self._act(observation, context)
        except BaseException:
            self.close()
            raise

    def _act(self, observation, context: Context):
        if (self._context is None or context.player_id != self._context.player_id
                or context.seed != self._context.seed or context.rules != self._context.rules
                or observation.player_id != context.player_id):
            raise AkochanError("Akochan requires reset with this game's context")
        deadline = min(time.monotonic() + self.timeout_seconds,
                       context.deadline if context.deadline is not None else float("inf"))
        self._verify_artifacts()  # Parameters are loaded lazily by the native AI.
        if time.monotonic() >= deadline:
            raise TimeoutError("Akochan decision timed out")
        # In pinned 0.4.10 events and new_events() contain the same native delta;
        # use the semantic API rather than infer a cursor from its stale .pyi.
        events = [player_event(json.loads(raw) if isinstance(raw, str) else raw, context.player_id)
                  for raw in observation.new_events()]
        if not events:
            raise AkochanError("Akochan needs player-visible event deltas")
        for event in events:
            event["can_act"] = False
            if event["type"] == "start_game":
                if self._seen_start:
                    raise AkochanError("Duplicate start_game in observation deltas")
                self._seen_start = True
                event.update(kyoku_first=0 if context.rules.game_mode == "4p-red-half" else 4, aka_flag=True)
        if not self._seen_start:
            raise AkochanError("Akochan cannot start from a partial game history")
        if self._process is None:
            self._start()
        if self._process.poll() is not None:
            raise AkochanError("Akochan process exited")
        if self._pending is not None:
            accepted = any(self._normalize({key: event[key] for key in self._awaiting if key in event})
                           == self._normalize(self._awaiting) for event in events)
            if accepted:
                pending, self._pending = self._pending, None
                self._awaiting = None
                self._write(events, deadline)
                return self._match(pending, observation, events)
            self._pending = self._awaiting = None  # A competing claim won.
        legal = observation.legal_actions()
        if not legal:
            raise AkochanError("No legal decision is available")
        final = events[-1]
        trigger_index = len(events) - 1
        while trigger_index >= 0 and events[trigger_index]["type"] == "dora":
            trigger_index -= 1
        decision = events[trigger_index] if trigger_index >= 0 else {}
        trigger = ((decision.get("type") == "tsumo" and decision.get("actor") == context.player_id)
                   or (decision.get("type") in ("dahai", "kakan") and decision.get("actor") != context.player_id))
        if not trigger:
            # The original pipe does not request responses to opponent ankan.
            # A mandatory pass is exact; a possible kokushi chankan is unsupported.
            if (final["type"] == "ankan" and final.get("actor") != context.player_id
                    and len(legal) == 1 and json.loads(legal[0].to_mjai())["type"] == "none"):
                self._write(events, deadline)
                return legal[0]
            raise AkochanError("Original Akochan pipe cannot decide on this event")
        # A prior kan's delayed dora can follow a new kakan's chankan trigger.
        # Preserve wire order: the original pipe decides at kakan, then consumes
        # trailing dora without deciding again. It does not use that late dora
        # in this decision; never reorder or duplicate native events to fake it.
        decision["can_act"] = True
        self._write(events, deadline)
        moves = self._read(deadline)
        action = self._match(moves[0], observation, events)
        if len(moves) == 2:
            self._awaiting, self._pending = moves
        return action

    def close(self) -> None:
        process, self._process = self._process, None
        self._context = None
        self._pending = self._awaiting = None
        if process is None:
            return

        def signal_group(sig):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS sandbox may report EPERM, rather than ESRCH, for a
                # process group whose last member just exited. Reap the child
                # before deciding whether this was an actual cleanup failure.
                if process.poll() is None:
                    raise

        try:
            if self._owns_group:
                signal_group(signal.SIGTERM)
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                if not self._owns_group:
                    process.kill()
                else:
                    signal_group(signal.SIGKILL)
                process.wait(timeout=1)
            if self._owns_group:
                # The leader may have exited while a descendant ignored TERM.
                signal_group(signal.SIGKILL)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
