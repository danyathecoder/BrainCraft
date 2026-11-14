"""Fun mode director: tension arcs (P2). Only after rehab mode works.

Goal: keep engagement_z in [-0.5, 1.0].
  too calm for 15 s -> escalate: faster spawns, mobs with AI walking
    to the walls, darkness effect, cave sounds, sheep distractors
  overloaded (fatigue high or 3 misses in a row) -> 20 s breather,
    then a 5-mob wave
  jaw clench -> "flare": targets glow for 5 s, 30 s cooldown
"""

from __future__ import annotations

from contracts import BrainState
from director.base import Decision


class FunDirector:
    def on_trial_result(self, side: str, hit: bool) -> list[Decision]:
        # TODO(P2)
        return []

    def on_state(self, state: BrainState) -> list[Decision]:
        # TODO(P2)
        return []
