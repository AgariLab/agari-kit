"""Closed-hand baseline: minimum shanten, then visible-information ukeire.

The agent takes wins and riichi and declines optional calls. It is an engineering
baseline, not a claim of strong play. All hand calculations belong to RiichiEnv.
"""

from __future__ import annotations

import json
import time
from collections import Counter

from .agent import Context, TsumogiriAgent


class EfficiencyAgent:
    def reset(self, context: Context) -> None:
        pass

    def act(self, observation, context: Context):
        from riichienv import calculate_shanten

        for kind in ("hora", "reach"):
            action = observation.select_action_from_mjai({"type": kind, "actor": context.player_id})
            if action is not None:
                return action
        discards = [action for action in observation.legal_actions()
                    if json.loads(action.to_mjai())["type"] == "dahai"]
        if len(discards) < 2:
            return discards[0] if discards else TsumogiriAgent().act(observation, context)
        state = observation.to_dict()
        # Physical ids make a called discard appear only once when it is also
        # retained in the river. Never read state['hands'] for other players.
        visible = set(observation.hand)
        visible.update(state["dora_indicators"])
        for river in state["discards"]:
            visible.update(river)
        for melds in state["melds"]:
            for meld in melds:
                visible.update(meld.tiles)
        counts = Counter(tile // 4 for tile in visible)
        cache = {}
        best, best_key = discards[0], None
        for action in discards:
            if context.deadline is not None and time.monotonic() >= context.deadline:
                break
            kind = action.tile // 4
            if kind not in cache:
                hand = list(observation.hand)
                hand.remove(action.tile)
                shanten = calculate_shanten(hand)
                ukeire = 0
                for tile_kind in range(34):
                    remaining = 4 - counts[tile_kind]
                    if remaining <= 0:
                        continue
                    tile = next(tile for tile in range(tile_kind * 4, tile_kind * 4 + 4)
                                if tile not in visible)
                    if calculate_shanten(hand + [tile]) < shanten:
                        ukeire += remaining
                cache[kind] = shanten, -ukeire
            red = json.loads(action.to_mjai())["pai"].endswith("r")
            key = (*cache[kind], red, action.tile)
            if best_key is None or key < best_key:
                best, best_key = action, key
        return best
