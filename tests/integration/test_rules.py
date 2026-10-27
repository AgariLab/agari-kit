"""Explicit Tenhou 4p rule regressions against the pinned native engine.

Every ordinary fixture starts with a complete, unique 136-tile wall, and moves
are selected from native legal_actions. The chankan wall is adapted from
smly/RiichiEnv tests/env/actions/test_claim_lifecycle.py at
479c1faeb33d082965eef8198f63261a79c0fce3 (Copyright 2026 Kohei Ozaki,
Apache-2.0 terms retained in LICENSE). Adaptation changes the helper API and
adds inventory, scoring and rule-boundary assertions. Original source:
https://github.com/smly/RiichiEnv/blob/479c1faeb33d082965eef8198f63261a79c0fce3/tests/env/actions/test_claim_lifecycle.py
Other fixtures and assertions are original AgariLab tests.
"""

import random
import unittest

from riichienv import ActionType, GameRule, MeldType, Phase, RiichiEnv, calculate_shanten, parse_hand

from agari_kit.engine import require_engine
from agari_kit.online import response_for_action


def inventory(env):
    meld_tiles = [tile for melds in env.melds for meld in melds for tile in meld.tiles]
    tiles = env.wall + meld_tiles + [tile for hand in env.hands for tile in hand]
    tiles += [tile for river in env.discards for tile in river if tile not in meld_tiles]
    return sorted(tiles)


