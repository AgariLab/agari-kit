"""Player-visible, isolated-process bridge to a separately installed Mortal.

No Mortal source or model tensors are included in this Apache-licensed module.
The selected runtime, licenses and weights live in a hash-bound external bundle.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time

from ..agent import Context, is_tsumogiri
from ..mjai import canonical_json, player_event
from ..online import FIELDS, clean_action


class MortalError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class MortalAgent:
    def __init__(self, *, manifest_path: str, threads: int = 1,
                 timeout_seconds: float = 10, startup_timeout_seconds: float = 60):
        verification_started = time.monotonic()
        if (type(threads) is not int or threads < 1
                or any(not math.isfinite(x) or x <= 0
                       for x in (timeout_seconds, startup_timeout_seconds))):
            raise ValueError("Invalid Mortal thread count or timeout")
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest_sha256 = sha256(self.manifest_path)
        self._manifest_stat = self._stat(self.manifest_path)
        self.manifest = json.loads(self.manifest_path.read_text())
        self.threads = threads
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self._process = None
        self._context = None
        self._owns_group = False
        self._buffer = b""
        self._serial = 0
        self._seen_start = False
        self._stderr_handle = None
        self.stderr_path = None
        self.initialization_seconds = None
        self._verify_artifacts(full=True)
        self.timings = {"construction_seconds": time.monotonic() - verification_started,
                        "reset_artifact_verification_seconds": [], "reset_seconds": []}

    def _resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Mortal bundle paths must be relative")
        path = (self.manifest_path.parent / relative).resolve()
        if self.manifest_path.parent not in path.parents:
            raise ValueError("Mortal bundle path escapes manifest directory")
        return path

    @staticmethod
    def _stat(path: Path) -> tuple:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _verify_artifacts(self, *, full=False) -> None:
        if self._stat(self.manifest_path) != self._manifest_stat:
            raise ValueError("Mortal manifest changed")
        manifest = self.manifest
        if manifest.get("schema_version") != 1 or manifest.get("kind") != "mortal-runtime-v1":
            raise ValueError("Unsupported Mortal runtime manifest")
        if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
            raise ValueError("Mortal manifest must bind runtime and weights")
        self.checkout = self._resolve(manifest["checkout"])
        self.python = Path(manifest["python"]["path"])
        if not self.python.is_absolute():
            raise ValueError("Mortal interpreter must be absolute")
        if full:
            identities = {}
            for relative, expected in manifest["files"].items():
                path = self._resolve(relative)
                if path in identities or sha256(path) != expected:
                    raise ValueError(f"Mortal artifact hash mismatch: {relative}")
                identities[path] = self._stat(path)
            required = {self.checkout / "inference.py", self.checkout / "model-identity.json",
                        self.checkout / "weights/s42-inference.pth", self.checkout / "runtime/LICENSE"}
            if not required.issubset(identities):
                raise ValueError("Mortal manifest omits required runtime/weight/license files")
            if sha256(self.python.resolve()) != manifest["python"]["sha256"]:
                raise ValueError("Mortal interpreter hash mismatch")
            identities[self.python] = self._stat(self.python)
            self._identities = identities
            if "environment_manifest" not in manifest:
                raise ValueError("Mortal manifest must bind its isolated environment inventory")
            if "environment_manifest" in manifest:
                inventory_path = self._resolve(manifest["environment_manifest"])
                if inventory_path not in identities:
                    raise ValueError("Unbound Mortal dependency inventory")
                inventory = json.loads(inventory_path.read_text())
                if inventory.get("schema_version") != 1 or not isinstance(inventory.get("files"), dict) or not inventory["files"]:
                    raise ValueError("Invalid Mortal dependency inventory")
                if not hasattr(self, "_runtime_identities"):
                    self._runtime_identities = {}
                    for relative, expected in inventory["files"].items():
                        path = self._resolve(relative)
                        if sha256(path) != expected:
                            raise ValueError(f"Mortal dependency hash mismatch: {relative}")
                        self._runtime_identities[path] = self._stat(path)
                elif any(self._stat(path) != identity for path, identity in self._runtime_identities.items()):
                    raise ValueError("Mortal dependency files changed between games")
        elif any(self._stat(path) != identity for path, identity in self._identities.items()):
            # Weights are loaded once. Reject subsequent bundle mutations instead
            # of re-reading tens of MB at every policy decision.
            raise ValueError("Mortal runtime bundle changed during the game")

    def _start(self) -> None:
        inherited = os.environ.get("AGARILAB_AGENT_PROCESS_GROUP") == str(os.getpgrp())
        self._owns_group = not inherited
        environment = os.environ.copy()
        for key in ("RIICHI_BOT_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
                    "PYTHONPATH", "PYTHONHOME"):
            environment.pop(key, None)
        environment.update(OMP_NUM_THREADS=str(self.threads), MKL_NUM_THREADS=str(self.threads),
                           PYTHONDONTWRITEBYTECODE="1")
        worker = Path(__file__).with_name("mortal_worker.py")
        logs = self.manifest_path.parent / "worker-logs"
        logs.mkdir(exist_ok=True)
        self._stderr_handle = tempfile.NamedTemporaryFile(prefix="worker-", suffix=".log", dir=logs, delete=False)
        self.stderr_path = self._stderr_handle.name
        self._process = subprocess.Popen(
            [str(self.python), "-I", "-B", str(worker), "--checkout", str(self.checkout),
             "--threads", str(self.threads)], cwd=self.checkout, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_handle,
            bufsize=0, start_new_session=self._owns_group)
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)
        self._buffer = b""
        self._serial = 0

    @staticmethod
    def _wait(stream, event, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Mortal worker timed out")
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            if not selector.select(remaining):
                raise TimeoutError("Mortal worker timed out")

    def _read(self, deadline):
        while b"\n" not in self._buffer:
            self._wait(self._process.stdout, selectors.EVENT_READ, deadline)
            try:
                chunk = os.read(self._process.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise MortalError("Mortal worker exited without a response")
            self._buffer += chunk
            if len(self._buffer) > 2**20:
                raise MortalError("Mortal response exceeds size limit")
        line, self._buffer = self._buffer.split(b"\n", 1)
        if self._buffer:
            raise MortalError("Mortal sent extra responses")
        try:
            result = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise MortalError("Invalid Mortal worker JSON") from exc
        if not isinstance(result, dict) or "error" in result:
            raise MortalError("Mortal worker failed: " + str(result.get("error") if isinstance(result, dict) else "invalid response"))
        return result

    def _request(self, command, deadline, **payload):
        if self._process.poll() is not None:
            raise MortalError("Mortal worker exited")
        try:
            unexpected = os.read(self._process.stdout.fileno(), 65536)
        except BlockingIOError:
            pass
        else:
            raise MortalError("Mortal sent unsolicited output" if unexpected else "Mortal worker exited")
        self._serial += 1
        message = memoryview((canonical_json({"id": self._serial, "command": command, **payload}) + "\n").encode())
        if len(message) > 2**20:
            raise MortalError("Mortal request exceeds size limit")
        while message:
            self._wait(self._process.stdin, selectors.EVENT_WRITE, deadline)
            try:
                message = message[os.write(self._process.stdin.fileno(), message):]
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise MortalError("Mortal worker exited during request") from exc
        result = self._read(deadline)
        if result.get("id") != self._serial:
            raise MortalError("Mortal response request id mismatch")
        return result

    def reset(self, context: Context) -> None:
        started_reset = time.monotonic()
        try:
            if (context.rules.preset != "tenhou" or context.rules.engine_version != "0.4.10"
                    or context.rules.game_mode != "4p-red-half"):
                raise ValueError("Mortal-S42 supports the pinned Tenhou four-player red half-game rules")
            verification_started = time.monotonic()
            self._verify_artifacts(full=True)
            self.timings["reset_artifact_verification_seconds"].append(time.monotonic() - verification_started)
            if self._process is None:
                started = time.monotonic()
                self._start()
                ready = self._read(started + self.startup_timeout_seconds)
                if ready != {"ready": True, "protocol": "agari-mortal-worker-v1"}:
                    raise MortalError("Unexpected Mortal worker startup")
                self.initialization_seconds = time.monotonic() - started
            response = self._request("reset", time.monotonic() + self.timeout_seconds, seat=context.player_id)
            if response != {"id": self._serial, "reset": True}:
                raise MortalError("Unexpected Mortal reset response")
            self._context = context
            self._seen_start = False
            self.timings["reset_seconds"].append(time.monotonic() - started_reset)
        except BaseException as exc:
            try:
                self.close()
            except Exception as cleanup_error:
                exc.add_note("Mortal cleanup verification: " + str(cleanup_error))
            raise

    @staticmethod
    def _normalize(move):
        result = dict(move)
        if "consumed" in result:
            result["consumed"] = sorted(result["consumed"])
        return result

    def _match(self, move, observation, events):
        if not isinstance(move, dict):
            raise MortalError("Mortal did not return an action object")
        move = dict(move)
        move.pop("meta", None)  # Upstream per-action Q diagnostics are not action fields.
        if move.get("type") not in FIELDS or set(move) - set(FIELDS[move["type"]]):
            raise MortalError("Unsupported Mortal action fields")
        clean_action(move)
        wanted = self._normalize(move)
        seat = self._context.player_id
        trigger = next((event for event in reversed(events)
                        if event["type"] in ("tsumo", "dahai", "kakan", "ankan")), None)
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
                if trigger is None:
                    continue
                native.update(target=trigger["actor"], pai=trigger.get("pai", move.get("pai")))
                # Libriichi's Hora event names actor/target, not the winning tile.
                # An explicitly supplied tile still has to match the live window.
                if "pai" not in wanted:
                    native.pop("pai", None)
            elif kind == "ryukyoku" and "actor" not in wanted:
                # Its only player-selectable draw is the native legal Kyushu action.
                native.pop("actor", None)
            elif kind == "ankan":
                native.pop("pai", None)
            if self._normalize(native) == wanted:
                return action
        raise MortalError("Mortal action disagrees with native legal actions")

    def act(self, observation, context: Context):
        try:
            if (self._context is None or context.player_id != self._context.player_id
                    or context.seed != self._context.seed or context.rules != self._context.rules
                    or observation.player_id != context.player_id):
                raise MortalError("Mortal requires reset with this game's context")
            deadline = min(time.monotonic() + self.timeout_seconds,
                           context.deadline if context.deadline is not None else float("inf"))
            self._verify_artifacts()
            events = [player_event(json.loads(raw) if isinstance(raw, str) else raw, context.player_id)
                      for raw in observation.new_events()]
            if not events:
                raise MortalError("Mortal requires nonempty player-visible event deltas")
            for event in events:
                if event["type"] == "start_game":
                    if self._seen_start:
                        raise MortalError("Duplicate start_game")
                    self._seen_start = True
            if not self._seen_start:
                raise MortalError("Mortal cannot start from partial game history")
            decision = len(events) - 1
            while decision >= 0 and events[decision]["type"] == "dora":
                decision -= 1
            if decision < 0:
                raise MortalError("No decision event before delayed dora")
            for index, event in enumerate(events):
                event["can_act"] = index == decision
            result = self._request("act", deadline, events=events)
            if set(result) != {"id", "action"}:
                raise MortalError("Unexpected Mortal action response")
            if result["action"] is None:
                legal = observation.legal_actions()
                if len(legal) == 1 and json.loads(legal[0].to_mjai())["type"] == "none":
                    return legal[0]
                raise MortalError("Mortal omitted a nontrivial decision")
            return self._match(result["action"], observation, events)
        except BaseException as exc:
            try:
                self.close()
            except Exception as cleanup_error:
                exc.add_note("Mortal cleanup verification: " + str(cleanup_error))
            raise

    def close(self) -> None:
        process, self._process = self._process, None
        self._context = None
        if process is None:
            if self._stderr_handle is not None:
                self._stderr_handle.close()
                self._stderr_handle = None
            return
        verification_error = None
        try:
            self._verify_artifacts(full=True)
        except Exception as exc:
            verification_error = exc
        def send(sig):
            try:
                if self._owns_group:
                    os.killpg(process.pid, sig)
                elif process.poll() is None:
                    process.send_signal(sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    raise
        try:
            send(signal.SIGTERM)
            try:
                process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                send(signal.SIGKILL)
                process.wait(timeout=1)
            if self._owns_group:
                send(signal.SIGKILL)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            if self._stderr_handle is not None:
                self._stderr_handle.close()
                self._stderr_handle = None
        if verification_error is not None:
            raise verification_error
