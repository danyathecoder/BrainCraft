"""World effects: every change is a plain Java Edition command sent via rcon.cmd.

Command syntax is for Java 1.20.5+ / 26.x (namespaced ids, SNBT/JSON text
components). The command builders are pure functions so they can be
checked without a server; `Effects` sends them and maps director
decisions to effects.
"""

from __future__ import annotations

import json
import logging
import re

from mc import config
from mc.world import clear_tower_cmd, lock_movement_cmds, player_anchor_cmd

log = logging.getLogger("braincraft.effects")

ERROR_MARKERS = ("Incorrect argument for command", "Unknown or incomplete command", "<--[HERE]")
_warned: set[str] = set()


def _shape(command: str) -> str:
    """Command with numbers blanked, so a bad summon warns once, not per target."""
    return re.sub(r"-?\d+(\.\d+)?", "#", command)


def send(rcon, command: str) -> str:
    """rcon.cmd(command), never raising for game-level replies.

    Syntax errors from the server are warned about once per distinct
    command shape. Connection errors still raise (the loop handles them).
    """
    reply = rcon.cmd(command) or ""
    check_reply(command, reply)
    return reply


def check_reply(command: str, reply: str) -> None:
    if reply and any(m in reply for m in ERROR_MARKERS):
        key = _shape(command)
        if key not in _warned:
            _warned.add(key)
            log.warning("Minecraft rejected command %r: %s", command, reply)
            print(f"WARNING: Minecraft rejected command {command!r}: {reply}")


def text(text: str, color: str | None = None, max_len: int = 90) -> str:
    """Text component, JSON (also valid SNBT in 1.21.5+)."""
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    comp = {"text": text}
    if color:
        comp["color"] = color
    return json.dumps(comp, ensure_ascii=False)


# --- command builders ------------------------------------------------------

P = config.PLAYER

CUE_SOUND = "minecraft:block.note_block.bell"
HIT_SOUND = "minecraft:entity.experience_orb.pickup"
MISS_SOUND = "minecraft:block.note_block.bass"
REST_SOUND = "minecraft:block.amethyst_block.chime"
BACK_SOUND = "minecraft:block.bell.use"
CAVE_SOUND = "minecraft:ambient.cave"
FALSE_TAG_SOUND = "minecraft:entity.villager.no"
WAVE_SOUND = "minecraft:event.raid.horn"
LEVEL_UP_SOUND = "minecraft:entity.player.levelup"

_MODE_SUBTITLE = {
    "rehab": "Rehab: scan left and right",
    "fun": "Fun: hold your ground",
}
def weather_cmd(mode: str) -> str:
    """Fun: a thunderstorm at night. Rehab: a clear night (calmer, for patients)."""
    return "weather thunder" if mode == "fun" else "weather clear"


# Scripted lightning in fun mode: this far from the player (targets stand at
# TARGET_DISTANCE = 24; a bolt hurts within ~3 blocks), within this angle of
# where they look, every LIGHTNING_EVERY_S seconds (random in the range).
LIGHTNING_DISTANCE = 42
LIGHTNING_MAX_ANGLE = 55.0
LIGHTNING_EVERY_S = (15.0, 40.0)


def lightning_cmd(angle_deg: float, facing_yaw: float = 180.0,
                  distance: float = LIGHTNING_DISTANCE) -> str:
    """A lightning bolt `angle_deg` left (<0) or right (>0) of where the player looks."""
    from game.geometry import target_position

    side = "L" if angle_deg < 0 else "R"
    x, y, z = target_position(side, abs(angle_deg), distance=distance, facing_yaw=facing_yaw)
    return f"summon minecraft:lightning_bolt {x:.1f} {y:.1f} {z:.1f}"


def bow_cmds(mode: str, player: str = P, tier: int = 0) -> list[str]:
    """Replace the player's bow: the XP tier's enchanted bow in fun mode
    (game/bow.py), a plain one in rehab (comparable metrics)."""
    from game import bow

    item = bow.bow_item(tier) if mode == "fun" else "minecraft:bow"
    return [f"clear {player} minecraft:bow", f"give {player} {item} 1"]