def position(hand_texts, draws, *, mode="4p-red-single", oya=0, round_wind=0,
             scores=None, reserved=()):
    """Deal distinct physical copies; fill other seats deterministically."""
    used = set(draws) | set(reserved)
    if len(used) != len(draws) + len(reserved):
        raise AssertionError("Fixture reserves a physical tile twice")
    hands = {}
    for seat, text in hand_texts.items():
        tiles, _ = parse_hand(text)
        hand = []
        for tile in tiles:
            # Keep an explicitly selected red five distinct from ordinary fives.
            candidates = [tile] if tile in (16, 52, 88) else [
                candidate for candidate in range(tile // 4 * 4, tile // 4 * 4 + 4)
                if candidate not in (16, 52, 88)
            ]
            chosen = next((candidate for candidate in candidates if candidate not in used), None)
            if chosen is None:
                raise AssertionError(f"Fixture has too many copies of tile type {tile // 4}")
            hand.append(chosen)
            used.add(chosen)
        if len(hand) != 13:
            raise AssertionError("Each specified starting hand must have 13 tiles")
        hands[seat] = hand
    pool = [tile for tile in range(136) if tile not in used]
    random.Random(17).shuffle(pool)
    for seat in range(4):
        if seat not in hands:
            hands[seat], pool = pool[:13], pool[13:]
    ordered = [hands[(oya + offset) % 4] for offset in range(4)]
    deal = [tile for start in (0, 4, 8) for hand in ordered for tile in hand[start:start + 4]]
    deal.extend(hand[12] for hand in ordered)
    wall = deal + list(draws) + pool + list(reserved)
    if sorted(wall) != list(range(136)):
        raise AssertionError("Fixture wall must contain every physical tile exactly once")
    env = RiichiEnv(mode, seed=17, rule=GameRule.default_tenhou())
    env.reset(wall=wall, oya=oya, round_wind=round_wind, scores=scores)
    return env


def action(env, seat, kind, *, tile=None):
    return next(candidate for candidate in env.get_observation(seat).legal_actions()
                if candidate.action_type == kind and (tile is None or candidate.tile == tile))


def has_action(env, seat, kind):
    return any(candidate.action_type == kind for candidate in env.get_observation(seat).legal_actions())


def pass_claims(env):
    if env.phase == Phase.WaitResponse:
        env.step({seat: action(env, seat, ActionType.PASS) for seat in env.active_players})


def discard(env, seat, tile):
    pass_claims(env)
    if env.current_player != seat:
        raise AssertionError(f"Expected player {seat}, got {env.current_player}")
    return env.step({seat: action(env, seat, ActionType.DISCARD, tile=tile)})


def chankan_position():
    """Legal pon -> daiminkan -> kakan sequence, retaining an obsolete chi action."""
    ready = [40, 44, 48, 4, 8, 12, 89, 92, 76, 80, 84, 108, 109]
    hands = [[], [tile for tile in ready if tile != 48] + [132], [], []]
    reserved = set(hands[1] + [36, 37, 38, 39, 96, 97, 98, 99, 48, 0, 1, 2, 3, 120])
    pool = [tile for tile in range(136) if tile not in reserved]
    hands[2], pool = [37, 38, 39, 97, 98] + pool[:8], pool[8:]
    after_pon = hands[2][-1]
    hands[0], pool = [96] + pool[:12], pool[12:]
    hands[3], pool = pool[:13], pool[13:]
    deal = [tile for start in (0, 4, 8) for hand in hands for tile in hand[start:start + 4]]
    deal.extend(hand[12] for hand in hands)
    wall = deal + [120, 0, 1, 48, 2, 3, 36] + pool + [99]
    if sorted(wall) != list(range(136)):
        raise AssertionError("Broken chankan fixture")
    env = RiichiEnv("4p-red-single", seed=42, rule=GameRule.default_tenhou())
    env.reset(wall=wall, oya=0)

    def claim(seat, kind):
        selected = action(env, seat, kind)
        env.step({player: selected if player == seat else action(env, player, ActionType.PASS)
                  for player in env.active_players})
        if inventory(env) != list(range(136)):
            raise AssertionError("Claim duplicated or lost a physical tile")

    discard(env, 0, 96)
    claim(2, ActionType.PON)
    discard(env, 2, after_pon)
    discard(env, 3, 0)
    discard(env, 0, 1)
    discard(env, 1, 132)
    discard(env, 2, 2)
    discard(env, 3, 3)
    discard(env, 0, 36)
    obsolete_chi = action(env, 1, ActionType.CHI)
    claim(2, ActionType.DAIMINKAN)
    if env.drawn_tile != 99:
        raise AssertionError("Unexpected rinshan tile")
    env.step({2: action(env, 2, ActionType.KAKAN, tile=99)})
    return env, obsolete_chi


def exhaust_first_hand(env):
    """Decline all calls/wins and tsumogiri until the first hand ends."""
    for _ in range(400):
        if any(event["type"] == "end_kyoku" for event in env.mjai_log):
            return
        if env.phase == Phase.WaitResponse:
            pass_claims(env)
        else:
            seat = env.current_player
            env.step({seat: action(env, seat, ActionType.DISCARD, tile=env.drawn_tile)})
    raise AssertionError("Fixture did not reach exhaustive draw")


class RuleRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_engine()

    def assertInventory(self, env):
        self.assertEqual(inventory(env), list(range(136)))

    def test_red_five_and_normal_five_have_distinct_actions_and_discards(self):
        for pai, tsumogiri, expected_tile in (("5mr", True, 16), ("5m", False, 17)):
            with self.subTest(pai=pai):
                env = position({0: "456m123p789s111z2z"}, [16])
                obs = env.get_observation(0)
                red = obs.select_action_from_mjai({"type": "dahai", "actor": 0, "pai": "5mr", "tsumogiri": True})
                normal = obs.select_action_from_mjai({"type": "dahai", "actor": 0, "pai": "5m", "tsumogiri": False})
                self.assertEqual(red.tile, 16)
                self.assertEqual(normal.tile, 17)
                self.assertNotEqual(red.tile, normal.tile)
                selected = red if tsumogiri else normal
                env.step({0: selected})
                event = next(event for event in env.mjai_log if event["type"] == "dahai")
                self.assertEqual(event["pai"], pai)
                self.assertEqual(event["tsumogiri"], tsumogiri)
                self.assertNotIn(expected_tile, env.hands[0])
                self.assertIn(normal.tile if tsumogiri else red.tile, env.hands[0])
                self.assertInventory(env)

    @unittest.expectedFailure
    def test_upstream_mjai_mapper_rejects_inconsistent_tsumogiri(self):
        # Known 0.4.10 compatibility fallback in observation/mjai_select.rs:
        # it prefers tsumogiri matches, but accepts the first pai match otherwise.
        # Replay validation must check this flag itself until upstream is stricter.
        env = position({0: "456m123p789s111z2z"}, [16])
        self.assertIsNone(env.get_observation(0).select_action_from_mjai(
            {"type": "dahai", "actor": 0, "pai": "5m", "tsumogiri": True}))

    def test_online_discard_flags_match_red_and_normal_physical_tiles(self):
        env = position({0: "456m123p789s111z2z"}, [16])
        observation = env.get_observation(0)
        for tile, pai, tsumogiri in ((16, "5mr", True), (17, "5m", False)):
            with self.subTest(pai=pai):
                expected = {"type": "dahai", "actor": 0, "pai": pai, "tsumogiri": tsumogiri}
                selected = action(env, 0, ActionType.DISCARD, tile=tile)
                self.assertEqual(response_for_action(selected, observation, [expected], 0), expected)

    def test_chi_only_next_seat_and_kuikae_is_forbidden(self):
        env = position({1: "123s123456m789p1z", 2: "13s234567p789m22z"}, [77])
        discard(env, 0, 77)
        self.assertTrue(has_action(env, 1, ActionType.CHI))
        self.assertFalse(has_action(env, 2, ActionType.CHI))
        chi = next(candidate for candidate in env.get_observation(1).legal_actions()
                   if candidate.action_type == ActionType.CHI
                   and {tile // 4 for tile in candidate.consume_tiles} == {18, 20})
        env.step({seat: chi if seat == 1 else action(env, seat, ActionType.PASS)
                  for seat in env.active_players})
        self.assertEqual(env.current_player, 1)
        self.assertEqual(env.melds[1][0].meld_type, MeldType.Chi)
        self.assertEqual(len(env.melds[1][0].tiles), 3)
        self.assertIsNone(env.get_observation(1).select_action_from_mjai({"type": "dahai", "actor": 1, "pai": "2s"}))
        self.assertInventory(env)

    def test_pon_takes_priority_over_chi(self):
        env = position({1: "13s123456m789p11z", 2: "22s234567p789m22z"}, [78])
        discard(env, 0, 78)
        chi = action(env, 1, ActionType.CHI)
        pon = action(env, 2, ActionType.PON)
        claims = {seat: action(env, seat, ActionType.PASS) for seat in env.active_players}
        claims.update({1: chi, 2: pon})
        env.step(claims)
        self.assertEqual(env.current_player, 2)
        self.assertFalse(env.melds[1])
        self.assertEqual(env.melds[2][0].meld_type, MeldType.Pon)
        self.assertEqual(len(env.hands[2]), 11)
        self.assertInventory(env)

    def test_ankan_reveals_dora_before_rinshan(self):
        env = position({0: "111m234p567s789s1z"}, [3])
        wall_size, dora_count = len(env.wall), len(env.dora_indicators)
        offset = len(env.mjai_log)
        env.step({0: action(env, 0, ActionType.ANKAN)})
        self.assertEqual(env.melds[0][0].meld_type, MeldType.Ankan)
        self.assertFalse(env.melds[0][0].opened)
        self.assertEqual(len(env.melds[0][0].tiles), 4)
        self.assertEqual(len(env.wall), wall_size - 1)
        self.assertEqual(len(env.dora_indicators), dora_count + 1)
        self.assertEqual(env.rinshan_draw_count, 1)
        events = [event["type"] for event in env.mjai_log[offset:]]
        self.assertLess(events.index("ankan"), events.index("dora"))
        self.assertLess(events.index("dora"), events.index("tsumo"))
        self.assertInventory(env)

    def test_reach_requires_a_second_legal_discard_and_charges_once(self):
        env = position({0: "33346m23477p345s"}, [11])
        self.assertTrue(has_action(env, 0, ActionType.ANKAN))
        env.step({0: action(env, 0, ActionType.RIICHI)})
        self.assertTrue(env.riichi_stage[0])
        self.assertFalse(env.riichi_declared[0])
        self.assertEqual(env.scores()[0], 25000)
        legals = env.get_observation(0).legal_actions()
        self.assertTrue(legals)
        self.assertEqual({candidate.action_type for candidate in legals}, {ActionType.DISCARD})
        env.step({0: legals[0]})
        pass_claims(env)
        self.assertFalse(env.riichi_stage[0])
        self.assertTrue(env.riichi_declared[0])
        self.assertEqual(env.scores()[0], 24000)
        self.assertEqual(env.riichi_sticks, 1)
        self.assertEqual(sum(event["type"] == "reach_accepted" for event in env.mjai_log), 1)
        self.assertEqual(sum(env.scores()) + env.riichi_sticks * 1000, 100000)
        self.assertInventory(env)

    def test_discard_furiten_blocks_all_waits_not_only_the_old_discard(self):
        for furiten in (False, True):
            with self.subTest(furiten=furiten):
                old_discard = 0 if furiten else 104
                env = position({1: "23m55p666s888p111z"}, [100, old_discard, 12])
                discard(env, 0, 100)
                discard(env, 1, old_discard)  # Previously discarded 1m (or safe 9s).
                discard(env, 2, 12)  # Now offered the other side of the wait: 4m.
                self.assertEqual(has_action(env, 1, ActionType.RON), not furiten)
                self.assertInventory(env)

    def test_discard_furiten_does_not_prevent_tsumo(self):
        env = position({1: "23m55p666s888p111z"}, [100, 0, 104, 105, 106, 12])
        discard(env, 0, 100)
        discard(env, 1, 0)
        discard(env, 2, 104)
        discard(env, 3, 105)
        discard(env, 0, 106)
        pass_claims(env)
        self.assertEqual(env.current_player, 1)
        self.assertTrue(has_action(env, 1, ActionType.TSUMO))
        env.step({1: action(env, 1, ActionType.TSUMO)})
        self.assertTrue(env.win_results[1].is_win)
        self.assertGreater(env.scores()[1], 25000)
        self.assertTrue(all(score < 25000 for seat, score in enumerate(env.scores()) if seat != 1))
        self.assertEqual(sum(env.scores()), 100000)
        self.assertInventory(env)

    def test_passing_ron_blocks_same_turn_then_clears_after_own_discard(self):
        env = position({3: "23m55p666s888p111z"}, [12, 13, 68, 104, 14])
        discard(env, 0, 12)
        self.assertTrue(has_action(env, 3, ActionType.RON))
        pass_claims(env)
        self.assertTrue(env.missed_agari_doujun[3])
        discard(env, 1, 13)
        self.assertFalse(has_action(env, 3, ActionType.RON))
        discard(env, 2, 68)
        discard(env, 3, 104)
        self.assertFalse(env.missed_agari_doujun[3])
        discard(env, 0, 14)
        self.assertTrue(has_action(env, 3, ActionType.RON))
        self.assertInventory(env)

    def test_riichi_pass_furiten_persists_after_own_discard(self):
        env = position({3: "23m55p666s888p111z"}, [100, 101, 102, 104, 12, 13, 68, 105, 14])
        discard(env, 0, 100)
        discard(env, 1, 101)
        discard(env, 2, 102)
        pass_claims(env)
        env.step({3: action(env, 3, ActionType.RIICHI)})
        discard(env, 3, 104)
        self.assertTrue(env.riichi_declared[3])
        discard(env, 0, 12)
        self.assertTrue(has_action(env, 3, ActionType.RON))
        pass_claims(env)
        discard(env, 1, 13)
        self.assertFalse(has_action(env, 3, ActionType.RON))
        discard(env, 2, 68)
        discard(env, 3, 105)
        discard(env, 0, 14)
        self.assertFalse(has_action(env, 3, ActionType.RON))
        self.assertInventory(env)

    def test_chankan_ron_preempts_rinshan_and_scores_the_winner(self):
        env, _ = chankan_position()
        self.assertEqual(env.phase, Phase.WaitResponse)
        self.assertEqual(env.active_players, [1])
        self.assertEqual({candidate.action_type for candidate in env.get_observation(1).legal_actions()},
                         {ActionType.RON, ActionType.PASS})
        before = env.scores()
        env.step({1: action(env, 1, ActionType.RON)})
        self.assertTrue(env.done())
        self.assertTrue(env.win_results[1].is_win)
        self.assertIn(3, env.win_results[1].yaku)  # RiichiEnv's chankan yaku id.
        self.assertEqual(env.rinshan_draw_count, 1)
        self.assertGreater(env.scores()[1], before[1])
        self.assertLess(env.scores()[2], before[2])
        self.assertEqual(sum(env.scores()), 100000)
        self.assertInventory(env)

    def test_chankan_pass_completes_added_kan_and_second_rinshan(self):
        env, _ = chankan_position()
        self.assertEqual(env.rinshan_draw_count, 1)  # Earlier daiminkan already drew once.
        pass_claims(env)
        self.assertFalse(env.done())
        self.assertEqual(env.current_player, 2)
        self.assertEqual(env.phase, Phase.WaitAct)
        self.assertEqual(env.rinshan_draw_count, 2)
        self.assertCountEqual([meld.meld_type for meld in env.melds[2]], [MeldType.Daiminkan, MeldType.Kakan])
        self.assertTrue(env.missed_agari_doujun[1])
        self.assertEqual(len(env.hands[2]) + 3 * len(env.melds[2]), 14)
        self.assertInventory(env)

    def test_obsolete_chi_is_rejected_during_chankan_without_tile_duplication(self):
        env, stale_chi = chankan_position()
        self.assertFalse(has_action(env, 1, ActionType.CHI))
        env.step({1: stale_chi})
        self.assertTrue(any(event.get("reason") == "Error: Illegal Action by Player 1" for event in env.mjai_log))
        self.assertFalse(env.win_results)
        self.assertInventory(env)

    def test_two_ron_winners_both_receive_payment(self):
        env = position({1: "12m456p789s111z22z", 2: "12m123p456s666z77z"}, [8])
        discard(env, 0, 8)
        self.assertTrue(has_action(env, 1, ActionType.RON))
        self.assertTrue(has_action(env, 2, ActionType.RON))
        claims = {seat: action(env, seat, ActionType.PASS) for seat in env.active_players}
        claims.update({seat: action(env, seat, ActionType.RON) for seat in (1, 2)})
        env.step(claims)
        self.assertTrue(env.done())
        self.assertEqual(set(env.win_results), {1, 2})
        wins = [event for event in env.mjai_log if event["type"] == "hora"]
        self.assertEqual({event["actor"] for event in wins}, {1, 2})
        self.assertEqual({event["target"] for event in wins}, {0})
        self.assertGreater(env.scores()[1], 25000)
        self.assertGreater(env.scores()[2], 25000)
        self.assertEqual(env.scores()[3], 25000)
        self.assertEqual(sum(env.scores()), 100000)
        self.assertInventory(env)

    def test_three_ron_is_abortive_draw_under_tenhou(self):
        env = position({seat: "123456789p234s1z" for seat in (1, 2, 3)}, [111])
        self.assertTrue(GameRule.default_tenhou().sanchaho_is_draw)
        discard(env, 0, 111)
        claims = {seat: action(env, seat, ActionType.RON) for seat in (1, 2, 3)}
        env.step(claims)
        self.assertFalse(any(event["type"] == "hora" for event in env.mjai_log))
        self.assertTrue(any(event.get("reason") == "sanchaho" for event in env.mjai_log))
        self.assertEqual(env.scores(), [25000] * 4)
        self.assertInventory(env)

    def test_exhaustive_draw_noten_payment_and_single_hand_termination(self):
        hands = {0: "147m258p369s1234z", 1: "123456789m123p1z",
                 2: "258m369p147s2345z", 3: "369m147p258s3456z"}
        env = position(hands, [135])
        exhaust_first_hand(env)
        self.assertTrue(env.done())
        self.assertEqual([calculate_shanten(hand) == 0 for hand in env.hands], [False, True, False, False])
        self.assertEqual(env.scores(), [24000, 28000, 24000, 24000])
        self.assertEqual([event["type"] for event in env.mjai_log[-3:]], ["ryukyoku", "end_kyoku", "end_game"])
        self.assertEqual(len(env.wall), 14)
        self.assertInventory(env)

    def test_hanchan_ends_after_south_four_but_not_east_four(self):
        for wind, should_end in ((0, False), (1, True)):
            with self.subTest(wind=wind):
                env = position({3: "147m258p369s1234z"}, [135], mode="4p-red-half",
                               oya=3, round_wind=wind, scores=[40000, 20000, 20000, 20000])
                exhaust_first_hand(env)
                self.assertEqual(env.done(), should_end)
                if should_end:
                    self.assertEqual(env.mjai_log[-1]["type"], "end_game")
                    self.assertEqual(env.ranks()[0], 1)
                else:
                    self.assertEqual(env.round_wind, 1)
                    self.assertEqual(env.oya, 0)


if __name__ == "__main__":
    unittest.main()
