# Architecture

How the BrainCraft applications communicate and how the game loop works.
GitHub renders the Mermaid diagrams below.

## How the applications talk
Everything runs on **one demo laptop** (Windows, because of Unicorn Suite). During development, everyone else runs `state_service.py` in **mock mode**, with HUD sliders instead of the headset.

```mermaid
flowchart LR
  H["Unicorn headset<br/>8-ch EEG"] -->|"Bluetooth"| US

  subgraph Laptop["Demo laptop"]
    US["Unicorn Suite<br/>Unicorn LSL app"]
    SS["state_service.py<br/>FastAPI :8000<br/>EEG pipeline eeg/"]
    GL["Game loop, Python<br/>director/ + game/ + log/"]
    MS["Minecraft server<br/>server.jar or Paper<br/>RCON :25575"]
    MC["Minecraft client<br/>unmodified"]
    HUD["HUD in browser<br/>localhost:8000"]
    CSV[("sessions/*.csv")]
    REP["report/make_report.py<br/>HTML report"]
  end

  US -->|"LSL stream, 250 Hz"| SS
  SS -->|"GET /state, 1 Hz"| GL
  GL -->|"RCON commands:<br/>summon, title, time, playsound"| MS
  MS -->|"replies: target alive?, yaw"| GL
  MS <-->|"Minecraft protocol :25565"| MC
  SS -->|"GET /state"| HUD
  HUD -.->|"POST /mock, dev only"| SS
  GL -->|"events + decisions"| CSV
  CSV --> REP
```

| Link | Protocol | Port |
|---|---|---|
| Headset → Unicorn Suite | Bluetooth | — |
| Unicorn Suite → state service | LSL | network multicast (same machine is safest) |
| State service → game loop, HUD | HTTP `GET /state` | 8000 |
| Game loop ↔ Minecraft server | RCON (TCP), plain command text | 25575 |
| Minecraft server ↔ client | normal multiplayer, join `localhost` | 25565 |

## The game loop
Minecraft only shows the world and takes the player's input. The Python game loop (~5 Hz) makes every decision and changes the world only through RCON commands.

```mermaid
flowchart LR
  ST["/state<br/>brain state, 1 Hz"] --> BE["Brain events<br/>fatigue, low engagement"]
  MP["Minecraft<br/>polled replies"] --> GE["Game events<br/>hit, miss, yaw"]
  BE --> D["Director<br/>rehab / fun"]
  GE --> D
  D --> DEC["Decisions<br/>each with a reason"]
  DEC --> RC["RCON commands"]
  RC --> MW["Minecraft world<br/>the player sees it"]
  MW --> MP
  BE --> LOG["EventLogger<br/>+ HUD decision log"]
  GE --> LOG
  DEC --> LOG
```

- **Game event:** poll `execute if entity @e[tag=t17]`. The target is gone before 6 s → hit (feedback sound, staircase update). Still there → miss (smoke, kill, maybe cues on).
- **Brain event:** e.g. `fatigue_z > 1.5` for 20 s with good quality → rest phase shown as dawn (`time set 23000`, "Take a breath"), then back to midnight with slower spawns.
- Every decision is mirrored in-game (`title @p actionbar …`) and in the HUD log, so judges see why the game changed.
