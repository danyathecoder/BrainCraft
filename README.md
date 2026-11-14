# BrainCraft

Neuroadaptive Minecraft game for Heroes of the Brain 2026. An AI
"director" reads the player's brain state from EEG (g.tec Unicorn) plus
in-game performance, and reshapes the game in real time.

- **Rehab mode (first):** visual scanning training inspired by therapy
  for post-stroke spatial neglect, with EEG-based fatigue pacing.
- **Fun mode (second):** same engine, the director builds tension arcs.

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
- **Events CSV** (`sessions/<session>.csv`): `t, event, target_id, side,
  ecc_deg, rt_ms, value, reason`, where `event` is one of `spawn, hit,
  miss, false_tag, rest_start, rest_end, decision, yaw`
- Every director decision is logged with a human-readable `reason`.

## Layout

```
contracts.py         shared data formats
state_service.py     FastAPI: /state (mock or EEG), /mock, HUD   (P2)
eeg/                 stream, features, calibration               (P1)
director/            rehab and fun directors                     (P2)
log/                 session event logger                        (P2)
mc/                  RCON wrapper, world setup                   (P3)
game/                target geometry and tracking                (P3)
hud/                 live HUD page                               (P4)
report/              session report from the events CSV          (P4)
sessions/            recordings and logs (git-ignored)
```

## Quickstart

Dependencies are managed with [Poetry](https://python-poetry.org/) 2.x.

```bash
poetry install                   # creates the venv, installs deps + dev group
poetry add <package>             # add a dependency (commit poetry.lock too)

# Mock brain state + HUD with sliders, no headset needed
poetry run uvicorn state_service:app --port 8000
# open http://localhost:8000
```

Minecraft: Java server with `enable-rcon=true`, `rcon.password` and
`rcon.port=25575` in `server.properties`, superflat world. Connection
settings live in `mc/config.py`.