_MODE_SWITCH = {
    "rehab": "Rehab mode",
    "fun": "Fun mode",
}


def actionbar_cmd(msg: str, color: str | None = None, player: str = P) -> str:
    return f"title {player} actionbar {text(msg, color)}"


def title_cmd(msg: str, color: str | None = None, player: str = P) -> str:
    return f"title {player} title {text(msg, color, max_len=40)}"


def subtitle_cmd(msg: str, color: str | None = None, player: str = P) -> str:
    return f"title {player} subtitle {text(msg, color, max_len=60)}"


def sound_at_player_cmd(sound: str, volume: float = 1.0, pitch: float = 1.0,
                        source: str = "master", player: str = P) -> str:
    return f"execute as {player} at @s run playsound {sound} {source} @s ~ ~ ~ {volume:g} {pitch:g}"


def sound_at_cmd(sound: str, x: float, y: float, z: float, volume: float = 3.0,
                 pitch: float = 1.0, player: str = P) -> str:
    """Positional sound. Volume > 1 extends the audible range (16 blocks x volume),
    targets are ~24 blocks away, so the default is 3 (48 blocks)."""
    return f"playsound {sound} master {player} {x:.1f} {y + 1:.1f} {z:.1f} {volume:g} {pitch:g}"


def cue_sound_cmd(x: float, y: float, z: float, player: str = P) -> str:
    return sound_at_cmd(CUE_SOUND, x, y, z, volume=3, pitch=1.2, player=player)


def smoke_cmd(x: float, y: float, z: float) -> str:
    return f"particle minecraft:large_smoke {x:.1f} {y + 1:.1f} {z:.1f} 0.4 0.6 0.4 0.02 40 force"


MOB_ENTITY = getattr(config, "TARGET_ENTITY", "minecraft:skeleton")


def summon_mob_cmd(target_id: str, x: float, y: float, z: float, side: str,
                   entity: str = MOB_ENTITY, enemy: str | None = None) -> str:
    """Skeleton with AI: walks toward the player and shoots. One arrow kills it.

    Same bow and helmet as the still targets, so it fires from the spawn
    distance and does not burn at dawn. `enemy` (mc.enemies key, fun
    mode) summons a tougher ranged mob instead.
    """
    if enemy and enemy != "skeleton":
        from mc import enemies

        return enemies.summon_cmd(enemies.get(enemy), x, y, z,
                                  ["target", target_id, side, "mob"], hold=False)
    nbt = (f'{{PersistenceRequired:1b,IsBaby:0b,CanPickUpLoot:0b,Health:1f,'
           f'{config.TARGET_GEAR_NBT},'
           f'{config.TARGET_ATTRIBUTES_NBT},'
           f'Tags:["target","{target_id}","{side}","mob"]}}')
    return f"summon {entity} {x:.1f} {y:.1f} {z:.1f} {nbt}"


def summon_sheep_cmd(target_id: str, x: float, y: float, z: float, side: str) -> str:
    """Still sheep distractor: shooting it is a false tag."""
    nbt = (f'{{NoAI:1b,PersistenceRequired:1b,Health:1f,'
           f'Tags:["distractor","{target_id}","{side}"]}}')
    return f"summon minecraft:sheep {x:.1f} {y:.1f} {z:.1f} {nbt}"


# --- effects ----------------------------------------------------------------


