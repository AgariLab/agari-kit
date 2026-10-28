import json
import tempfile
import unittest
from pathlib import Path

from agari_kit.evaluation import evaluate
from agari_kit.evaluation_campaign import evaluate_campaign, verify_campaign


class NativeCampaignTests(unittest.TestCase):
    def test_native_shards_equal_serial_games_and_candidate_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeds, lineup = [910001, 910002], ["efficiency"] * 4
            campaign = root / "campaign"
            evaluate_campaign(seeds, lineup, campaign, shard_seed_count=1, shard_index=0)
            evaluate_campaign(seeds, lineup, campaign, shard_seed_count=1, shard_index=1, resume=True)
            merged = evaluate_campaign(seeds, lineup, campaign, shard_seed_count=1, resume=True, merge_only=True)
            serial = evaluate(seeds, lineup, root / "serial")
            self.assertEqual(merged["candidate"], serial["candidate"])
            self.assertEqual(merged["candidate_diagnostics"], serial["candidate_diagnostics"])
            self.assertGreater(merged["candidate_diagnostics"]["hands"], 0)
            self.assertEqual(verify_campaign(campaign), merged)
            rows = lambda directory: [json.loads(line) for line in (directory / "games.jsonl").read_text().splitlines()]
            for grouped, single in zip(rows(campaign), rows(root / "serial"), strict=True):
                for field in ("seed", "rotation", "scores", "ranks", "candidate_diagnostics", "event_counts"):
                    self.assertEqual(grouped[field], single[field])


if __name__ == "__main__":
    unittest.main()
