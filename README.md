# BrainCraft

Neuroadaptive Minecraft game for Heroes of the Brain 2026. An AI
"director" reads the player's brain state from EEG (g.tec Unicorn) plus
in-game performance, and reshapes the game in real time.

- **Rehab mode (first):** visual scanning training inspired by therapy
  for post-stroke spatial neglect. Each signal has one job:
  - **warm-up:** the first 60 s on the right side only;
  - **the brain decides which side:** a sustained rightward alpha
    lateralization (ALI, relative to the player's own calibration) sends
    more targets left;
  - **performance decides how far:** a left-eccentricity staircase, with
    cues after failures;
  - **fatigue decides when to rest:** an EEG-driven dawn rest phase.

  Targets are skeletons that live 60 s and shoot back, so ignoring the
  left has a cost, and the player can die (respawn is immediate).
- **Fun mode (second):** same engine, the director builds tension arcs.
  Difficulty rises every 90 s (pace, count, enemy type: skeleton → stray
  → bogged → pillager → blaze, all shooting), and up to two levels more
  when the player's engagement falls below its own recent peak. The
  player gets an enchanted bow.

How both modes spawn targets, which metrics drive them and every rule
with its numbers: **[docs/modes.md](docs/modes.md)**.

## Roles

| Role | Owns |
|---|---|
| **P1 EEG** | `eeg/`: LSL stream, filters, band powers, indices, artifacts, calibration |
| **P2 Backend + director** | `state_service.py`, `director/`, `log/`, mode switch |
| **P3 Minecraft** | `mc/`, `game/`: server, RCON, map, targets, hit/miss/RT, cues |
| **P4 HUD, report, pitch** | `hud/`, `report/`, slides, demo script |

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for diagrams of how
the applications communicate (headset → LSL → state service → game
loop → RCON → Minecraft) and of the director's game loop.

## Contracts

Defined in [`contracts.py`](contracts.py). Don't change them without
telling the team.

- **`GET /state`** (~1 Hz): `t, engagement_z, fatigue_z, alpha_low_z,
  quality ("ok"|"bad"), artifact ("none"|"blink"|"jaw"|"motion"),
  jaw_clench`
- **`GET/POST /status`** (`GameStatus`, game loop → HUD), **`/decisions`**
  (director decisions with reasons), **`/mode`** (`rehab`|`fun`, HUD → game)
- **Events CSV** (`sessions/<stamp>-<name>.csv`): `t, event, target_id,
  side, ecc_deg, rt_ms, value, reason`, where `event` is one of `spawn,
  found, hit, miss, false_tag, rest_start, rest_end, decision, yaw`
- **State CSV** (`<same stem>-state.csv`): one `BrainState` row per second
- Every director decision is logged with a human-readable `reason`.

## Layout

```
contracts.py         shared data formats
state_service.py     FastAPI hub: /state, /mock, /status, /decisions, /mode, HUD
game_loop.py         runs a session: director + trials + effects   (P2/P3)
eeg/                 headset bridge (g.Pype -> LSL), synthetic EEG,
                     stream, features, calibration, pipeline,
                     signal check                                  (P1)
director/            rehab and fun directors                       (P2)
log/                 session event + state logger                  (P2)
mc/                  RCON client, world setup, SimRcon simulator   (P3)
game/                geometry, trial runner, in-game effects       (P3)
hud/                 live HUD page (offline, no CDN)               (P4)
report/              HTML session report from the CSVs             (P4)
scripts/             launcher braincraft.py, Minecraft server setup
Makefile             shortcuts for the launcher (make start, make dev)
docs/                architecture diagrams, setup guide
sessions/            recordings and logs (git-ignored)
```

## Quickstart

**Full setup for every service (Minecraft server and client, headset,
EEG, game loop, HUD, report): [`docs/setup.md`](docs/setup.md).**

Fastest: the launcher starts and stops everything in order, one Ctrl+C
stops it all ([setup.md, fast path](docs/setup.md#fast-path-launcher-and-make)).

```bash
make setup ACCEPT_EULA=1    # once: deps + g.Pype + Minecraft server (accepts the Minecraft EULA)
```

**A. Everything on one computer** (headset paired to it):

```bash
make start PLAYER=diuna              # bridge, calibration, Minecraft server, service, HUD, game
make start PLAYER=diuna MODE=fun     # fun mode
make start PLAYER=diuna OSCAR=1      # with g.tec OSCAR artifact removal (own baseline: PLAYER=diuna-oscar)
```

Join `localhost` in Minecraft 26.3; the HUD opens at <http://localhost:8000>.

**B. Two computers** (headset laptop + server laptop on the same network,
[setup.md, two computers](docs/setup.md#two-computers)):

```bash
# headset computer: bridge only, prints its IP and the server command
make headset                        # OSCAR=1 for OSCAR
# server computer: Minecraft server, service, HUD, game
make start EEG_HOST=<headset IP> PLAYER=diuna
```

Minecraft on the headset computer joins the server computer's IP; the
HUD is at `http://<server IP>:8000` from either.

Common extras: `SKIP_CAL=1` (no calibration protocol), `DURATION=300`,
`make doctor` (what's installed and running), `make report`.
No hardware at all: `make dev` (HUD sliders + simulated game).

Dependencies are managed with [Poetry](https://python-poetry.org/) 2.x.

```bash
poetry install                   # creates the venv, installs deps + dev group
poetry install --with gpype      # + headset bridge (laptop with the headset)
poetry run pytest -q             # offline; live Minecraft tests are skipped

# Mock brain state + HUD with sliders, no headset needed
poetry run uvicorn state_service:app --port 8000
# open http://localhost:8000

# Headset -> LSL (own terminal), see docs/setup.md section 6
poetry run python -m eeg.gpype_bridge

# A whole rehab session without Minecraft or EEG, in seconds
poetry run python game_loop.py --mode rehab --sim --no-service --duration 120 --speed 20 --fake-fatigue-at 20
poetry run python -m report.make_report sessions/<file>.csv --open
```

Add dependencies with `poetry add <package>` and commit `poetry.lock`.
