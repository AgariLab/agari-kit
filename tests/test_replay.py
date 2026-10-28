import base64
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agari_kit.cli import main
from agari_kit.mjai import DataError, inspect_game
from agari_kit.replay import display_events, export_replay


def fixture():
    """Display/settlement fixture, not a legal or engine-verified Mahjong game."""
    return [
        {"type": "start_game", "names": ["甲", "乙", "丙", "丁"]},
        {"type": "start_kyoku", "bakaze": "E", "kyoku": 1, "honba": 0, "kyotaku": 0,
         "oya": 0, "dora_marker": "1s", "scores": [25000] * 4,
         "tehais": [["1m"] * 13, ["2p"] * 13, ["3s"] * 13, ["E"] * 13]},
        {"type": "tsumo", "actor": 0, "pai": "5mr"},
        {"type": "reach", "actor": 0},
        {"type": "dahai", "actor": 0, "pai": "5mr", "tsumogiri": True},
        {"type": "reach_accepted", "actor": 0},
        {"type": "tsumo", "actor": 1, "pai": "9p"},
        {"type": "dahai", "actor": 1, "pai": "9p", "tsumogiri": True},
        {"type": "hora", "actor": 0, "target": 1, "deltas": [9000, -8000, 0, 0]},
        {"type": "end_kyoku"},
        {"type": "end_game", "scores": [33000, 17000, 25000, 25000]},
    ]


def unpack(document):
    data = re.search(r'<script id="agari-replay-data"[^>]*>([^<]+)</script>', document)[1]
    return json.loads(base64.b64decode(data))


def draw_conflict_fixture():
    """Display fixture using #247's recorded score pattern, not a legal game."""
    start = fixture()[1]
    start.update(scores=[32600, 23500, 17400, 26500], honba=2)
    following = json.loads(json.dumps(start))
    following.update(scores=[31600, 23500, 16400, 25500], honba=3, kyotaku=3)
    return [
        {"type": "start_game"}, start,
        *({"type": "reach_accepted", "actor": p} for p in [3, 2, 0]),
        {"type": "ryukyoku", "deltas": [-1000, 0, -1000, -1000]},
        {"type": "end_kyoku"}, following,
        {"type": "ryukyoku", "deltas": [0, 0, 0, 0]},
        {"type": "end_kyoku"},
        {"type": "end_game", "scores": [34600, 23500, 16400, 25500]},
    ]


