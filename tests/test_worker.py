import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agari_kit.agent import Context
from agari_kit.online import DecisionTimeout, Request, WorkerError
from agari_kit.worker import ProcessRunner


def fixture_worker(connection, behavior):
    connection.send({"status": "ready"})
    try:
        while True:
            request, context = connection.recv()
            if behavior == "hang":
                time.sleep(30)
            elif behavior == "exit":
                break
            elif behavior == "credentials":
                connection.send({"status": "ok", "request_id": request.request_id,
                                 "action": {"type": "none", "bot_token_present": "RIICHI_BOT_TOKEN" in os.environ}})
            else:
                connection.send({"status": "ok", "request_id": request.request_id,
                                 "action": {"type": "none"}})
    except EOFError:
        pass
    finally:
        connection.close()


HANGING_FAMILY = """
import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen(
    [sys.executable, '-u', '-c',
     'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready", flush=True); time.sleep(60)'],
    stdout=subprocess.PIPE, text=True)
assert grandchild.stdout.readline().strip() == 'ready'
grandchild.stdout.close()
path = Path(sys.argv[1])
pending = path.with_suffix('.pending')
pending.write_text(json.dumps({
    'child': os.getpid(), 'grandchild': grandchild.pid, 'worker': os.getppid(),
    'group': os.getpgrp(), 'group_hint': os.environ.get('AGARILAB_AGENT_PROCESS_GROUP')}))
pending.replace(path)
time.sleep(60)
"""


def descendant_worker(connection, configuration):
    configuration = json.loads(configuration)
    path = Path(configuration["pid_file"])
    subprocess.Popen([sys.executable, "-c", HANGING_FAMILY, str(path)])
    try:
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("descendant_fixture_startup_timeout")
            time.sleep(.005)
        connection.send({"status": "ready"})
        connection.recv()
        if configuration["behavior"] == "exit":
            os._exit(7)  # Bypass finally, leaving both descendants alive.
        time.sleep(60)
    finally:
        connection.close()


def closing_native_worker(connection, marker):
    from agari_kit import worker

    class ClosingAgent:
        def close(self):
            Path(marker).write_text("closed")

    # No observation is decoded in this fixture; exercise the actual native
    # worker's finally block without requiring a model or engine dependency.
    sys.modules["riichienv"] = SimpleNamespace(Observation=object)
    worker.require_engine = lambda: None
    worker.make_agent = lambda name: ClosingAgent()
    worker._native_worker(connection, "fixture")


