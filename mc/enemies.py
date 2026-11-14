"""Enemy roster for fun mode (and the default rehab skeleton).

Ranged mobs only, each tier a step harder; every one shoots, so the
player can die. Pure data + /summon NBT builders (no RCON here).

    tier 0  skeleton  arrows                      1 HP   bow + helmet
    tier 1  stray     arrows of Slowness          6 HP   bow + helmet
    tier 2  bogged    arrows of Poison           10 HP   bow + helmet
    tier 3  pillager  crossbow bolts             16 HP   crossbow
    tier 4  blaze     fireballs, sets you alight 20 HP   (no gear, fire immune)

Undead burn at dawn (rest phase), which would log as a hit, so they get
an unbreakable leather helmet. Health is set with both the attribute
(so it is not clamped to the default) and the Health tag.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc import config

_HELMET = 'head:{id:"minecraft:leather_helmet",count:1,components:{"minecraft:unbreakable":{}}}'


@dataclass(frozen=True)
class Enemy:
    key: str
    entity: str
    label: str
    threat: str  # shown when the tier unlocks
    health: float
    weapon: str | None  # main-hand item id, or None
    burns: bool  # undead: needs the helmet against daylight
    lifetime_s: float  # fun mode: time before it counts as a miss
    xp: int  # experience points for a kill (the XP bar is the score)

    @property
    def gear_nbt(self) -> str:
        slots, drops = [], []
        if self.weapon:
            slots.append(f'mainhand:{{id:"{self.weapon}",count:1}}')
            drops.append("mainhand:0.0f")
        if self.burns:
            slots.append(_HELMET)
            drops.append("head:0.0f")
        if not slots:
            return ""
        return f"equipment:{{{','.join(slots)}}},drop_chances:{{{','.join(drops)}}}"


ROSTER: tuple[Enemy, ...] = (
    Enemy("skeleton", "minecraft:skeleton", "Skeletons", "arrows", 1, "minecraft:bow", True, 6.0, 5),
    Enemy("stray", "minecraft:stray", "Strays", "their arrows slow you", 6, "minecraft:bow", True,
          8.0, 10),
    Enemy("bogged", "minecraft:bogged", "Bogged", "their arrows poison you", 10, "minecraft:bow",
          True, 9.0, 15),
    Enemy("pillager", "minecraft:pillager", "Pillagers", "crossbow bolts hit harder", 16,
          "minecraft:crossbow", False, 10.0, 25),
    Enemy("blaze", "minecraft:blaze", "Blazes", "fireballs set you on fire", 20, None, False, 12.0, 40),
)
BY_KEY = {e.key: e for e in ROSTER}
DEFAULT = "skeleton"
MOB_XP_BONUS = 1.5  # a walking enemy is harder to hit
SHEEP_XP_PENALTY = 10  # shooting a sheep distractor


def kill_xp(enemy_key: str | None, kind: str = "target") -> int:
    """XP for a kill: per enemy, x1.5 for one that walks in."""
    xp = get(enemy_key).xp
    return int(round(xp * MOB_XP_BONUS)) if kind == "mob" else xp


def get(key: str | None) -> Enemy:
    return BY_KEY.get(key or DEFAULT, BY_KEY[DEFAULT])


def _attributes(enemy: Enemy, hold: bool) -> str:
    attrs = [f'{{id:"minecraft:follow_range",base:{config.TARGET_FOLLOW_RANGE}.0d}}',
             f'{{id:"minecraft:max_health",base:{enemy.health:g}.0d}}']
    if hold:
        attrs.append('{id:"minecraft:movement_speed",base:0.0d}')
    return f"attributes:[{','.join(attrs)}]"


def summon_nbt(enemy: Enemy, tags: list[str], hold: bool, glowing: bool = False) -> str:
    """NBT for /summon: `hold` keeps it on its mark (still target), else it walks in."""
    parts = ["PersistenceRequired:1b", "CanPickUpLoot:0b", f"Health:{enemy.health:g}f"]
    if enemy.entity != "minecraft:blaze":
        parts.append("IsBaby:0b")
    if enemy.gear_nbt:
        parts.append(enemy.gear_nbt)
    parts.append(_attributes(enemy, hold))
    if glowing:
        parts.append("Glowing:1b")
    parts.append("Tags:[" + ",".join(f'"{t}"' for t in tags) + "]")
    return "{" + ",".join(parts) + "}"


def summon_cmd(enemy: Enemy, x: float, y: float, z: float, tags: list[str], hold: bool,
               glowing: bool = False) -> str:
    return f"summon {enemy.entity} {x:.1f} {y:.1f} {z:.1f} {summon_nbt(enemy, tags, hold, glowing)}"