class Effects:
    """Applies world effects through rcon.cmd. Never edits mc/rcon.py."""

    def __init__(self, rcon, player: str = P):
        self.rcon = rcon
        self.player = player
        self.xp = 0  # session score, mirrors the player's XP bar
        self.bow_tier = 0  # fun-mode bow tier (game/bow.py), only ever goes up

    def cmd(self, command: str) -> str:
        return send(self.rcon, command)

    # session -------------------------------------------------------------

    def session_start(self, mode: str) -> None:
        p = self.player
        self.clear_entities()
        self.cmd(clear_tower_cmd())
        self.cmd("time set midnight")
        self.cmd(weather_cmd(mode))
        self.cmd(f"effect clear {p}")
        self.cmd(player_anchor_cmd(p))
        for c in lock_movement_cmds(p):
            self.cmd(c)
        self.cmd(f"give {p} minecraft:arrow 64")
        self.cmd(f"xp set {p} 0 levels")  # the XP bar is this session's score
        self.cmd(f"xp set {p} 0 points")
        self.xp, self.bow_tier = 0, 0
        self.give_bow(mode)
        self.cmd(subtitle_cmd(_MODE_SUBTITLE.get(mode, mode), player=p))
        self.cmd(title_cmd("BrainCraft", "gold", player=p))

    def give_bow(self, mode: str) -> None:
        if mode == "fun":  # catch up on XP earned while in rehab
            from game import bow

            self.bow_tier = max(self.bow_tier, bow.tier_for(self.xp))
        for c in bow_cmds(mode, self.player, self.bow_tier):
            self.cmd(c)

    def bow_upgrade(self, tier: int, label: str) -> None:
        self.bow_tier = tier
        for c in bow_cmds("fun", self.player, tier):
            self.cmd(c)
        self.cmd(sound_at_player_cmd(LEVEL_UP_SOUND, 1, 1, player=self.player))
        self.cmd(subtitle_cmd(label, "gold", player=self.player))
        self.cmd(title_cmd("Bow upgraded!", "gold", player=self.player))

    def enemy_unlock(self, label: str, threat: str) -> None:
        p = self.player
        self.cmd(sound_at_player_cmd(WAVE_SOUND, 1, 1.4, player=p))
        self.cmd(subtitle_cmd(f"{label}: {threat}", "red", player=p))
        self.cmd(title_cmd("New enemy!", "red", player=p))

    def lightning(self, angle_deg: float, facing_yaw: float | None) -> None:
        """Fun-mode atmosphere: a bolt far behind the targets, in view."""
        self.cmd(lightning_cmd(angle_deg, 180.0 if facing_yaw is None else facing_yaw))

    def keep_player(self) -> None:
        """Re-apply the movement lock; after a death and respawn Minecraft
        resets player attributes. Idempotent, called every few s. No
        Saturation: it kept the food bar full and healed the player
        almost instantly, so nobody could die."""
        for c in lock_movement_cmds(self.player):
            self.cmd(c)

    def session_end(self) -> None:
        self.clear_entities()
        self.cmd("time set midnight")
        self.cmd(f"effect clear {self.player} minecraft:darkness")
        self.cmd(title_cmd("Session over", "gold", player=self.player))

    def clear_entities(self) -> None:
        self.cmd("kill @e[tag=target]")
        self.cmd("kill @e[tag=distractor]")

    def mode_switch(self, mode: str) -> None:
        if mode != "fun":
            self.cmd(f"effect clear {self.player} minecraft:darkness")
            self.cmd("time set midnight")
        self.cmd(weather_cmd(mode))
        self.give_bow(mode)
        self.cmd(subtitle_cmd(_MODE_SWITCH.get(mode, mode), player=self.player))
        self.cmd(title_cmd("Mode switch", "gold", player=self.player))

    # rest phase (rehab) --------------------------------------------------

    def rest_start(self, seconds: float) -> None:
        p = self.player
        self.cmd("time set 23000")  # dawn
        self.cmd("weather clear")
        self.cmd(sound_at_player_cmd(REST_SOUND, 1, 0.8, player=p))
        self.cmd(subtitle_cmd(f"Rest for {seconds:g} s, look around calmly", "aqua", player=p))
        self.cmd(title_cmd("Take a breath", "aqua", player=p))

    def rest_end(self) -> None:
        p = self.player
        self.cmd("time set midnight")
        self.cmd(sound_at_player_cmd(BACK_SOUND, 1, 1, player=p))
        self.cmd(subtitle_cmd("", player=p))
        self.cmd(title_cmd("Back to the watch", "gold", player=p))

    # trial feedback ------------------------------------------------------

    def hit(self, target=None) -> None:
        """Kill: sound + XP as the visible score (more for tougher enemies)."""
        from mc import enemies

        xp = enemies.kill_xp(getattr(target, "enemy", None), getattr(target, "kind", "target"))
        self.xp += xp
        self.cmd(sound_at_player_cmd(HIT_SOUND, 1, 1.2, player=self.player))
        self.cmd(f"xp add {self.player} {xp} points")
        self.cmd(actionbar_cmd(f"+{xp} XP", "green", player=self.player))

    def miss(self, target) -> None:
        x, y, z = target.pos
        self.cmd(smoke_cmd(x, y, z))
        self.cmd(sound_at_cmd(MISS_SOUND, x, y, z, volume=2, pitch=0.6, player=self.player))

    def false_tag(self, target=None) -> None:
        from mc import enemies

        self.xp = max(0, self.xp - enemies.SHEEP_XP_PENALTY)
        self.cmd(sound_at_player_cmd(FALSE_TAG_SOUND, 1, 1, player=self.player))
        self.cmd(f"xp add {self.player} -{enemies.SHEEP_XP_PENALTY} points")
        self.cmd(actionbar_cmd(f"-{enemies.SHEEP_XP_PENALTY} XP  sheep are friends!", "red",
                               player=self.player))

    def echo(self, decision) -> None:
        """Mirror every director decision in-game."""
        self.cmd(actionbar_cmd(decision.reason, "yellow", player=self.player))

    # fun mode ------------------------------------------------------------

    def darkness(self, seconds: int) -> None:
        self.cmd(f"effect give {self.player} minecraft:darkness {int(seconds)} 0 true")

    def clear_darkness(self) -> None:
        self.cmd(f"effect clear {self.player} minecraft:darkness")

    def cave_sound(self) -> None:
        self.cmd(sound_at_player_cmd(CAVE_SOUND, 1, 1, source="ambient", player=self.player))

    def flare(self, seconds: float) -> None:
        self.cmd(f"effect give @e[tag=target] minecraft:glowing {int(seconds)} 0 true")

    def breather_start(self, seconds: float) -> None:
        self.clear_darkness()
        self.cmd(sound_at_player_cmd(REST_SOUND, 1, 0.8, player=self.player))
        self.cmd(subtitle_cmd(f"{seconds:g} s breather, a wave is coming", "aqua", player=self.player))
        self.cmd(title_cmd("Breather", "aqua", player=self.player))

    def wave(self, n: int) -> None:
        self.cmd(sound_at_player_cmd(WAVE_SOUND, 1, 1, player=self.player))
        self.cmd(subtitle_cmd(f"{n} skeletons incoming", "red", player=self.player))
        self.cmd(title_cmd("Wave!", "red", player=self.player))

    # director decisions --------------------------------------------------

    def apply(self, decision) -> None:
        """World effect for a decision (if any), then the actionbar echo."""
        a, prm = decision.action, decision.params
        if a == "rest_start":
            self.rest_start(prm.get("duration_s", 45))
        elif a == "rest_end":
            self.rest_end()
        elif a == "darkness":
            self.darkness(prm.get("seconds", 30))
        elif a == "clear_darkness":
            self.clear_darkness()
        elif a == "cave_sound":
            self.cave_sound()
        elif a == "flare":
            self.flare(prm.get("seconds", 5))
        elif a == "breather_start":
            self.breather_start(prm.get("duration_s", 20))
        elif a == "wave":
            self.wave(prm.get("n", 5))
        elif a == "mode":
            self.mode_switch(prm.get("mode", "rehab"))
        elif a == "bow_upgrade":
            self.bow_upgrade(prm.get("tier", 0), prm.get("label", ""))
        elif a == "enemy_unlock":
            self.enemy_unlock(prm.get("label", "New enemy"), prm.get("threat", ""))
        self.echo(decision)