def process_is_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan killed by SIGKILL may briefly remain a zombie until init reaps
    # it; a zombie cannot execute or retain a running external engine.
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                            capture_output=True, text=True, check=False)
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_does_not_inherit_matchmaking_credentials(self):
        with patch.dict(os.environ, {"RIICHI_BOT_TOKEN": "fixture-secret"}):
            runner = ProcessRunner("credentials", worker_target=fixture_worker)
            try:
                await runner.start()
                request = Request(0, "test", [{"type": "none"}], time.monotonic() + 1, "test")
                reply = await runner.decide(request, Context(0, 0))
                self.assertFalse(reply["bot_token_present"])
                self.assertEqual(os.environ["RIICHI_BOT_TOKEN"], "fixture-secret")
            finally:
                await runner.close()

    async def test_same_worker_serves_multiple_decisions(self):
        runner = ProcessRunner("normal", worker_target=fixture_worker)
        await runner.start()
        process_id = runner.process.pid
        try:
            for number in range(3):
                request = Request(number, "test", [{"type": "none"}], time.monotonic() + 1, "test")
                self.assertEqual(await runner.decide(request, Context(0, 0)), {"type": "none"})
                self.assertEqual(runner.process.pid, process_id)
        finally:
            await runner.close()
        self.assertIsNone(runner.process)

    async def test_runner_can_restart_after_complete_cleanup(self):
        runner = ProcessRunner("normal", worker_target=fixture_worker)
        try:
            for _ in range(2):
                await runner.start()
                request = Request(0, "test", [{"type": "none"}], time.monotonic() + 1, "test")
                self.assertEqual(await runner.decide(request, Context(0, 0)), {"type": "none"})
                await runner.close()
                self.assertIsNone(runner.process)
        finally:
            await runner.close()

    async def test_timeout_reaps_hung_process(self):
        runner = ProcessRunner("hang", worker_target=fixture_worker)
        await runner.start()
        process_id = runner.process.pid
        request = Request(0, "test", [{"type": "none"}], time.monotonic() + .03, "test")
        with self.assertRaises(DecisionTimeout):
            await runner.decide(request, Context(0, 0))
        self.assertIsNone(runner.process)
        self.assertNotIn(process_id, [child.pid for child in multiprocessing.active_children()])

    async def test_worker_exit_is_reported_and_reaped(self):
        runner = ProcessRunner("exit", worker_target=fixture_worker)
        await runner.start()
        request = Request(0, "test", [{"type": "none"}], time.monotonic() + 1, "test")
        with self.assertRaises(WorkerError):
            await runner.decide(request, Context(0, 0))
        self.assertIsNone(runner.process)

    async def test_cancelled_decision_does_not_leave_background_work(self):
        runner = ProcessRunner("hang", worker_target=fixture_worker)
        await runner.start()
        request = Request(0, "test", [{"type": "none"}], time.monotonic() + 30, "test")
        task = asyncio.create_task(runner.decide(request, Context(0, 0)))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(runner.process)

    @unittest.skipUnless(os.name == "posix", "POSIX process group cleanup")
    async def test_close_never_signals_the_parent_process_group(self):
        runner = ProcessRunner("normal", worker_target=fixture_worker)
        await runner.start()
        runner._process_group = os.getpgrp()  # Invalid ownership must use the PID fallback.
        with patch("agari_kit.worker.os.killpg") as killpg:
            await runner.close()
            killpg.assert_not_called()
        self.assertIsNone(runner.process)

    @unittest.skipUnless(os.name == "posix", "POSIX graceful worker termination")
    async def test_termination_runs_native_agent_close(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "closed.txt"
            runner = ProcessRunner(str(marker), worker_target=closing_native_worker)
            try:
                await runner.start()
            finally:
                await runner.close()
            self.assertEqual(marker.read_text(), "closed")

    async def assert_descendants_cleaned(self, behavior):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "descendants.json"
            runner = ProcessRunner(json.dumps({"behavior": behavior, "pid_file": str(path)}),
                                   worker_target=descendant_worker)
            descendants = []
            try:
                await runner.start()
                info = json.loads(path.read_text())
                descendants = [info["child"], info["grandchild"]]
                self.assertEqual(info["worker"], runner.process.pid)
                self.assertEqual(info["group"], runner.process.pid)
                self.assertEqual(info["group_hint"], str(runner.process.pid))
                self.assertNotEqual(info["group"], os.getpgrp())
                self.assertTrue(all(process_is_running(pid) for pid in descendants))
                deadline = time.monotonic() + (.03 if behavior == "timeout" else 30)
                request = Request(0, "test", [{"type": "none"}], deadline, "test")
                if behavior == "cancel":
                    task = asyncio.create_task(runner.decide(request, Context(0, 0)))
                    await asyncio.sleep(.01)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    error = DecisionTimeout if behavior == "timeout" else WorkerError
                    with self.assertRaises(error):
                        await runner.decide(request, Context(0, 0))
                self.assertIsNone(runner.process)
                deadline = time.monotonic() + 2
                while any(process_is_running(pid) for pid in descendants) and time.monotonic() < deadline:
                    await asyncio.sleep(.01)
                self.assertFalse(any(process_is_running(pid) for pid in descendants))
            finally:
                await runner.close()
                for pid in descendants:
                    if process_is_running(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @unittest.skipUnless(os.name == "posix", "POSIX process group cleanup")
    async def test_timeout_kills_hanging_child_and_grandchild(self):
        await self.assert_descendants_cleaned("timeout")

    @unittest.skipUnless(os.name == "posix", "POSIX process group cleanup")
    async def test_cancel_kills_hanging_child_and_grandchild(self):
        await self.assert_descendants_cleaned("cancel")

    @unittest.skipUnless(os.name == "posix", "POSIX process group cleanup")
    async def test_worker_exit_still_kills_hanging_child_and_grandchild(self):
        await self.assert_descendants_cleaned("exit")


if __name__ == "__main__":
    unittest.main()
