# BrainCraft shortcuts. Every target wraps scripts/braincraft.py, so
# without `make` (e.g. plain Windows) run the line under the target by
# hand, e.g. `poetry run python scripts/braincraft.py start --eeg gpype`.
# Variables: make start PLAYER=anna MODE=fun EEG=unicorn

PY      := poetry run python
RUN     := $(PY) scripts/braincraft.py
EEG     ?= gpype      # gpype | unicorn | synthetic | mock
GAME    ?= rcon       # rcon | sim | none
MODE    ?= rehab      # rehab | fun
PLAYER  ?= player
DURATION ?= 600
ARGS    ?=            # extra flags for `start`, e.g. ARGS="--oscar --no-browser"

START = $(RUN) start --eeg $(strip $(EEG)) --game $(strip $(GAME)) --mode $(strip $(MODE)) \
        --player $(strip $(PLAYER)) --duration $(strip $(DURATION)) $(ARGS)

.PHONY: help setup server doctor start dev demo-synthetic hud bridge check calibrate report test

help:            ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup:           ## poetry install + g.Pype; with ACCEPT_EULA=1 also the Minecraft server
	poetry install --with gpype
	$(RUN) setup $(if $(ACCEPT_EULA),--accept-eula)

server:          ## set up the Minecraft 26.3 server; ACCEPT_EULA=1 accepts the Minecraft EULA
	$(PY) scripts/setup_mc_server.py $(if $(ACCEPT_EULA),--accept-eula)

doctor:          ## what is installed / running
	$(RUN) doctor

start:           ## full demo: headset bridge, calibration, Minecraft, service, HUD, game
	$(START)

dev:             ## no hardware: HUD sliders + simulated game
	$(RUN) start --eeg mock --game sim --mode $(strip $(MODE)) $(ARGS)

demo-synthetic:  ## no headset: synthetic EEG + real Minecraft
	$(RUN) start --eeg synthetic --game rcon --mode $(strip $(MODE)) --player synthetic $(ARGS)

hud:             ## headset + service + HUD only, no game
	$(RUN) start --eeg $(strip $(EEG)) --game none --player $(strip $(PLAYER)) $(ARGS)

bridge:          ## only the headset -> LSL bridge (with scope window)
	$(PY) -m eeg.gpype_bridge --record sessions/raw

check:           ## signal sanity check: close your eyes, alpha should jump
	$(RUN) check

calibrate:       ## new baseline for PLAYER
	$(RUN) calibrate --player $(strip $(PLAYER)) --eeg $(strip $(EEG))

report:          ## HTML report of the latest session
	$(RUN) report

test:            ## test suite
	poetry run pytest -q
