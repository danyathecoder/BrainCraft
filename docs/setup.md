# BrainCraft setup guide

One section per service. Each has: what it is, prerequisites, commands
(Windows PowerShell and macOS where they differ), how to check it works,
and common errors.

> **Status (2026-11-14):** the commands in sections 3, 4 and 7–13 were
> run and checked: the Minecraft parts against a real headless 26.3
> server, the EEG parts against synthetic EEG only. Not tested yet: the
> real Unicorn headset (section 6; the g.Pype bridge was checked with
> g.Pype's own test generator), the Minecraft client (section 5) and
> Windows. If a command fails, run it with `--help` and fix this page.

---

## 1. Overview

```
Unicorn headset --Bluetooth--> eeg.gpype_bridge (or Unicorn LSL app) --LSL--> state_service :8000 --HTTP--> game_loop.py --RCON :25575--> Minecraft server <--:25565-- Minecraft client
                                                                          \--> HUD in browser (localhost:8000)
```

**Which machine runs what**

- **Demo laptop:** everything. LSL is most reliable on a single machine,
  so headset reader, state service, game loop, Minecraft server,
  Minecraft client and HUD all run on this one laptop. With the g.Pype
  bridge it can be **Windows x64, an Apple-silicon Mac or Linux x86-64**;
  the Unicorn Suite fallback needs **Windows**.
- **Teammates' laptops (any OS):** develop with the state service in
  **mock** mode (HUD sliders instead of EEG), synthetic EEG, and the game
  loop in **simulation** mode (no Minecraft needed). A local Minecraft
  server is optional.

**Start order (demo):** Minecraft server → `mc.world` → client joins →
headset + `eeg.gpype_bridge` → `eeg.check` → calibration → state service (EEG)
→ HUD → game loop → report afterwards. Full list in
[section 14](#14-demo-day-checklist).

| Port | What |
|---|---|
| 25565 | Minecraft server (client joins `localhost`) |
| 25575 | RCON (game loop → server) |
| 8000 | State service + HUD |

---

## 2. Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.10–3.14 | everything |
| Poetry | 2.x | dependencies (`poetry install`) |
| Git | any | the repo |
| Java | **25** for Minecraft 26.3 (verified from Mojang's version JSON; the setup script checks it) | Minecraft server |
| Minecraft Java Edition licence | one per laptop that plays | Minecraft client, `online-mode=true` server |
| g.Pype (g.tec), via `poetry install --with gpype` | 4.2.0 | headset → LSL (section 6.2): Windows x64, macOS arm64 (macOS 14+), Linux x86-64 |
| Unicorn Suite (g.tec) | current | fallback headset → LSL (section 6.3), **Windows only** |

**Python + Poetry**

- Windows: install Python from python.org (tick "Add python.exe to
  PATH"), then `py -m pip install --user pipx; py -m pipx ensurepath`,
  open a new terminal, `pipx install poetry`.
- macOS: `brew install python@3.13 pipx && pipx ensurepath && pipx install poetry`.
- Check: `poetry --version` → `Poetry (version 2.x)`.

**Java 25**

- Windows: Eclipse Temurin 25 MSI from <https://adoptium.net> (tick
  "Set JAVA_HOME variable" and "Add to PATH"), or
  `winget install EclipseAdoptium.Temurin.25.JDK`. Open a new terminal.
- macOS: `brew install openjdk@25`. It is **keg-only** (not on PATH):
  the binary is `/opt/homebrew/opt/openjdk@25/bin/java` (Apple Silicon;
  `/usr/local/opt/openjdk@25/bin/java` on Intel). Pass it to the setup
  script with `--java`, or set `JAVA_HOME=/opt/homebrew/opt/openjdk@25`.
- Check: `java -version` → `openjdk version "25..."`.
  (On macOS with keg-only Java: `/opt/homebrew/opt/openjdk@25/bin/java -version`.)

---

## 3. Repo and Python dependencies

```bash
git clone <repo-url> BrainCraft
cd BrainCraft
poetry install          # creates .venv, installs deps + dev group
poetry run pytest -q    # all tests offline; Minecraft live tests are skipped
```

On the laptop the headset connects to, also install the optional g.Pype
group (headset driver, LSL, Qt window; several hundred MB):

```bash
poetry install --with gpype
```

**Check:** pytest ends with `N passed, M skipped`. With the gpype group
installed, `tests/test_gpype_bridge.py` also runs a real g.Pype → LSL →
`EEGStream` round trip.

**Common errors**

- `poetry: command not found` → open a new terminal after
  `pipx ensurepath`.
- Wrong Python picked: `poetry env use 3.13` (or `py -3.13` path on
  Windows), then `poetry install` again.

---

## 4. Minecraft server

A vanilla Minecraft Java **dedicated server** (`server.jar`), version
**26.3**, with RCON enabled and a superflat world. RCON only exists on a
dedicated server, not in singleplayer or "Open to LAN". No mods, no
plugins.

### 4.1 Set up (online)

From the repo root:

```bash
poetry run python scripts/setup_mc_server.py --accept-eula
# macOS with keg-only Java:
poetry run python scripts/setup_mc_server.py --accept-eula --java /opt/homebrew/opt/openjdk@25/bin/java
```

What it does, in `server/` (git-ignored):

1. Reads Mojang's version manifest → version JSON for 26.3.
2. Downloads `server.jar` (~62 MB) and verifies its SHA-1. If a
   `server.jar` with the right SHA-1 is already there it skips the
   download. The version JSON is cached in `server/.braincraft-version.json`
   for offline reruns.
3. Reads the required Java version from the version JSON
   (`javaVersion.majorVersion`, 25 for 26.3) and checks `java -version`.
4. Writes/merges `server.properties` (keeps keys it does not manage):
   `enable-rcon=true`, `rcon.port=25575`, `rcon.password=braincraft`,
   `broadcast-rcon-to-ops=false`, `level-type=minecraft\:flat`,
   `generate-structures=false`, `difficulty=easy`, `spawn-protection=0`,
   `view-distance=8`, `simulation-distance=6`, `online-mode=true`,
   `motd=BrainCraft`, `spawn-monsters=false`, `allow-flight=true`, and
   `server-ip=` (empty) if missing.
5. Writes `eula.txt` with `eula=true` **only** with `--accept-eula`.
   Read the Minecraft EULA first: <https://aka.ms/MinecraftEULA>.
   Without the flag the server refuses to start.
6. Writes `start_server.bat` and `start_server.sh`
   (`java -Xms1G -Xmx2G -jar server.jar nogui`).

Options: `--version 26.3`, `--dir server`, `--rcon-password`,
`--rcon-port`, `--memory 2G`, `--java PATH`, `--jar PATH`, `--offline`,
`--skip-java-check`. Exit code 0 = ready, 1 = files written but
something to fix (Java, EULA, wrong jar version), 2 = no server.jar.

If you change the RCON password or port, tell the game too:
PowerShell `$env:RCON_PASSWORD="..."`, bash `export RCON_PASSWORD=...`
(also `RCON_PORT`, `RCON_HOST`; defaults in `mc/config.py`).

**Python can't verify HTTPS on macOS** (`CERTIFICATE_VERIFY_FAILED`,
python.org build): the script automatically retries with `curl`. To fix
Python itself, run `Install Certificates.command` from
`/Applications/Python 3.x/`.

### 4.2 Set up offline (venue Wi-Fi blocks Mojang)

1. On a phone hotspot (or at home), get the 26.3 `server.jar`: run the
   setup script there, or download it from
   <https://www.minecraft.net/en-us/download/server> (check it says 26.3).
   Copy it to a USB stick.
2. On the demo laptop:

```powershell
poetry run python scripts/setup_mc_server.py --jar D:\server.jar --accept-eula --offline
```

`--offline` skips Mojang entirely. Without metadata the SHA-1 can't be
checked, but the script reads the version and the Java requirement from
the `version.json` inside the jar and warns if it isn't 26.3. If the
laptop has ever run the online setup, the cached metadata is used and
the jar is SHA-1 checked.

### 4.3 First start

```powershell
# Windows (or double-click server\start_server.bat)
cd server; .\start_server.bat
```

```bash
# macOS
cd server && ./start_server.sh
```

- The first start generates the world (~30 s). Wait for
  `RCON running on 0.0.0.0:25575` and `Done (…)! For help, type "help"`.
- `No key layers in MapLike[{}]` in the log is harmless.
- **Windows Defender Firewall prompt** for Java: allow **Private
  networks** only (untick Public). Everything runs on localhost anyway;
  allowing private lets a teammate join from the same network.
- Optional, local only: set `server-ip=127.0.0.1` in
  `server/server.properties` so nobody else on the network can join or
  reach RCON.
- If `server/world` already existed from a non-flat run, stop the
  server, delete `server/world`, start again.

### 4.4 World setup

With the server running (a player doesn't need to be online):

```bash
poetry run python -m mc.world               # or: --player <name>
```

It force-loads the arena chunks (needed: with nobody online, chunks are
not loaded and targets can't be summoned), sets game rules (night
forever, no mobs/weather, no drops, keep inventory…), sets midnight,
clear weather, easy difficulty, builds the tower, sets the world spawn,
and, if a player is online, puts them on the tower facing north in
adventure mode with a bow and arrows. With nobody online it prints
`nobody online, skipping…`; run it again after joining. Safe to rerun.

### 4.5 Check RCON works

```bash
poetry run python -c "from mc.rcon import Rcon; r = Rcon(); print(r.cmd('list')); print(r.cmd('forceload query 0 0'))"
```

Expected: `There are 0 of a max of 20 players online:` and
`Chunk at [0, 0] in minecraft:overworld is marked for force loading`.

Live tests against the running server:

```bash
BRAINCRAFT_MC_IT=1 poetry run pytest tests/test_mc_integration.py -q              # macOS
$env:BRAINCRAFT_MC_IT="1"; poetry run pytest tests/test_mc_integration.py -q        # PowerShell
```

### 4.6 Stop

Type `stop` in the server console (saves the world). Or over RCON:
`poetry run python -c "from mc.rcon import Rcon; Rcon().cmd('stop')"`.
Avoid closing the window with the X.

### Common errors

- `Failed to load eula.txt` / server exits at once → run setup with
  `--accept-eula`.
- `UnsupportedClassVersionError … class file version 69.0` → Java is
  too old; install Java 25 and rerun the setup script (it rewrites the
  start scripts with `--java`).
- `**** FAILED TO BIND TO PORT!` → another server is running (close it,
  or check Task Manager / `lsof -i :25565`).
- More in [section 15](#15-troubleshooting).

---

## 5. Minecraft client

The **unmodified official game**. No mods, no Fabric (an optional
Fabric client mod is future work).

1. Minecraft Launcher → **Installations** → **New installation** →
   Version **26.3** (must match the server exactly) → Create.
2. **Play** with that installation → **Multiplayer** → **Direct
   Connection** → Server address `localhost` → Join Server.
3. Run `poetry run python -m mc.world` again (puts you on the tower).

**Required settings** (Options):

| Setting | Value | Why |
|---|---|---|
| FOV | **70** ("Normal") | target eccentricities assume it (`mc/config.py: CLIENT_FOV_DEG`) |
| Video Settings → Render Distance | 8 | matches the server |
| Pause on Lost Focus | **OFF** (toggle with **F3+P**) | game keeps running when the HUD window has focus |
| Video Settings → Fullscreen | ON on the demo screen (F11) | |
| Controls → Mouse Sensitivity | comfortable for the player | |

Adventure mode, bow and arrows are set by `mc.world`; don't change the
game mode by hand.

**Check:** you stand on a small stone tower in a flat night world,
facing north, with a bow. F3 shows `Facing: north`.

**Common errors:** `Outdated client!` / `Outdated server!` → versions
differ; pick the 26.3 installation. `Failed to log in: Invalid session`
→ restart the launcher and sign in again (`online-mode=true` needs a
real account).

---

## 6. EEG headset (g.tec Unicorn Hybrid Black)

8-channel EEG headset, Bluetooth, 250 Hz. Something has to read it and
publish the raw samples as a Lab Streaming Layer (LSL) stream, which the
state service reads. Two ways, same stream layout, so nothing
downstream changes:

| | Reader | OS | Use |
|---|---|---|---|
| **A** | `eeg/gpype_bridge.py` (g.tec's g.Pype SDK) | Windows x64, macOS arm64, Linux x86-64 | default |
| **B** | Unicorn Suite + Unicorn LSL app | Windows | fallback if A fails |

The reader is a **separate process** on purpose: after a Bluetooth drop
you restart only the reader; the state service, HUD and game keep
running (the HUD shows STALE meanwhile). See
[architecture.md](architecture.md).

### 6.1 Fit the headset

1. Charge it; turn it on.
2. Fit the cap: electrodes in contact with the scalp, ear-clip
   references on both mastoids. If the signal is poor with dry contact,
   use electrode gel.
3. Give the electrodes a few minutes to settle before calibrating
   (the first minutes drift).

### 6.2 Option A: g.Pype bridge (default)

Install once (section 3): `poetry install --with gpype`. No licence and
no Unicorn Suite needed (g.Pype 4.2.0; free for non-commercial use).

**Pair the headset**

- **Windows:** Settings → Bluetooth & devices → Add device → Bluetooth →
  pick `UN-XXXX.XX.XX` (serial on the headset). Required before the
  bridge can open it.
- **macOS (Apple silicon, macOS 14+):** no manual pairing documented;
  the first run asks for **Bluetooth permission** for your terminal
  (System Settings → Privacy & Security → Bluetooth → allow Terminal /
  iTerm / VS Code). Restart the terminal after allowing it.

**Run** (in its own terminal, in the foreground; don't start it with `&`):

```bash
poetry run python -m eeg.gpype_bridge
```

A g.Pype window opens with a live scope of the 8 EEG channels (filtered
1–30 Hz for display only; LSL gets the raw signal). Expected output:

```
Unicorn Hybrid Black -> LSL stream 'BrainCraft-Unicorn' (type EEG, 250 Hz)
Streaming. Close the window or press Ctrl+C to stop.
```

Use the scope to check the fit: blinks show as big slow deflections on
Fz, jaw clench as dense high-frequency noise everywhere, a flat or
railing trace is a bad electrode.

| Option | What |
|---|---|
| `--record sessions/raw` | also write raw samples to `sessions/raw_<timestamp>.csv` (keep these: offline threshold tuning) |
| `--serial UN-...` | pick a headset when several are paired (default: first one) |
| `--name X` | LSL stream name (default `BrainCraft-Unicorn`); then set `BRAINCRAFT_LSL_NAME=X` for `eeg.check`, calibration and the state service |
| `--oscar` | g.tec OSCAR artifact removal. Calibrate and play with the **same** setting, or the baseline won't match |
| `--test-signal` | the device's built-in test signal instead of electrodes (checks Bluetooth and the pipeline) |
| `--fake` | no headset: g.Pype sine generator (plumbing test only; for realistic data use section 7) |
| `--no-scope` | window without the scope (less CPU) |
| `--headless` | no window; **Windows/Linux only** (on macOS the Unicorn driver needs the window's event loop) |

Stream columns (the layout `eeg/layout.py` expects, so no
`BRAINCRAFT_EEG_COLS` / `BRAINCRAFT_GYRO_COLS` needed): 0–7 EEG (µV) Fz,
C3, Cz, C4, Pz, PO7, Oz, PO8; 8–10 accelerometer (g); 11–13 gyroscope
(deg/s); 14 battery (%); 15 counter; 16 validation. g.Pype may append an
arrival-stamp column 17; it is ignored.

Without a headset you can check the bridge with
`poetry run python -m eeg.gpype_bridge --fake`, then
`poetry run python -m eeg.check` in a second terminal.

### 6.3 Option B: Unicorn Suite + Unicorn LSL (Windows fallback)

> We have not used Unicorn Suite yet. Every UI step below: **verify
> against g.tec's Unicorn Suite documentation.**

1. Install **Unicorn Suite** on the demo laptop (download from g.tec
   with the licence that comes with the headset). Install the **Unicorn
   LSL** app from inside the Suite if it isn't there. *(verify)*
2. Plug in the **Unicorn Bluetooth dongle** (use it rather than the
   laptop's built-in Bluetooth). *(verify)*
3. Turn the headset on; pair it if asked. *(verify)*
4. Open **Unicorn LSL** → select the device → **Open** → **Start**
   streaming. *(verify)*

Only one program can hold the headset: stop the g.Pype bridge first.
If the Unicorn LSL columns differ from the layout above, set
`BRAINCRAFT_EEG_COLS` / `BRAINCRAFT_GYRO_COLS` (see `eeg/layout.py`).

### 6.4 Check (both options)

```bash
poetry run python -m eeg.stream
```

Expected: one EEG stream listed (`'BrainCraft-Unicorn' type='EEG'
channels= 17 srate=250` for option A). Then run the signal check in
[section 8](#8-signal-sanity-check).

**Common errors**

- `g.Pype is not installed` → `poetry install --with gpype`.
- `No Unicorn device available. Please pair with a Unicorn first.` →
  headset off, out of range, not paired (Windows), or no Bluetooth
  permission for the terminal (macOS).
- `already open in this process` / device busy → another program (the
  Unicorn Suite, a second bridge) holds the headset; close it.
- Read errors on macOS mentioning the run loop → you used `--headless`
  or the window was closed; run without `--headless`.
- Bridge exits after a few failed reads (Bluetooth drop) → move closer,
  restart the bridge; the state service picks the stream up again.
- No stream listed → see "LSL stream not found" in
  [section 15](#15-troubleshooting).
- Flat or very noisy channels → refit electrodes, add gel, ask the
  player to relax the jaw.

---

## 7. Synthetic EEG (no headset)

For development without the headset: an 8-channel, 250 Hz signal with
posterior alpha, an engagement level, blinks, jaw clench and head motion.

As an LSL stream (looks like the headset to everything downstream):

```bash
poetry run python -m eeg.synthetic --lsl [--name BrainCraftSynthetic] [--seed N] [--fatigue-rate 0.2]
```

Type a command and press Enter: `e` eyes closed/open (alpha up/down),
`b` blink, `j` jaw clench, `m` head motion, `f+` / `f-` fatigue up/down,
`s` show state, `q` quit.

Or in-process inside the state service, no LSL at all: set
`BRAINCRAFT_EEG_INPUT=synthetic` (see [section 10](#10-state-service)).
That is a quick smoke test only, because it can't be controlled. For a
**controllable demo without the headset**, run `eeg.synthetic --lsl` in
a second terminal and keep `BRAINCRAFT_EEG_INPUT=lsl`; `f+` a few times
makes the director start a rest phase.

**Check:** `poetry run python -m eeg.stream` lists the synthetic stream.

---

## 8. Signal sanity check

```bash
poetry run python -m eeg.check                    # default: --source lsl
poetry run python -m eeg.check --source synthetic
# options: --name STREAM, --window 2 (seconds), --seconds N (stop after N)
```

Every second it prints alpha power per channel, the PO7/Oz/PO8 alpha
relative to the first 5 s, the Fz/Cz engagement ratio and any artifact.
With `--source synthetic` it takes the same stdin commands as section 7.

**Check:** ask the player to close their eyes for ~10 s: alpha power at
**Oz / PO7 / PO8** should jump clearly, and drop when eyes open again.
With synthetic input, press `e` in the synthetic generator.

**Common errors:** no alpha change → electrodes at the back aren't in
contact (hair), or the wrong stream is selected.

---

## 9. Calibration

Records the player's baseline, about **2.5 minutes**. Ask them to sit
still and follow the prompts: 60 s eyes open, 30 s eyes closed, 60 s
easy game.

```bash
poetry run python -m eeg.calibrate --out sessions/baseline.json
poetry run python -m eeg.calibrate --source synthetic --out sessions/baseline.json
# options: --name STREAM, --fast (12/10/12 s phases, for testing)
```

It reports whether the **eyes-closed alpha jump** was seen. If not,
check the electrodes at the back or the channel layout before going on.

- **One baseline per person and source.** Synthetic and headset runs
  both write `sessions/baseline.json` by default: delete it or point
  `BRAINCRAFT_BASELINE` at another file when switching.
- If no baseline exists, the state service in EEG mode calibrates by
  itself first (150 s, prompts only on the service console), so run
  this step explicitly before the demo.
- The z-scores are smoothed (~15 s), so they move less than raw values.
  If rest phases never trigger with the real headset, lower the
  thresholds in `director/rehab.py` (`RehabParams.fatigue_z_threshold`,
  `engagement_z_low`).

**Check:** `sessions/baseline.json` exists; the state service in EEG
mode shows z-scores around 0 at rest.

---

## 10. State service

FastAPI hub on port 8000: brain state, game status, decisions, mode,
and the HUD page.

**Mock mode** (default, HUD sliders, no headset):

```bash
poetry run uvicorn state_service:app --port 8000
```

**EEG mode:**

```powershell
# PowerShell
$env:BRAINCRAFT_SOURCE="eeg"; $env:BRAINCRAFT_EEG_INPUT="lsl"   # or "synthetic"
poetry run uvicorn state_service:app --port 8000
```

```bash
# macOS / bash
BRAINCRAFT_SOURCE=eeg BRAINCRAFT_EEG_INPUT=lsl poetry run uvicorn state_service:app --port 8000
```

`BRAINCRAFT_EEG_INPUT=lsl` reads the headset (or `eeg.synthetic --lsl`);
`synthetic` generates EEG in-process. In PowerShell the `$env:` values
stay set for that terminal; `Remove-Item Env:BRAINCRAFT_SOURCE` to go
back to mock.

More EEG settings (environment variables):

| Variable | Default | |
|---|---|---|
| `BRAINCRAFT_LSL_NAME` | first stream of type `EEG` | pick a stream by name |
| `BRAINCRAFT_BASELINE` | `sessions/baseline.json` | baseline file |
| `BRAINCRAFT_EEG_COLS` | `0-7` | EEG columns in the LSL stream |
| `BRAINCRAFT_GYRO_COLS` | `11-13` | gyro columns, or `none` |

The Unicorn column layout is **assumed** (0–7 EEG, 8–10 accelerometer,
11–13 gyro); check it with `eeg.check` on the real stream.

| Endpoint | |
|---|---|
| `GET /state` | brain state (~1 Hz) + `source` |
| `POST /mock` | set mock values (mock mode only) |
| `GET/POST /status` | game status, game loop → HUD |
| `GET/POST /decisions` | director decisions with reasons |
| `GET/POST /mode` | `rehab` / `fun` |
| `GET /` | HUD |

**Check:** <http://localhost:8000/state> returns JSON with a fresh `t`
and the right `source`.

---

## 11. Game loop

Drives the game: reads `/state`, spawns targets over RCON, detects
hits/misses, runs the director, logs to `sessions/`.

```bash
# Real Minecraft (server running, player joined, state service running)
poetry run python game_loop.py --mode rehab --rcon --name anna-1

# Simulation, no Minecraft, no state service, 2 min in ~7 s, with a rest phase
poetry run python game_loop.py --mode rehab --sim --no-service --duration 120 --speed 20 --fake-fatigue-at 20

# Fun mode in simulation, calm player -> escalation
poetry run python game_loop.py --mode fun --sim --no-service --speed 20 --fake-calm-at 10
```

| Option | |
|---|---|
| `--mode rehab\|fun` | starting mode (the HUD buttons switch it live) |
| `--rcon` / `--sim` | real server, or the simulated player (left-neglect profile) |
| `--name NAME` | **session name** for the CSV files (not the Minecraft player) |
| `--duration S` | session length, default 600 s |
| `--speed X` | sim only: X times real time, `0` = as fast as possible |
| `--state-url URL` | state service, default `http://localhost:8000` |
| `--no-service` | built-in fake brain state instead of `/state` |
| `--fake-fatigue-at S` / `--fake-calm-at S` | fake brain only: become tired / calm at S seconds |
| `--seed N`, `--log-dir DIR` | reproducible sims, where CSVs go (default `sessions/`) |

The Minecraft player is chosen with the `MC_PLAYER` environment
variable (default `@p`, the nearest player, fine with one player).
With `--rcon` and no state service, EEG rules pause (quality "bad")
until `/state` answers. Ctrl+C stops and prints the CSV paths.

**Check:** targets appear left/right of the tower; the HUD decision log
fills; two CSVs (`<stamp>-<name>.csv` and `-state.csv`) appear in
`sessions/`.

---

## 12. HUD

Open <http://localhost:8000> in a browser, on a **second screen** facing
the judges. It shows the live brain state, game status, the director's
decision log (each with its reason) and **mode buttons** (rehab / fun).
In mock mode it also has sliders for the brain state.

**Check:** values update about once per second; clicking a mode button
changes the mode shown by `GET /mode`.

---

## 13. Session report

After a session, one offline HTML training session report (~5 MB,
plotly inlined, opens without internet) from the events CSV and, if
present, the `-state.csv` next to it.

```bash
poetry run python -m report.make_report sessions/<file>.csv --open
# options: --state FILE (other state CSV), --out FILE (default <file>-report.html)
```

**Check:** the report opens in the browser with hits/misses per side
and the director's decisions.

---

## 14. Demo-day checklist

Arrive early; do it in this order.

1. [ ] Laptop on power, notifications off, Minecraft Launcher signed in.
2. [ ] **Server:** `server\start_server.bat`; wait for `Done` and
       `RCON running on`.
3. [ ] **World:** `poetry run python -m mc.world`.
4. [ ] **Client:** launch 26.3 → Direct Connection `localhost`. FOV 70,
       render distance 8, Pause on Lost Focus OFF (F3+P), fullscreen.
       Run `python -m mc.world` again → player on the tower with a bow.
5. [ ] **Headset:** on and fitted, then in its own terminal
       `poetry run python -m eeg.gpype_bridge --record sessions/raw`
       (fallback: Unicorn LSL streaming). Scope shows clean traces.
6. [ ] **Signal:** `poetry run python -m eeg.check` → eyes-closed alpha
       jump at Oz/PO7/PO8.
7. [ ] **Calibration:** `poetry run python -m eeg.calibrate --out sessions/baseline.json` (2.5 min).
8. [ ] **State service (EEG):** `$env:BRAINCRAFT_SOURCE="eeg"; poetry run uvicorn state_service:app --port 8000`.
9. [ ] **HUD:** <http://localhost:8000> on the second screen.
10. [ ] **Game loop:** `poetry run python game_loop.py --mode rehab --rcon --name <session>`.
11. [ ] **After:** `poetry run python -m report.make_report sessions/<file>.csv --open`.
12. [ ] **Backup video** of a full run on the laptop desktop and a USB
        stick, in case the headset or venue fails.

Fallbacks: no headset → synthetic EEG (section 7); no EEG at all → mock
mode + HUD sliders; no Minecraft → `game_loop.py --sim`.

---

## 15. Troubleshooting

**RCON: connection refused** (`RconConnectionError … refused the
connection`) → server not running or not ready (wait for
`RCON running on`), `enable-rcon=true` missing, or the port differs from
`RCON_PORT` (default 25575). Check `server/server.properties`.

**RCON: wrong password** (`RconAuthError`) → `rcon.password` in
`server.properties` must equal `RCON_PASSWORD` (default `braincraft`).
Restart the server after editing `server.properties`.

**`Outdated client!` / `Outdated server!`** → client and server
versions differ. Use the 26.3 launcher installation, and check
`server.jar` is 26.3 (the setup script warns).

**`No player was found`** → nobody is online, or the name passed with
`--player` / `--name` / `MC_PLAYER` doesn't match the username (case
matters in commands). Join first; `mc.world` skips player steps until
then.

**LSL stream not found** → run everything on the **same laptop**; LSL
uses multicast, which venue Wi-Fi often blocks. Check the reader is
actually streaming (bridge printed `Streaming.`; Unicorn LSL started,
not just opened). Allow Python through the
Windows firewall (private networks). Close other apps holding the
headset. Test the pipeline with `eeg.synthetic --lsl`.

**Java too old** (`UnsupportedClassVersionError`, or the setup script
says `Java N is too old`) → install Java 25 (section 2), open a new
terminal, rerun the setup script (with `--java PATH` if Java isn't on
PATH).

**Targets don't appear / `That position is not loaded`** → chunks not
loaded: run `python -m mc.world` (it force-loads the arena); check with
`forceload query 0 0`. If targets spawn underground or float, `GROUND_Y`
is wrong: stand on the grass, press F3, read the Y of the `XYZ` line,
set `GROUND_Y` in `mc/config.py` (superflat default: -60).

**HUD shows STALE** → `/state` timestamp isn't updating: the state
service crashed or (EEG mode) the LSL stream stopped. Check the
uvicorn terminal, restart the bridge (or Unicorn LSL streaming), or
fall back to mock mode.

**Targets burn at dawn** (rest phase) → zombies burn in daylight. The
default target is `minecraft:husk`, which doesn't; check
`MC_TARGET_ENTITY` isn't set to a zombie.
