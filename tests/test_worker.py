import asyncio
import multiprocessing
import time
import unittest

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
            else:
                connection.send({"status": "ok", "request_id": request.request_id,
                                 "action": {"type": "none"}})
    except EOFError:
        pass
    finally:
        connection.close()


class WorkerTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
