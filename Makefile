# BrainCraft shortcuts. Every target wraps scripts/braincraft.py, so
# without `make` (e.g. plain Windows) run the line under the target by
# hand, e.g. `poetry run python scripts/braincraft.py start --eeg gpype`.
# Variables: make start PLAYER=diuna MODE=fun EEG=unicorn

PY      := poetry run python
RUN     := $(PY) scripts/braincraft.py
EEG     ?= gpype      # gpype | unicorn | synthetic | mock
GAME    ?= rcon       # rcon | sim | none
MODE    ?= rehab      # rehab | rehab_01 | fun
PLAYER  ?= player
DURATION ?= 600
MC_NAME ?=            # optional: only these Minecraft usernames may join, e.g. MC_NAME="Steve Alex"
EEG_HOST ?=           # two computers: the headset computer's IP, on the server computer
OSCAR   ?=            # OSCAR=1: the headset bridge runs g.tec OSCAR artifact removal
SKIP_CAL ?=           # SKIP_CAL=1: no calibration protocol (quick 30 s baseline if none saved)
ARGS    ?=            # extra flags for `start`, e.g. ARGS="--oscar --no-browser"

START = $(RUN) start --eeg $(strip $(EEG)) --game $(strip $(GAME)) --mode $(strip $(MODE)) \
        --player $(strip $(PLAYER)) --duration $(strip $(DURATION)) \
        $(foreach n,$(MC_NAME),--mc-name $(n)) $(if $(strip $(SKIP_CAL)),--skip-calibration) \
        $(HOSTARG) $(OSCARARG) $(ARGS)
HOSTARG = $(if $(strip $(EEG_HOST)),--eeg-host $(strip $(EEG_HOST)))
OSCARARG = $(if $(strip $(OSCAR)),--oscar)

.PHONY: help setup server doctor start dev demo-synthetic hud headset bridge check calibrate \
        artifacts train-artifacts replay whitelist report test

help:            ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup:           ## poetry install + g.Pype; with ACCEPT_EULA=1 also the Minecraft server
	poetry install --with gpype
	$(RUN) setup $(if $(ACCEPT_EULA),--accept-eula)

server:          ## set up the Minecraft 26.3 server; ACCEPT_EULA=1 accepts the Minecraft EULA
	$(PY) scripts/setup_mc_server.py $(if $(ACCEPT_EULA),--accept-eula)

doctor:          ## what is installed / running
	$(RUN) doctor $(HOSTARG)

start:           ## everything on one computer (or with EEG_HOST: the server side of two)
	$(START)

dev:             ## no hardware: HUD sliders + simulated game
	$(RUN) start --eeg mock --game sim --mode $(strip $(MODE)) $(ARGS)

demo-synthetic:  ## no headset: synthetic EEG + real Minecraft
	$(RUN) start --eeg synthetic --game rcon --mode $(strip $(MODE)) --player synthetic \
	    $(foreach n,$(MC_NAME),--mc-name $(n)) $(ARGS)

hud:             ## headset + service + HUD only, no game
	$(RUN) start --eeg $(strip $(EEG)) --game none --player $(strip $(PLAYER)) \
	    $(if $(strip $(SKIP_CAL)),--skip-calibration) $(HOSTARG) $(OSCARARG) $(ARGS)

headset:         ## two computers: on the headset computer, bridge only + connection info
	$(RUN) headset $(OSCARARG) $(ARGS)

bridge:          ## only the headset -> LSL bridge (with scope window)
	$(PY) -m eeg.gpype_bridge --record sessions/raw

check:           ## signal sanity check: close your eyes, alpha should jump
	$(RUN) check $(HOSTARG)

calibrate:       ## new baseline for PLAYER
	$(RUN) calibrate --player $(strip $(PLAYER)) --eeg $(strip $(EEG)) $(HOSTARG)

artifacts:       ## labelled 3.5 min artifact recording for PLAYER (OSCAR=1 if the bridge runs --oscar)
	$(RUN) artifacts --player $(strip $(PLAYER)) $(if $(strip $(OSCAR)),--oscar) $(HOSTARG)

train-artifacts: ## train the artifact model on sessions/artifacts-*.npz (saved only if better)
	$(RUN) train-artifacts $(ARGS)

replay:          ## signal-quality numbers for all recordings; ARGS="--extra --vs-legacy"
	$(RUN) replay $(ARGS)

whitelist:       ## allow MC_NAME to join the running Minecraft server
	$(RUN) whitelist $(MC_NAME)

report:          ## HTML reports for every session that has none yet; opens the latest
	$(RUN) report

test:            ## test suite
	poetry run pytest -q
