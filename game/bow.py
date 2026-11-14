"""Fun-mode bow that upgrades with the XP score.

    XP    bow
      0   Power I, Infinity, Unbreaking III
     50   Power II, Punch I
    150   + Flame (arrows set enemies on fire)
    300   Power III, Punch II
    500   Power V

Infinity on every tier: arrows never run out. Only fun mode uses these;
rehab keeps a plain bow so its metrics stay comparable. XP never takes a
tier away (a sheep penalty can lower XP, not the bow).
"""

from __future__ import annotations

from dataclasses import dataclass

_BASE = {"minecraft:infinity": 1, "minecraft:unbreaking": 3}


@dataclass(frozen=True)
class BowTier:
    xp: int  # XP needed
    label: str
    enchantments: dict


TIERS: tuple[BowTier, ...] = (
    BowTier(0, "Power I, Infinity", {"minecraft:power": 1, **_BASE}),
    BowTier(50, "Power II, Punch", {"minecraft:power": 2, "minecraft:punch": 1, **_BASE}),
    BowTier(150, "Flame: arrows set enemies on fire",
            {"minecraft:power": 2, "minecraft:punch": 1, "minecraft:flame": 1, **_BASE}),
    BowTier(300, "Power III, Punch II",
            {"minecraft:power": 3, "minecraft:punch": 2, "minecraft:flame": 1, **_BASE}),
    BowTier(500, "Power V",
            {"minecraft:power": 5, "minecraft:punch": 2, "minecraft:flame": 1, **_BASE}),
)


def tier_for(xp: int) -> int:
    """Highest tier this XP has reached."""
    return max(i for i, t in enumerate(TIERS) if xp >= t.xp)


def bow_item(tier: int) -> str:
    """Item argument for /give (26.3 component syntax; the old {levels:{...}} form is rejected)."""
    ench = ",".join(f'"{k}":{v}' for k, v in TIERS[tier].enchantments.items())
    return f"minecraft:bow[minecraft:enchantments={{{ench}}}]"
