# Game modes: how they work

How BrainCraft decides **what appears, where, when, and why**. There are
two modes: **rehab** (default) and **fun**. You can switch between them
live with the HUD buttons. Parameters live in `director/rehab.py`
(`RehabParams`), `director/fun.py` (`FunParams`, `LEVELS`) and
`mc/config.py`.

## Contents
- [Shared machinery](#shared-machinery)
- [Brain signals](#brain-signals)
- [Rehab mode](#rehab-mode)
- [Fun mode](#fun-mode)
- [Sounds](#sounds)
- [Decision catalogue](#decision-catalogue)
- [What gets measured](#what-gets-measured)
- [Tuning cheat sheet](#tuning-cheat-sheet)

---

## Shared machinery

```mermaid
flowchart LR
  EEG["EEG pipeline<br/>1 value/s"] -->|"/state"| GL
  MC["Minecraft<br/>RCON replies"] --> TR["TrialRunner<br/>hit / miss / found"]
  TR --> GL["game_loop.py<br/>5 Hz"]
  GL --> D["Director<br/>rehab or fun"]
  D -->|"SpawnSpec"| TR
  D -->|"Decision + reason"| FX["Effects<br/>RCON commands"]
  FX --> MC
  D --> LOG["CSV logs, HUD, report"]
```

**The loop** (`game_loop.py`) runs at **5 Hz**. Once per second it:
1. reads the HUD mode (`/mode`);
2. reads the brain state (`/state`) and passes it to the director (`on_state`);
3. logs the state and the player's yaw;
4. re-applies the movement lock every 5 s (`KEEP_PLAYER_S`), because a death resets it.

At every 5 Hz step it polls the active targets and asks the director
what to spawn (`due_spawns`).

**The player** stands on the grass at `STAND_X, STAND_Z`, facing north,
in adventure mode with a bow and 192 arrows. There's no Saturation
effect, because it healed the player too fast to ever die:
- walking, jumping and knockback are off; only the view turns;
- difficulty is **normal**, so skeletons shoot and the player **can die**;
- `immediate_respawn` and `keep_inventory` are on, and the spawn point is the standing spot.

**Where targets appear** (`game/geometry.py`):
- on an arc **24 blocks** from the player (`TARGET_DISTANCE`);
- at an **eccentricity** of 15, 25, 35 or 45° (`ECCENTRICITIES`) to the left or right of **the direction the player is looking at spawn time**;
- all within the view at FOV 70 on a 16:9 screen (±51°).

**Target kinds:**

| Kind | Entity | Behaviour | Used by |
|---|---|---|---|
| `target` | skeleton, bow, unbreakable helmet (doesn't burn at dawn), movement speed 0 | stands on its mark and shoots; one arrow kills it (Health 1) | rehab, fun |
| `mob` | skeleton with AI | walks toward the player and shoots | fun (levels 2+, waves) |
| `sheep` | sheep | distractor: shooting it is a false tag (−1) | fun (levels 2+) |

**Trial outcomes** (`game/trials.py`, polled at 5 Hz):
- **found:** the first time the player's yaw is within **±10°** of the target's bearing. Its `rt_ms` is the search time.
- **hit:** the target disappeared before its lifetime ended (shot). Its `rt_ms` is the time to hit, accurate to half a poll.
- **miss:** the lifetime ran out, and the target is removed with smoke and a low sound.
- **false tag:** a sheep was shot.
- **XP is the visible score** (the player's XP bar and level, reset at
  session start). Each kill gives XP: skeleton 5, stray 10, bogged 15,
  pillager 25, blaze 40, ×1.5 for an enemy that walks in. A sheep costs
  10 XP. The action bar shows `+25 XP`. Values are in `mc/enemies.py`
  (`xp`, `MOB_XP_BONUS`, `SHEEP_XP_PENALTY`).
- **cued** targets glow and play a sound at their position, repeated once after 2 s if not yet found.

Every **decision** carries a human-readable reason. It is shown in the
in-game action bar and the HUD decision log, and saved in the session
CSV.

---

## Brain signals

From `eeg/pipeline.py`. Once per second the pipeline takes the last 4 s
in 1 s segments, leaves out bad seconds, z-scores the values against
the player's calibration baseline and smooths them (EMA ~15 s).

| Signal | What it is | Used for |
|---|---|---|
| `engagement_z` | β/(α+θ) at Fz, Cz (Pope 1995), z vs baseline | **falling vs its own 2-min peak → harder** (fun: levels, fast; rehab: enemies, slow); rehab: strongly low → rest, challenge band |
| `fatigue_z` | (θ+α)/β at Fz, z vs baseline | rehab: rest phase; fun: overload breather |
| `alpha_low_z` | 8–10 Hz power at Pz, Oz | logged and shown, no rule uses it yet |
| `ali` | (α PO7 − α PO8)/(α PO7 + α PO8) **minus the player's calibration mean**; rising = attention moving left | rehab: **rising ALI heals the player** (+1 heart) |
| `quality` | `ok` when ≥ 2 of the 4 seconds are clean | every EEG rule counts **only good-quality seconds** |
| `artifact`, `jaw_clench` | newest second: blink / jaw / motion / contact | fun: jaw clench = flare |

`HoldTimer` (`director/base.py`) makes every rule require its
condition for N good seconds. A bad second neither adds time nor resets
the timer, so noise never triggers anything.

---

## Rehab mode

Visual scanning training inspired by therapy for post-stroke spatial
neglect (left side ignored). **Each signal has one job:**

| Question | Decided by |
|---|---|
| How does the session start? | **warm-up**: first 60 s on the right only, then **10% left / 90% right** |
| Which side? | **performance**: every hit moves spawns +0.05 to the left, every miss −0.05 |
| How far left? | **performance**: the left-angle staircase, plus cues |
| How tough are the enemies? | **time (very slow) + falling engagement** |
| Reward | **the brain (ALI)**: attention moving left heals the player |
| When to rest? | **the brain (fatigue / strongly low engagement)**: dawn rest phase |

```mermaid
stateDiagram-v2
  [*] --> WarmUp: session start
  WarmUp --> Training: 60 s over (10% left)
  state Training {
    [*] --> Scan
    Scan --> Scan: hit +0.05 left / miss -0.05
    Scan --> Scan: ALI rises >= 0.02 in 1 s: +1 heart
  }
  Training --> Rest: fatigue_z > 1.5 for 20 s, or engagement_z < -1.5 for 30 s
  Rest --> Training: 45 s over (slower pace)
```

### Spawning
- **One target at a time** (`max_active` = 1). The first appears 2 s after the start (`lead_in_s`).
- **Lifetime: 60 s** (`target_lifetime_s`). The enemy shoots the whole time, so ignoring it hurts, and the player can die.
- **Next target:** 1 s after a **hit** (`hit_gap_s`), or `spawn_interval_s` (4 s) after a **miss**.
- **Warm-up, the first 60 s** (`warmup_s`): right side only, at a random angle.
- **After the warm-up:** left with probability **`p_left`**, which starts at **0.1**, otherwise right.
  - **Left** targets use the staircase angle, and are cued while cues are on.
  - **Right** targets stay at **35°** (`right_ecc_deg`), a stable control condition so left/right differences reflect the left side.
- **No spawns during a rest.** Targets still up when a rest starts are removed without counting.

### Side: hits move the spawns left (performance)
- **Every hit** (either side) raises `p_left` by **0.05**, and **every miss** lowers it by 0.05, within **0.10–0.70** (`p_left_hit_step`, `p_left_miss_step`, `p_left_min`, `p_left_max`).
- **Not during the warm-up.**
- A **`left_chance`** decision is logged each time `p_left` has moved a full 10 points since the last one ("hits → left targets 10% → 20%"). Smaller wobbles aren't logged.
- In a simulated 10-min session it climbs from 10% to 70% in about 3 minutes of good play.

### Distance: the staircase (performance)
It runs over the last **5 left** targets (`staircase_window`); right targets don't count:
- **≥ 4 hits** → left angle one step out (15 → 25 → 35 → 45°), cues off.
- **≤ 2 hits** → one step in, and **cues on** for the next 5 left targets (glow + sound).
- 3 hits → no change.
- The window is cleared after every step. The staircase no longer changes the side.

### Reward: rising ALI heals (brain)
- **ALI is relative to the player's calibration,** smoothed over ~15 s, and updated every second.
- **Trigger:** a good-quality second where ALI rose by **≥ 0.02** compared with the previous second (`ali_reward_rise`). A rising ALI means attention is moving **left**, which is what the training wants.
- **Effect:** **+2 HP (1 heart)**, via Regeneration II for 3 s, a chime, and "+1 heart" in the action bar. It also shows as an `ali_reward` decision with its reason.
- **At most once every 5 s** (`ali_reward_cooldown_s`). It needs consecutive good seconds, and doesn't fire during a rest. It does work during the warm-up.
- **Tuning note:** because ALI is smoothed, a 0.02 rise in one second needs a clear real shift. If the reward almost never fires with real players, lower `ali_reward_rise`; if it fires constantly, raise it. With realistic simulated ALI it fires about 4 times a minute.

### Enemies: very slowly harder, faster when engagement falls (brain)
They use the same enemies as fun mode (`mc/enemies.py`: skeleton → stray → bogged → pillager → blaze, each with more HP), but **much slower**.

- **Time:** +1 tier every **5 minutes** (`enemy_every_s`; fun: 90 s). A standard 10-minute session ends when bogged arrive.
- **Falling engagement:** **+1 tier** (at most, `max_enemy_boost`) when `engagement_z` stays **0.5 below its own 2-minute peak for 20 good seconds**. It uses the same detector as fun mode (`EngagementDrop`), with slower settings.
  - The step is undone after engagement is back near its earlier level for **60 s**.
  - Steps are at least **60 s** apart.
- **Relation to the rest rule:** a *strong, absolute* low (`engagement_z < −1.5` for 30 s) still means a rest. The harder enemy is for a *moderate, relative* drop.
- **Pick:** each spawn is the newest allowed enemy half the time, otherwise an older one. This applies to left and right targets alike, so the right-side control stays comparable.
- **Lifetime** stays 60 s for every enemy; tougher ones need more arrows. Each trial logs its enemy (`target:stray`), so hit times can be compared per enemy.
- **New enemies** show "New enemy!" (`enemy_unlock`). Boost steps log `enemy_boost_up` / `enemy_boost_down`.

### Pace: the challenge rule (performance + brain)
Hit rate **> 85% over the last 10 trials** while `engagement_z` is in
**[−0.5, 1.0]** → spawn interval −0.5 s (floor 2 s). The trial window is
then cleared.

### Rest (brain)
- **Trigger:** `fatigue_z > 1.5` for **20 good seconds**, or `engagement_z < −1.5` for **30 good seconds**.
- **The 45 s rest phase** (`rest_s`): the world turns to **dawn** with a calm sound and the titles "Take a breath" / "look around calmly". There are no spawns, and active targets are removed.
- **After the rest:** midnight again, "Back to the watch", the spawn interval +0.5 s (max 6 s), and **no new rest for 120 s** (`rest_refractory_s`).

---

## Fun mode

Same engine, the goal is a game that keeps getting harder, and harder
still when the player's engagement **falls**.

**Difficulty level = time baseline + engagement boost** (0–4):

```mermaid
flowchart LR
  T["Time<br/>+1 level every 90 s"] --> L["Level 0-4<br/>pace, count, angles,<br/>walking enemies, sheep,<br/>darkness, enemy type"]
  E["Engagement falling<br/>vs own 2-min peak"] -->|"boost +1 (max +2)"| L
  R["Engagement recovered<br/>for 30 s"] -->|"boost -1"| L
  O["Overload<br/>fatigue or 3 misses"] -->|"breather, boost -1"| L
```

- **Time baseline:** **+1 level every 90 s** (`level_every_s`): level 0 at
  0–1.5 min, 1 from 1.5 min, 2 from 3 min, 3 from 4.5 min, 4 from 6 min.
  The game always progresses, even for a player whose engagement stays
  high.
  - **Why not an absolute threshold:** in real sessions `engagement_z`
    stayed high all the time (baseline mismatch, smoothing). The old rule
    ("bored below −0.5 → harder, above +1.0 → easier") never escalated, and
    players only ever saw skeletons at the slowest pace.
- **Engagement boost** (+1 per step, at most **+2** above the time
  schedule, `max_boost`):
  - **Falling:** `engagement_z` at least **0.5 below its own peak of the
    last 2 min** (`drop_z`, `peak_window_s`) for **10 good seconds**
    (`drop_hold_s`). The old absolute rule still counts too: below −0.5
    for 15 s.
  - **After a step:** at least **30 s** before the next one
    (`boost_cooldown_s`), and the peak restarts from the current value, so
    a further step needs a fresh drop.
  - **Recovered:** engagement back to within 0.2 of where it was before it
    fell (at least above −0.5) for **30 good seconds** → boost −1.
    Difficulty never drops below the time schedule.
  - **At max level** a further drop adds cave sounds and darkness.
  - **High engagement no longer lowers the level** (the old "hype" rule
    is gone).
- Bad-quality seconds count for nothing, as everywhere.

| Level | Reached at (no boost) | Spawn every | Max at once | Eccentricity up to | Walking enemies | Sheep | Darkness | Enemy unlocked |
|---|---|---|---|---|---|---|---|---|
| 0 calm watch | start | 3.0 s | 1 | 25° | — | — | — | Skeleton |
| 1 | 1.5 min | 2.5 s | 2 | 35° | — | — | — | Stray |
| 2 | 3 min | 2.5 s | 2 | 45° | 30 % | 20 % | — | Bogged |
| 3 | 4.5 min | 2.0 s | 3 | 45° | 35 % | 20 % | yes | Pillager |
| 4 maximum | 6 min | 1.5 s | 3 | 45° | 40 % | 25 % | yes | Blaze |

- **Each level-up** plays a cave sound, and on levels 3–4 adds darkness for
  30 s. A level-down clears the darkness.
- **Side:** left/right 50/50.
- **Lifetimes:** still target **6 s** (`TARGET_LIFETIME_S`), walking mob 14 s, sheep 8 s. Tougher enemies get their own, longer lifetime (table below).

**Enemies get tougher** (`mc/enemies.py`). All of them shoot and can kill
the player:

| Tier | Enemy | Threat | HP | Gear | Lifetime | Appears from |
|---|---|---|---|---|---|---|
| 0 | Skeleton | arrows | 1 | bow + helmet | 6 s | level 0 |
| 1 | Stray | arrows of Slowness | 6 | bow + helmet | 8 s | level 1 |
| 2 | Bogged | arrows of Poison | 10 | bow + helmet | 9 s | level 2 |
| 3 | Pillager | crossbow bolts | 16 | crossbow | 10 s | level 3 |
| 4 | Blaze | fireballs, sets you on fire | 20 | — (fire immune) | 12 s | level 4 |

- **The toughest enemy is the current level.** Every game starts with
  skeletons, and a new type comes every 1.5 min. Falling engagement brings
  the next types **early** (up to two levels ahead).
- **Each spawn is the newest allowed tier half the time,** otherwise a
  lower one, for variety. This applies to still targets, walking mobs and
  waves.
- **When a tier is first reached,** an `enemy_unlock` decision fires: the
  title "New enemy!" plus its threat. Dropping a level lowers the tier
  without re-announcing.
- **Undead** (skeleton, stray, bogged) wear an unbreakable helmet so they
  don't burn at dawn. Health is set as the `max_health` attribute plus
  `Health`.
- **Logging:** spawn, hit and miss events log the enemy: `target:stray`,
  `mob:blaze`. Plain skeletons are logged as before.

**The player's bow in fun mode upgrades with the XP score**
(`game/bow.py`). The arrows never run out (Infinity), and every bow glints:

| XP | Bow |
|---|---|
| 0 | Power I, Infinity, Unbreaking III |
| 50 | Power II, Punch |
| 150 | + Flame (arrows set enemies on fire) |
| 300 | Power III, Punch II |
| 500 | Power V |

- **Each upgrade:** a `bow_upgrade` decision ("170 XP ≥ 150 → bow
  upgraded: Flame…"), the title "Bow upgraded!", a level-up sound, and the
  new bow in hand.
- **The bow never downgrades,** even when a sheep costs XP. XP earned in
  rehab counts when switching to fun.
- **Rehab keeps a plain bow,** so its metrics stay comparable.

**Overload → breather → wave:**
- **Trigger:** `fatigue_z > 1.5` for 5 good seconds, or **3 misses in a row**. At most once every 45 s after the previous breather.
- **The 20 s breather:** boost −1 (never below the time schedule), no
  spawns, darkness cleared, a calm sound.
- **Then a wave:** **5 walking enemies** of the current tiers, alternating
  left and right.

**A storm instead of a quiet night:**
- **Fun mode runs in a thunderstorm** (`weather thunder`): rain and a
  darker sky.
- **Scripted lightning** strikes every **15–40 s**, **42 blocks away**
  (well behind the targets at 24) and within ±55° of where the player
  looks, so it's visible but can't hit a target or the player.
- **Rehab stays a clear night** (calmer for patients); switching modes
  switches the weather.
- **Caveat:** in a thunderstorm Minecraft also strikes naturally at random
  spots. It rarely lands near a target or the player, but if it does, a
  target can die (logged as a hit) or the player takes damage.

**Jaw clench = flare:** a detected jaw clench makes all targets glow for
5 s, with a 30 s cooldown. It's the player's one deliberate brain or
muscle control.

---

## Sounds

Sounds are part of the feedback, and in rehab part of the training. All
of them are sent by the game loop over RCON as `playsound` commands
(`game/effects.py`); Minecraft itself adds the enemies' own sounds and
the storm.

**Two kinds of sound:**
- **Positional (at the target).**
  - **How:** `playsound <sound> master <player> <x> <y+1> <z> <volume> <pitch>`, played at the target's own coordinates.
  - **What the player hears:** Minecraft pans it in stereo and fades it with distance. They **hear which side it comes from** before they see the target, and the stereo balance shifts as they turn toward it.
  - **Range:** volume 3 = audible up to 48 blocks (16 × volume); targets stand 24 blocks away.
  - **Used for:** cues and misses.
- **At the player (non-directional).**
  - **How:** `execute as <player> at @s run playsound … @s ~ ~ ~`, so it plays at the same loudness wherever the player looks.
  - **Used for:** feedback about the game state (hit, rest, level-up, new enemy, …).

**Every sound the game plays:**

| When | Sound (`minecraft:…`) | Where | Volume / pitch | Mode |
|---|---|---|---|---|
| **Cue:** a cued target spawns | `block.note_block.bell` | **at the target** | 3 / 1.2 (high) | rehab |
| **Cue repeat:** cued target not found after 2 s (`CUE_REPEAT_S`) | `block.note_block.bell` | **at the target** | 3 / 1.2 | rehab |
| **Hit** | `entity.experience_orb.pickup` | at the player | 1 / 1.2 | both |
| **Miss** (lifetime over) | `block.note_block.bass` | **at the target** | 2 / 0.6 (low) | both |
| Sheep shot (false tag) | `entity.villager.no` | at the player | 1 / 1 | fun |
| Rest phase starts / breather | `block.amethyst_block.chime` | at the player | 1 / 0.8 (soft, low) | rehab / fun |
| Rest over ("Back to the watch") | `block.bell.use` | at the player | 1 / 1 | rehab |
| **ALI reward** (+1 heart) | `block.amethyst_block.chime` | at the player | 0.8 / 1.6 (bright) | rehab |
| New enemy ("New enemy!") | `event.raid.horn` | at the player | 1 / 1.4 | both |
| Wave after a breather | `event.raid.horn` | at the player | 1 / 1 | fun |
| Bow upgrade | `entity.player.levelup` | at the player | 1 / 1 | fun |
| Level-up (escalation), boost at max level | `ambient.cave` (category `ambient`) | at the player | 1 / 1 | fun |

**How the sounds change during a session:**
- **Cues follow performance (rehab).**
  - **Cues on:** after ≤ 2 of 5 left hits, the next 5 left targets are cued. The bell plays from the target's position, so the player hears "something on the left" and turns toward it. If they still haven't looked at it (within ±10°) after 2 s, the bell rings once more.
  - **Cues off:** after 5 cued targets, or a staircase step up. Auditory spatial cueing toward the neglected side is the point of this; it fades out as the player improves.
- **Misses are located.**
  - **What:** the low bass plays where the missed target stood, together with smoke.
  - **Why:** the player learns where they failed to look.
- **Feedback sounds are paired with meaning.**
  - **Rising / reward:** bright, high sounds (hit 1.2, ALI reward 1.6).
  - **Calm / rest:** soft, low ones (rest 0.8).
  - **Threat:** horns (new enemy, wave).
  - Each also has a text in the action bar or a title, so the sound is never the only signal.
- **Fun mode gets louder as it gets harder.** Every level-up plays a cave sound. A further engagement drop at max level adds another, with darkness on levels 3–4.

**Sounds that come from Minecraft itself:**
- **Enemies make their normal sounds** (no `Silent` tag): rattling bones, the bow being drawn, crossbow loading, blaze breathing and fireballs. These come from the enemy's real position, so in fun mode the player can hear an attacker before seeing it.
- **Storm (fun only):**
  - **Rain and thunder** from `weather thunder`.
  - **Our lightning bolts** (every 15–40 s, 42 blocks away, in view) bring their own thunder clap from that direction.
  - **Rehab** stays a clear, quiet night.

**Client settings that matter:**
- **Stereo headphones** (or speakers left and right of the player). Without stereo, positional cues lose their direction.
- **Music & Sounds:** Master, Hostile Creatures, Weather and Ambient/Environment must not be muted. Turning **Music** off is fine and makes the cues clearer.
- **Pause on Lost Focus off** (F3+P), so sounds keep playing while the HUD window is in front.

**Tuning:** the sound ids, volumes and pitches are constants at the top of
`game/effects.py` (`CUE_SOUND`, `HIT_SOUND`, `MISS_SOUND`, …). The cue
repeat time is `CUE_REPEAT_S` in `game/trials.py`.

---

## Decision catalogue

Every decision is echoed in the action bar and the HUD log, and saved
to the CSV.

| Action | Mode | Trigger | In-game effect |
|---|---|---|---|
| `rehab_start` / `fun_start` | both | session or mode start | title + subtitle |
| `warmup_end` | rehab | 60 s elapsed | both sides from now on, 10% left |
| `left_chance` | rehab | hits / misses moved `p_left` a full 10 points | more or fewer left targets |
| `ali_reward` | rehab | ALI rose ≥ 0.02 in 1 s (max every 5 s) | +1 heart, chime, "+1 heart" |
| `enemy_boost_up` / `enemy_boost_down` | rehab | engagement 0.5 below its 2-min peak for 20 s / recovered for 60 s | next enemy tier early / back to the 5-min schedule |
| `staircase_up` / `staircase_down` | rehab | 5 left trials | left angle, cues |
| `cues_off` | rehab | 5 cued targets shown | — |
| `spawn_interval` | rehab | challenge rule, or after a rest | faster / slower pace |
| `rest_start` / `rest_end` | rehab | fatigue / engagement rule | dawn + titles / midnight |
| `escalate` / `deescalate` | fun | time step (every 90 s), boost change, breather | level change |
| `boost_up` / `boost_down` | fun | engagement falling vs its 2-min peak / recovered for 30 s | +1 / −1 level above the time schedule |
| `cave_sound`, `darkness`, `clear_darkness` | fun | with a level-up (darkness on levels 3–4) / level-down | sound, Darkness effect |
| `breather_start` / `breather_end`, `wave` | fun | overload | pause, then 5 walking enemies |
| `flare` | fun | jaw clench | all targets glow 5 s |
| `enemy_unlock` | both | a new enemy tier is reached (fun: level; rehab: 5-min schedule + boost) | "New enemy!" title + threat |
| `bow_upgrade` | fun | session XP reaches the next bow tier | better bow in hand, "Bow upgraded!" title |
| `mode` | both | HUD button | mode switch title |

---

## What gets measured

Per session, in `sessions/`:
- `<time>-<name>.csv`: events (spawn, found, hit, miss, false_tag, rest, decisions, yaw 1/s);
- `-state.csv`: the brain state every second, including ALI and quality;
- `-report.html`: the report.

The report shows:
- left vs right **hit rate** and **reaction time**;
- **search time** (spawn → found);
- the **asymmetry index** (R−L)/(R+L), first vs last third;
- **max left eccentricity** reached;
- the number of **rest phases**;
- **time looking left** (yaw);
- **signal quality**;
- the z-score and **ALI** curves with rest phases shaded;
- the full decision log.

The report is a training summary, not a diagnostic assessment.

---

## Tuning cheat sheet

| Want | Change |
|---|---|
| shorter / longer warm-up | `RehabParams.warmup_s` |
| rehab: how fast spawns move left | `RehabParams.p_left` (start 0.1), `p_left_hit_step`, `p_left_miss_step`, `p_left_min`, `p_left_max` |
| rehab: ALI healing reward | `ali_reward_rise` (0.02), `ali_reward_cooldown_s` (5 s), `ali_reward_hp` (2) |
| rehab: harder enemies | `enemy_every_s` (300 s), `max_enemy_boost` (1), `enemy_drop_z`, `enemy_drop_hold_s`, `enemy_recover_hold_s`, `enemy_boost_cooldown_s` |
| more or less time to find a rehab target | `RehabParams.target_lifetime_s` (60 s) |
| rests trigger more easily (smoothed z-scores move little) | `fatigue_z_threshold`, `engagement_z_low`, `fatigue_hold_s` |
| staircase steps (left angle) | `staircase_window`, `staircase_up_hits`, `staircase_down_hits` |
| target angles / distance | `mc/config.py` `ECCENTRICITIES`, `TARGET_DISTANCE` (keep ≤ 45° at FOV 70) |
| fun difficulty curve | `director/fun.py` `LEVELS`, `FunParams` |
| fun target lifetime | `mc/config.py` `TARGET_LIFETIME_S`, `FunParams.mob_lifetime_s` / `sheep_lifetime_s` |
| how fast fun gets harder over time | `FunParams.level_every_s` (90 s) |
| how strongly falling engagement adds difficulty | `drop_z` (0.5), `drop_hold_s` (10 s), `peak_window_s` (120 s), `max_boost` (2), `boost_cooldown_s` (30 s) |
| when the boost eases off | `recover_z` (0.2), `recover_hold_s` (30 s) |
| enemy variety per spawn | `FunParams.newest_tier_share` (0.5) |
| enemy health / lifetime / roster | `mc/enemies.py` `ROSTER` |
| fun bow tiers (XP thresholds, enchantments) | `game/bow.py` `TIERS` |
| storm lightning: how often, how far | `game/effects.py` `LIGHTNING_EVERY_S`, `LIGHTNING_DISTANCE`, `LIGHTNING_MAX_ANGLE` |
| XP per kill / sheep penalty | `mc/enemies.py` `xp`, `MOB_XP_BONUS`, `SHEEP_XP_PENALTY` |
| sounds (ids, volume, pitch) / cue repeat | `game/effects.py` `*_SOUND`, `sound_at_cmd`; `game/trials.py` `CUE_REPEAT_S` |

Test any change without hardware first:
`poetry run python game_loop.py --mode rehab --sim --no-service --duration 600 --speed 0`.
That runs 10 simulated minutes in a few seconds, and the report opens
from `sessions/`.
