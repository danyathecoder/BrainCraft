"""What a director returns: actions for the game, each with a reason."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Decision:
    action: str  # e.g. "spawn_interval", "rest_phase", "cue_left"
    params: dict = field(default_factory=dict)
    reason: str = ""  # shown on the HUD and logged; never empty
