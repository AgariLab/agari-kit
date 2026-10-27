"""Persistent native agent process; timeouts cancel work rather than orphaning a thread."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import time

from .agent import make_agent
from .engine import require_engine
from .online import DecisionTimeout, WorkerError, response_for_action


def _native_worker(connection, agent_name: str) -> None:
    agent = None
    try:
        require_engine()
        from riichienv import Observation
        agent = make_agent(agent_name)
        reset = False
        connection.send({"status": "ready"})
        while True:
            request, context = connection.recv()
            if not reset:
                agent.reset(context)
                reset = True
            observation = Observation.deserialize_from_base64(request.observation)
            if observation.player_id != context.player_id:
                raise WorkerError("observation_seat_mismatch")
            action = agent.act(observation, context)
            response = response_for_action(action, observation, request.possible_actions, context.player_id)
            connection.send({"status": "ok", "request_id": request.request_id, "action": response})
    except EOFError:
        pass
    except Exception:
        # Exceptions may carry remote data or model/config credentials. Only a
        # fixed error code crosses the worker boundary.
        try:
            connection.send({"status": "worker_failed"})
        except (OSError, EOFError):
            pass
    finally:
        try:
            close = getattr(agent, "close", None)
            if callable(close):
                close()
        except Exception:
            # Cleanup errors can also contain model paths or credentials.
            pass
        finally:
            connection.close()


def _terminate_worker(signum, frame) -> None:
    raise SystemExit(128 + signum)


def _worker_entry(connection, agent_name: str, target) -> None:
    """Establish ownership before an agent can launch external processes."""
    try:
        # Matchmaking credentials belong to the transport, never to an Agent.
        os.environ.pop("RIICHI_BOT_TOKEN", None)
        pid = os.getpid()
        group = None
        if os.name == "posix":
            os.setsid()
            group = os.getpgrp()
            os.environ["AGARILAB_AGENT_PROCESS_GROUP"] = str(group)
            signal.signal(signal.SIGTERM, _terminate_worker)
        else:
            os.environ.pop("AGARILAB_AGENT_PROCESS_GROUP", None)
        connection.send({"status": "worker_started", "pid": pid, "pgid": group})
        # If startup is cancelled before the parent confirms ownership, no agent
        # (and therefore no external child) has been started yet.
        if connection.recv() != {"status": "start"}:
            return
        target(connection, agent_name)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


def _group_exists(group: int) -> bool:
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(group: int, signum: int) -> None:
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        # The group can disappear between the existence probe and the signal.
        # Retain a fixed diagnostic if it still exists but cannot be signalled.
        if _group_exists(group):
            raise WorkerError("worker_process_group_signal_denied") from exc


class ProcessRunner:
    def __init__(self, agent_name: str, *, worker_target=_native_worker):
        self.agent_name, self.worker_target = agent_name, worker_target
        self.connection = self.process = None
        self._process_group = None
        self._closing_task = None

    async def start(self, *, timeout: float = 15.0) -> None:
        if self.process is not None or (self._closing_task is not None and not self._closing_task.done()):
            raise WorkerError("worker_already_started")
        self._closing_task = None
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.connection = parent
        self.process = context.Process(target=_worker_entry,
                                       args=(child, self.agent_name, self.worker_target), daemon=True)
        try:
            self.process.start()
            child.close()
            deadline = time.monotonic() + timeout
            response = await self._receive(deadline)
            group = self.process.pid if os.name == "posix" else None
            if response != {"status": "worker_started", "pid": self.process.pid, "pgid": group}:
                raise WorkerError("worker_process_group_unconfirmed")
            if group is not None:
                if group <= 1 or group == os.getpgrp() or os.getpgid(self.process.pid) != group:
                    raise WorkerError("worker_process_group_unconfirmed")
                self._process_group = group
            self.connection.send({"status": "start"})
            response = await self._receive(deadline)
            if response != {"status": "ready"}:
                raise WorkerError("worker_initialization_failed")
        except BaseException:
            child.close()
            await self.close()
            raise

    async def _receive(self, deadline: float) -> dict:
        while time.monotonic() < deadline:
            if self.connection is None or self.process is None:
                raise WorkerError("worker_not_running")
            if self.connection.poll():
                try:
                    return self.connection.recv()
                except (EOFError, OSError) as exc:
                    raise WorkerError("worker_disconnected") from exc
            if not self.process.is_alive():
                raise WorkerError("worker_exited")
            await asyncio.sleep(min(.005, max(0, deadline - time.monotonic())))
        raise DecisionTimeout("inference_deadline_exceeded")

    async def decide(self, request, context) -> dict:
        if self.connection is None:
            raise WorkerError("worker_not_running")
        try:
            self.connection.send((request, context))
            response = await self._receive(request.deadline)
            if response.get("status") != "ok" or response.get("request_id") != request.request_id:
                raise WorkerError("worker_decision_failed")
            return response["action"]
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._closing_task is None:
            self._closing_task = asyncio.create_task(self._close_process())
        # A second cancellation must not interrupt process-group cleanup.
        await asyncio.shield(self._closing_task)

    async def _close_process(self) -> None:
        process, self.process = self.process, None
        group, self._process_group = self._process_group, None
        connection, self.connection = self.connection, None
        try:
            if process is not None and process.pid is not None:
                # Only the group whose leader was our own spawned worker is owned.
                # Keep the recorded ID after worker death: grandchildren may remain.
                if os.name != "posix" or group != process.pid or group <= 1 or group == os.getpgrp():
                    group = None
                try:
                    if group is not None:
                        _signal_group(group, signal.SIGTERM)
                    elif process.is_alive():
                        process.terminate()
                    deadline = time.monotonic() + .2
                    while time.monotonic() < deadline:
                        alive = process.is_alive()
                        if group is not None and not _group_exists(group):
                            # Ownership ends with the group; never signal a later
                            # process that might reuse this numeric group ID.
                            group = None
                        if not alive and group is None:
                            break
                        await asyncio.sleep(.005)
                finally:
                    # Also run if event-loop shutdown cancels the cleanup task.
                    try:
                        if group is not None:
                            # A descendant may ignore TERM after the worker exits.
                            _signal_group(group, signal.SIGKILL)
                    finally:
                        if process.is_alive():
                            process.kill()
                        process.join(timeout=.2)
                        if process.is_alive():
                            raise WorkerError("worker_could_not_be_stopped")
                        process.close()
            elif process is not None:
                process.close()
        finally:
            # Do not race EOF against TERM while the worker runs agent.close().
            if connection is not None:
                connection.close()