class ReplayTests(unittest.TestCase):
    def exported(self, events, player=None):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "game.jsonl", Path(tmp) / "view.html"
            original = "".join(json.dumps(e) + "\n" for e in events)
            source.write_text(original)
            with patch("agari_kit.replay._viewer_assets", return_value=(b"\x1f\x8bfake", "License")):
                export_replay(source, target, player=player)
            self.assertEqual(source.read_text(), original)
            return unpack(target.read_text())

    def test_conflicting_draw_keeps_source_and_does_not_backfill_future_scores(self):
        events = draw_conflict_fixture()
        untouched = json.loads(json.dumps(events))
        data = self.exported(events, player=0)
        self.assertEqual(events, untouched)
        self.assertEqual(data["events"][5], events[5])
        self.assertEqual(data["score_steps"][4]["scores"], [31600, 23500, 16400, 25500])
        self.assertEqual(data["score_steps"][5], {
            "basis": "derived", "scores": [30600, 23500, 15400, 24500]})
        self.assertEqual(data["score_steps"][6], data["score_steps"][5])
        self.assertEqual(data["score_steps"][7], {
            "basis": "recorded", "scores": events[7]["scores"]})
        self.assertEqual(data["score_conflicts"], [{
            "settlement_event": 6, "next_round_event": 8,
            "settlement_scores": [30600, 23500, 15400, 24500],
            "next_round_scores": [31600, 23500, 16400, 25500],
        }])
        self.assertEqual(data["manifest"]["score_conflicts"], 1)
        self.assertEqual(data["events"][1]["tehais"][1:], [["?"] * 13] * 3)

    def test_scores_override_deltas_and_matching_draws_are_not_flagged(self):
        for recorded in (False, True):
            events = draw_conflict_fixture()
            if recorded:
                events[5]["scores"] = events[7]["scores"][:]
            else:
                events[5]["deltas"] = [0] * 4
            with self.subTest(recorded=recorded):
                data = self.exported(events)
                self.assertEqual(data["score_conflicts"], [])
                self.assertEqual(data["score_steps"][5]["scores"], events[7]["scores"])
                self.assertEqual(data["score_steps"][5]["basis"],
                                 "recorded" if recorded else "derived")
                self.assertEqual(data["events"][5], events[5])

    def test_recorded_score_conflict_is_reported_without_diagnosing_riichi_bug(self):
        events = draw_conflict_fixture()
        events[5]["scores"] = [1, 2, 3, 4]
        data = self.exported(events)
        self.assertEqual(data["score_steps"][5], {"basis": "recorded", "scores": [1, 2, 3, 4]})
        self.assertEqual(data["score_conflicts"][0]["settlement_scores"], [1, 2, 3, 4])

    def test_end_game_does_not_confirm_or_repair_the_last_draw(self):
        for final_scores in ([34600, 23500, 16400, 25500], None):
            events = draw_conflict_fixture()[:7]
            events.append({"type": "end_game"})
            if final_scores is not None:
                events[-1]["scores"] = final_scores
            with self.subTest(final_scores=final_scores):
                data = self.exported(events)
                self.assertEqual(data["score_conflicts"], [])
                self.assertEqual(data["score_steps"][5]["scores"], [30600, 23500, 15400, 24500])
                self.assertEqual(data["score_steps"][-1], {
                    "basis": "recorded" if final_scores else "derived",
                    "scores": final_scores or [30600, 23500, 15400, 24500]})

    def test_scores_follow_recorded_deltas_and_riichi_deposits(self):
        original = fixture()
        viewed = display_events(original)
        self.assertEqual(viewed[8]["scores"], [33000, 17000, 25000, 25000])
        self.assertNotIn("scores", original[8])
        original[8]["scores"] = [1, 2, 3, 4]
        self.assertEqual(display_events(original)[8]["scores"], [1, 2, 3, 4])

    def test_player_export_physically_removes_hidden_fields_and_annotations(self):
        original = fixture()
        original[1]["wall"] = ["private-wall"]
        original[6]["meta"] = {"future": "private-analysis"}
        viewed = display_events(original, 0)
        self.assertEqual(viewed[1]["tehais"][0], ["1m"] * 13)
        self.assertEqual(viewed[1]["tehais"][1:], [["?"] * 13] * 3)
        self.assertEqual(viewed[6]["pai"], "?")
        self.assertEqual(viewed[7]["pai"], "9p")
        self.assertNotIn("private", json.dumps(viewed))
        self.assertEqual(original[6]["pai"], "9p")
        self.assertEqual(display_events(original)[6]["pai"], "9p")

    def test_bad_shapes_fail_before_creating_an_artifact(self):
        for index, update in [(2, {"actor": 9}), (1, {"scores": [1]}),
                              (2, {"pai": "bad-tile"}), (8, {"deltas": None}),
                              (2, {"type": "future-event"})]:
            events = fixture()
            events[index].update(update)
            with self.subTest(update=update), self.assertRaisesRegex(DataError, "event"):
                display_events(events)
        with self.assertRaises(DataError):
            display_events(fixture()[:-1])

    def test_html_is_safe_packaged_self_contained_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "game.jsonl"
            target = Path(tmp) / "view.html"
            events = fixture()
            attack = '</script><script>window.injected=true</script><img src="https://invalid.test/">'
            events[0]["names"][0] = attack
            source.write_text("".join(json.dumps(e) + "\n" for e in events))
            # Tiny fake assets keep this test independent of the engine install.
            with patch("agari_kit.replay._viewer_assets", return_value=(b"\x1f\x8bfake", "Test license")):
                result = export_replay(source, target, player=2)
            document = target.read_text()
            self.assertNotIn(attack, document)
            self.assertEqual(unpack(document)["events"][0]["names"][0], attack)
            self.assertIn("connect-src 'none'", document)
            self.assertIn("Test license", document)
            self.assertEqual(result["source_sha256"], inspect_game(events)["sha256"])
            self.assertEqual(result["view"], "player-2-visible")
            with self.assertRaisesRegex(DataError, "already exists"):
                export_replay(source, target)
            self.assertEqual(target.read_text(), document)
            self.assertEqual(list(Path(tmp).glob(".agari-replay-*")), [])

    def test_inspect_cli_stays_read_only_and_replay_writes_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "game.jsonl", Path(tmp) / "view.html"
            source.write_text("".join(json.dumps(e) + "\n" for e in fixture()))
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["data", "inspect", str(source)]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), inspect_game(fixture()))
            with patch("agari_kit.replay._viewer_assets", return_value=(b"\x1f\x8bfake", "License")), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["replay", str(source), "--output", str(target)]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["artifact"], "offline-mjai-replay")
            self.assertTrue(target.exists())

    def test_external_filesystem_needs_no_hardlinks_and_write_failure_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "game.jsonl", Path(tmp) / "view.html"
            source.write_text("".join(json.dumps(e) + "\n" for e in fixture()))
            with patch("agari_kit.replay._viewer_assets", return_value=(b"\x1f\x8bfake", "License")), \
                    patch("os.link", side_effect=OSError("hardlinks unsupported")) as link:
                with patch("agari_kit.replay.os.fsync", side_effect=OSError("volume write failed")):
                    with self.assertRaisesRegex(OSError, "volume write failed"):
                        export_replay(source, target)
                self.assertFalse(target.exists())
                export_replay(source, target)
                link.assert_not_called()
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
