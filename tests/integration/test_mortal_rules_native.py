"""The supported preset avoids two known cross-engine MJAI rule differences."""
import unittest

from agari_kit.engine import require_engine


class MortalRulesNativeTests(unittest.TestCase):
    def test_pinned_tenhou_contract(self):
        require_engine()
        from riichienv import GameRule
        tenhou = GameRule.default_tenhou()
        self.assertFalse(tenhou.allows_ron_on_ankan_for_kokushi_musou)
        self.assertFalse(tenhou.dealer_first_discard_is_tedashi)

    def test_majsoul_is_a_different_contract(self):
        from riichienv import GameRule
        majsoul = GameRule.default_mjsoul()
        self.assertTrue(majsoul.allows_ron_on_ankan_for_kokushi_musou)
        self.assertTrue(majsoul.dealer_first_discard_is_tedashi)


if __name__ == "__main__":
    unittest.main()
