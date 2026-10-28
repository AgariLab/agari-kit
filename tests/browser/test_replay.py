"""Opt-in real Chrome regression suite; no browser driver/package download.

Run separately from unit/engine tests. CHROME_BIN may override local Chrome.
AGARI_REPLAY_TRAIN_MANIFEST and AGARILAB_STORAGE_ROOT identify a frozen training
partition. Only a train game is opened; held-out game contents are not read.
AGARI_REPLAY_TRAIN_GAME may choose a basename in train instead of its first game.
AGARI_REPLAY_EVIDENCE_DIR optionally retains HTML, screenshots and provenance.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from websockets.sync.client import connect

from agari_kit.mjai import inspect_game, read_events
from agari_kit.replay import display_events, export_replay


class Chrome:
    def __init__(self):
        binary = os.environ.get("CHROME_BIN") or shutil.which("chromium") or shutil.which("google-chrome")
        if not binary:
            binary = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if not Path(binary).is_file():
            raise RuntimeError("Browser tests require Chrome/Chromium; set CHROME_BIN")
        self.profile = tempfile.TemporaryDirectory(prefix="agari-replay-chrome-")
        self.stderr = open(Path(self.profile.name) / "chrome.log", "wb")
        self.process = subprocess.Popen([
            binary, "--headless=new", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=" + self.profile.name, "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--disable-extensions", "--metrics-recording-only", "--window-size=1440,1200", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=self.stderr)
        self.ws = None
        try:
            port_file = Path(self.profile.name) / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not port_file.is_file():
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Chrome did not start: " + (Path(self.profile.name) / "chrome.log").read_text())
                time.sleep(0.05)
            port, browser_path = port_file.read_text().splitlines()[:2]
            self.ws = connect(f"ws://127.0.0.1:{port}{browser_path}", proxy=None, max_size=20 * 1024 * 1024)
            self.counter = 0
            self.events = []
            self.session = None
            target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            self.call("Runtime.enable")
            self.call("Network.enable")
            self.call("Page.enable")
            self.call("Network.setBlockedURLs", {"urls": ["http://*", "https://*"]})
            self.call("Network.emulateNetworkConditions", {
                "offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0,
            })
        except Exception:
            self.close()
            raise

    def call(self, method, params=None):
        self.counter += 1
        message = {"id": self.counter, "method": method, "params": params or {}}
        if self.session:
            message["sessionId"] = self.session
        self.ws.send(json.dumps(message))
        while True:
            response = json.loads(self.ws.recv(timeout=20))
            if response.get("id") == self.counter:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result", {})
            self.events.append(response)

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True})
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result["result"].get("value")

    def open(self, path):
        self.events.clear()
        self.call("Page.navigate", {"url": path.resolve().as_uri()})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self.evaluate("document.documentElement.dataset.replayReady")
            if status == "true":
                self.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                return
            if status == "error":
                raise AssertionError(self.evaluate("document.getElementById('error').textContent"))
            time.sleep(0.05)
        raise AssertionError("Replay did not render before deadline")

    def state(self):
        return self.evaluate("agariReplay.viewer.gameState.getState()")

    def click(self, element_id):
        self.evaluate("document.getElementById(" + json.dumps(element_id) + ").click()")
        self.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")

    def seek(self, position):
        self.evaluate("(() => { const e=document.getElementById('seek'); e.value=" + str(position)
                      + "; e.dispatchEvent(new Event('input')); })()")
        self.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")

    def screenshot(self, path):
        self.evaluate("window.scrollTo(0, 0)")
        data = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        path.write_bytes(base64.b64decode(data["data"]))

    def close(self):
        if self.ws:
            self.ws.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.stderr.close()
        self.profile.cleanup()


class BrowserReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="agari-replay-evidence-")
        cls.output = Path(os.environ.get("AGARI_REPLAY_EVIDENCE_DIR", cls.temp.name))
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.chrome = Chrome()

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.temp.cleanup()

    def assert_offline_and_rendered(self):
        browser = self.chrome
        self.assertGreater(browser.evaluate("document.querySelectorAll('#viewer .tile-layer').length"), 40)
        self.assertGreater(browser.evaluate("document.getElementById('viewer').getBoundingClientRect().height"), 300)
        self.assertEqual(browser.evaluate("document.getElementById('error').textContent"), "")
        self.assertFalse(browser.evaluate("Boolean(window.injected)"))
        external = [e["params"]["request"]["url"] for e in browser.events
                    if e.get("method") == "Network.requestWillBeSent"
                    and e["params"]["request"]["url"].startswith(("http:", "https:"))]
        errors = [e for e in browser.events if e.get("method") == "Runtime.exceptionThrown"]
        self.assertEqual(external, [])
        self.assertEqual(errors, [])

    def test_real_train_game_navigation_scores_and_private_projection(self):
        manifest_path = Path(os.environ["AGARI_REPLAY_TRAIN_MANIFEST"])
        root = Path(os.environ["AGARILAB_STORAGE_ROOT"])
        manifest = json.loads(manifest_path.read_text())
        train = manifest["partitions"]["train"]
        selected = os.environ.get("AGARI_REPLAY_TRAIN_GAME")
        candidates = [e for e in train if Path(e["mjai_path"]).name == selected] if selected else train[:1]
        self.assertEqual(len(candidates), 1, "Selected game must occur exactly once in train")
        game = candidates[0]
        source = root / game["mjai_path"]
        original = list(read_events(source))
        self.assertEqual(inspect_game(original)["sha256"], game["sha256"])
        projected = display_events(original)
        steps = [e for e in projected if e["type"] != "start_game"]
        evidence = {"source_partition": "train", "game_id": game["game_id"],
                    "train_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    "source_sha256": game["sha256"], "views": []}
        for player in (None, 0):
            with self.subTest(player=player):
                view = "full" if player is None else "player-0"
                output = self.output / (view + ".html")
                result = export_replay(source, output, player=player)
                self.chrome.open(output)
                evidence["browser"] = self.chrome.evaluate("navigator.userAgent")
                initial = self.chrome.state()
                self.assertEqual([len(p["hand"]) for p in initial["players"]], [13] * 4)
                if player is not None:
                    self.assertTrue(all(p["hand"] == ["back"] * 13 for p in initial["players"][1:]))
                self.chrome.click("next")
                drawn = self.chrome.state()
                self.assertEqual(len(drawn["players"][steps[1]["actor"]]["hand"]), 14)
                self.chrome.click("next")
                discarded = self.chrome.state()
                self.assertEqual(discarded["players"][steps[2]["actor"]]["discards"][-1]["tile"], steps[2]["pai"])
                self.assertEqual(len(discarded["players"][steps[2]["actor"]]["hand"]), 13)
                self.chrome.click("previous")
                self.assertEqual(self.chrome.state()["players"], drawn["players"])
                self.chrome.seek(20)
                if player is not None:
                    self.assertTrue(all(all(t == "back" for t in p["hand"])
                                        for p in self.chrome.state()["players"][1:]))
                self.assert_offline_and_rendered()
                self.chrome.screenshot(self.output / (view + ".png"))
                for index, event in enumerate(steps, 1):
                    if event["type"] in ("hora", "ryukyoku"):
                        self.chrome.seek(index)
                        self.assertEqual([p["score"] for p in self.chrome.state()["players"]], event["scores"])
                        text = self.chrome.evaluate("document.getElementById('viewer').textContent")
                        for score in event["scores"]:
                            self.assertIn(str(score), text)
                # Native round buttons as well as the wrapper's selector work.
                self.chrome.seek(1)
                self.chrome.evaluate("document.querySelector('[title=\"Next Kyoku\"]').click()")
                second = next(e for e in steps[1:] if e["type"] == "start_kyoku")
                self.assertEqual([p["score"] for p in self.chrome.state()["players"]], second["scores"])
                self.chrome.evaluate("(() => { const e=document.getElementById('round'); e.value=0; e.dispatchEvent(new Event('change')); })()")
                self.assertEqual(self.chrome.state()["eventIndex"], 1)
                last_settlement = next(e for e in reversed(steps) if e["type"] in ("hora", "ryukyoku"))["scores"]
                self.chrome.seek(len(steps) - 1)
                self.assertEqual([p["score"] for p in self.chrome.state()["players"]], last_settlement)
                self.chrome.click("next")
                self.assertEqual([p["score"] for p in self.chrome.state()["players"]], original[-1]["scores"])
                self.chrome.click("previous")
                self.assertEqual([p["score"] for p in self.chrome.state()["players"]], last_settlement)
                self.chrome.click("next")
                self.chrome.screenshot(self.output / (view + "-terminal.png"))
                self.assert_offline_and_rendered()
                evidence["views"].append({**result, "last_settlement_scores": last_settlement,
                    "browser_checks": "render, steps, rewind, rounds, settlements, terminal pool award, offline"})
        (self.output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")

    def test_calls_red_tiles_hidden_counts_and_script_injection(self):
        # Purpose-built display fixture only, not a legally playable game.
        start = {"type": "start_kyoku", "bakaze": "E", "kyoku": 1, "honba": 0,
                 "kyotaku": 0, "oya": 0, "dora_marker": "1s", "scores": [25000] * 4,
                 "tehais": [["5mr", "1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "E", "S", "W"],
                            ["5m", "5m"] + ["2p"] * 11, ["2s"] * 13, ["N"] * 13]}
        events = [
            {"type": "start_game", "names": ['</script><script>window.injected=true</script>', "乙", "丙", "丁"]},
            start, {"type": "tsumo", "actor": 0, "pai": "1p"},
            {"type": "dahai", "actor": 0, "pai": "5mr", "tsumogiri": False},
            {"type": "pon", "actor": 1, "target": 0, "pai": "5mr", "consumed": ["5m", "5m"]},
            {"type": "dahai", "actor": 1, "pai": "2p", "tsumogiri": False},
            {"type": "tsumo", "actor": 1, "pai": "5m"},
            {"type": "kakan", "actor": 1, "pai": "5m", "consumed": ["5m", "5m", "5mr"]},
            {"type": "dora", "dora_marker": "9s"},
            {"type": "ryukyoku", "deltas": [0, 0, 0, 0]},
            {"type": "end_kyoku"}, {"type": "end_game", "scores": [25000] * 4},
        ]
        source = self.output / "display-fixture.jsonl"
        source.write_text("".join(json.dumps(e) + "\n" for e in events))
        for player in (None, 0):
            output = self.output / f"display-fixture-{player}.html"
            export_replay(source, output, player=player)
            self.chrome.open(output)
            self.chrome.seek(3)
            self.assertEqual(self.chrome.state()["players"][0]["discards"][0]["tile"], "5mr")
            self.chrome.seek(4)
            state = self.chrome.state()
            self.assertEqual(state["players"][0]["discards"], [])
            self.assertEqual(len(state["players"][1]["hand"]), 11)
            self.assertEqual(state["players"][1]["melds"][0]["tiles"], ["5m", "5m", "5mr"])
            self.chrome.seek(7)
            state = self.chrome.state()
            self.assertEqual(state["players"][1]["melds"][0]["type"], "kakan")
            self.assertEqual(len(state["players"][1]["melds"][0]["tiles"]), 4)
            self.assertEqual(len(state["players"][1]["hand"]), 10)
            self.chrome.seek(3)
            self.assertEqual(len(self.chrome.state()["players"][0]["discards"]), 1)
            self.assertEqual(len(self.chrome.state()["players"][1]["hand"]), 13)
            self.chrome.seek(8)
            self.assertEqual(self.chrome.state()["doraMarkers"], ["1s", "9s"])
            self.assert_offline_and_rendered()
            self.chrome.screenshot(self.output / f"meld-{player}.png")

    def test_final_riichi_pool_award_is_a_separate_reversible_event(self):
        events = [
            {"type": "start_game"},
            {"type": "start_kyoku", "bakaze": "S", "kyoku": 4, "honba": 0, "kyotaku": 0,
             "oya": 3, "dora_marker": "1s", "scores": [25000] * 4,
             "tehais": [["1m"] * 13 for _ in range(4)]},
            {"type": "reach_accepted", "actor": 1},
            {"type": "ryukyoku", "deltas": [0, 0, 0, 0]},
            {"type": "end_kyoku"},
            {"type": "end_game", "scores": [26000, 24000, 25000, 25000]},
        ]
        source = self.output / "terminal-fixture.jsonl"
        source.write_text("".join(json.dumps(e) + "\n" for e in events))
        target = self.output / "terminal-fixture.html"
        export_replay(source, target)
        self.chrome.open(target)
        self.chrome.seek(4)
        self.assertEqual([p["score"] for p in self.chrome.state()["players"]], [25000, 24000, 25000, 25000])
        self.chrome.click("next")
        self.assertEqual(self.chrome.state()["lastEvent"]["type"], "end_game")
        self.assertEqual([p["score"] for p in self.chrome.state()["players"]], events[-1]["scores"])
        self.chrome.click("previous")
        self.assertEqual([p["score"] for p in self.chrome.state()["players"]], [25000, 24000, 25000, 25000])
        self.chrome.click("next")
        self.assertEqual([p["score"] for p in self.chrome.state()["players"]], events[-1]["scores"])
        self.assert_offline_and_rendered()

    def test_chi_daiminkan_and_ankan_rewind_with_hidden_hands(self):
        cases = [
            ("chi", ["2m", "3m"], "1m", 11),
            ("daiminkan", ["5pr", "5p", "5p"], "5p", 10),
            ("ankan", ["7s"] * 4, None, 10),
        ]
        for kind, consumed, called, count in cases:
            with self.subTest(kind=kind):
                start = {"type": "start_kyoku", "bakaze": "E", "kyoku": 1, "honba": 0,
                         "kyotaku": 0, "oya": 0, "dora_marker": "1s", "scores": [25000] * 4,
                         "tehais": [[called or "1m"] + ["N"] * 12,
                                    consumed + ["N"] * (13 - len(consumed)), ["N"] * 13, ["N"] * 13]}
                call = {"type": kind, "actor": 1, "consumed": consumed}
                before = [{"type": "tsumo", "actor": 1 if kind == "ankan" else 0, "pai": "9s"}]
                if called:
                    before.append({"type": "dahai", "actor": 0, "pai": called, "tsumogiri": False})
                    call.update({"target": 0, "pai": called})
                events = [{"type": "start_game"}, start] + before + [call,
                          {"type": "ryukyoku", "deltas": [0] * 4}, {"type": "end_kyoku"},
                          {"type": "end_game", "scores": [25000] * 4}]
                source = self.output / (kind + ".jsonl")
                source.write_text("".join(json.dumps(e) + "\n" for e in events))
                output = self.output / (kind + ".html")
                export_replay(source, output, player=0)
                self.chrome.open(output)
                position = 2 + len(before)
                self.chrome.seek(position)
                state = self.chrome.state()
                self.assertEqual(state["players"][1]["melds"][0]["type"], kind)
                self.assertEqual(state["players"][1]["hand"], ["back"] * count)
                self.chrome.seek(1)
                self.assertEqual(self.chrome.state()["players"][1]["hand"], ["back"] * 13)
                self.chrome.seek(position)
                self.assertEqual(self.chrome.state()["players"][1]["hand"], ["back"] * count)
                self.assert_offline_and_rendered()


if __name__ == "__main__":
    unittest.main()
