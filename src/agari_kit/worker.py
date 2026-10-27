"""Persistent native agent process; timeouts cancel work rather than orphaning a thread."""

from __future__ import annotations

import asyncio
import multiprocessing
import time

from .agent import make_agent
from .engine import require_engine
from .online import DecisionTimeout, WorkerError, response_for_action


def _native_worker(connection, agent_name: str) -> None:
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
        connection.close()


class ProcessRunner:
    def __init__(self, agent_name: str, *, worker_target=_native_worker):
        self.agent_name, self.worker_target = agent_name, worker_target
        self.connection = self.process = None

    async def start(self, *, timeout: float = 15.0) -> None:
        if self.process is not None:
            raise WorkerError("worker_already_started")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.connection = parent
        self.process = context.Process(target=self.worker_target, args=(child, self.agent_name), daemon=True)
        try:
            self.process.start()
            child.close()
            response = await self._receive(time.monotonic() + timeout)
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
        process, self.process = self.process, None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
            deadline = time.monotonic() + .2
            while process.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            if process.is_alive():
                process.kill()
            process.join(timeout=.2)
            if process.is_alive():
                raise WorkerError("worker_could_not_be_stopped")
            process.close()
