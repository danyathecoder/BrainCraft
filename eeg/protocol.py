"""Labelled artifact recording (~3.5 min) for tuning and training (P1).

    python -m eeg.protocol --player diuna [--name STREAM] [--oscar] [--fast]
    python -m eeg.protocol --player test --source synthetic --fast   # no headset

Prompts the player through phases and saves raw EEG + gyro with labels to
sessions/artifacts-<player>-<time>.npz. Phases:

    still        40 s  sit still, eyes open                      -> "none"
    eyes_closed  20 s  eyes closed                               -> "none" (alpha check)
    blink        20 s  blink once at each BLINK cue (every 2.5 s) -> cue windows "blink"
    jaw          21 s  clench at each CLENCH cue (every 3 s)      -> cue windows "jaw"
    head         21 s  small head turn at each TURN cue (3 s)     -> cue windows "motion"
    talk         20 s  talk normally                              -> "talk" (muscle)
    play         60 s  play / move the mouse as in the game       -> evaluation only

Cue windows start at the cue and last CUE_WINDOW_S; the rest of a cued phase
is unlabelled. Run it twice per person to compare the bridge with and
without OSCAR (--oscar only labels the file; OSCAR itself is switched on
in the bridge: make headset ARGS=--oscar).

Read back with load_protocol(); analysed by eeg.replay and eeg.artifact_model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SESSIONS = Path(__file__).resolve().parent.parent / "sessions"

# name, seconds, instruction, (cue label, cue period s) or None
PHASES = (
    ("still", 40, "Sit still, eyes OPEN, look at the screen, relax your face", None),
    ("eyes_closed", 20, "Close your EYES and relax", None),
    ("blink", 20, "Blink ONCE at each BLINK cue, otherwise keep your eyes open", ("blink", 2.5)),
    ("jaw", 21, "Clench your JAW for one second at each CLENCH cue", ("jaw", 3.0)),
    ("head", 21, "Turn your HEAD a little left or right at each TURN cue, then back", ("motion", 3.0)),
    ("talk", 20, "TALK normally: count aloud or describe the room", None),
    ("play", 60, "PLAY as in the game: move the mouse, look around, aim", None),
)
CUE_TEXT = {"blink": "BLINK", "jaw": "CLENCH", "motion": "TURN"}
CUE_WINDOW_S = {"blink": 1.2, "jaw": 1.5, "motion": 2.0}
CUE_LEAD_S = 1.0  # first cue this long after the phase starts
FAST_SCALE = 0.25  # --fast: phase lengths x this


@dataclass
class Protocol:
    eeg: np.ndarray  # (n, 8) uV
    gyro: np.ndarray | None  # (n, 3) deg/s
    fs: float
    phases: list[tuple[str, int, int]]  # (name, start, end) sample indices
    cues: list[tuple[int, str]]  # (sample index, label)
    meta: dict
    path: Path | None = None

    @property
    def seconds(self) -> float:
        return len(self.eeg) / self.fs

    def phase_at(self, i: int) -> str:
        for name, a, b in self.phases:
            if a <= i < b:
                return name
        return "-"


def save_protocol(p: Protocol, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        eeg=p.eeg.astype(np.float32),
        gyro=np.zeros((len(p.eeg), 0), np.float32) if p.gyro is None else p.gyro.astype(np.float32),
        fs=np.float64(p.fs),
        phase_names=np.array([n for n, _, _ in p.phases]),
        phase_bounds=np.array([[a, b] for _, a, b in p.phases], dtype=np.int64).reshape(-1, 2),
        cue_idx=np.array([i for i, _ in p.cues], dtype=np.int64),
        cue_label=np.array([lab for _, lab in p.cues]),
        meta=np.array(json.dumps(p.meta)),
    )
    return path


def load_protocol(path: str | Path) -> Protocol:
    d = np.load(path, allow_pickle=False)
    gyro = d["gyro"].astype(float)
    return Protocol(
        eeg=d["eeg"].astype(float),
        gyro=gyro if gyro.shape[1] else None,
        fs=float(d["fs"]),
        phases=[(str(n), int(a), int(b)) for n, (a, b) in zip(d["phase_names"], d["phase_bounds"])],
        cues=[(int(i), str(lab)) for i, lab in zip(d["cue_idx"], d["cue_label"])],
        meta=json.loads(str(d["meta"])),
        path=Path(path),
    )


def _simulate(gen, phase: str, cue: str | None, rng: np.random.Generator) -> None:
    """Synthetic stream only: make the generator do what the player is asked."""
    gen.eyes_closed = phase == "eyes_closed"
    if cue == "blink":
        gen.blink()
    elif cue == "jaw":
        gen.jaw(duration_s=1.0)
    elif cue == "motion":
        gen.motion()
    elif phase == "talk" and rng.random() < 0.5:
        gen.jaw(amplitude_uv=20.0, duration_s=0.5)
    elif phase == "play" and rng.random() < 0.15:
        rng.choice([gen.blink, lambda: gen.motion(deg_s=40.0, duration_s=1.0)])()


def run_protocol(stream, player: str, oscar: bool = False, scale: float = 1.0,
                 say=print, simulate: bool = True, seed: int = 0) -> Protocol:
    """Run the phases on a started EEGStream (paced by its samples)."""
    fs = float(stream.fs)
    gen = getattr(stream, "generator", None) if simulate else None
    rng = np.random.default_rng(seed)
    speed = max(getattr(stream, "speed", 1.0), 1.0)
    if not stream.wait_for_samples(int(fs), timeout=10 / speed + 5):
        raise RuntimeError("no EEG data arriving; is the stream running?")
    say(f"[protocol] {player}: {sum(s for _, s, _, _ in PHASES) * scale:.0f} s, follow the prompts")
    start = stream.start_recording()
    phases, cues = [], []
    for name, seconds, text, cue in PHASES:
        dur = seconds * scale
        a = stream.total_samples - start
        say(f"[protocol] {name.upper()}: {text} ({dur:.0f} s)")
        next_cue = CUE_LEAD_S if cue else None
        tick, t = 0.25, 0.0
        while t < dur - 1e-9:
            if next_cue is not None and t >= next_cue and next_cue < dur - CUE_WINDOW_S[cue[0]]:
                cues.append((stream.total_samples - start, cue[0]))
                say(f"[protocol]    {CUE_TEXT[cue[0]]}")
                if gen is not None:
                    _simulate(gen, name, cue[0], rng)
                next_cue += cue[1]
            elif gen is not None and abs(t - round(t)) < 1e-9:
                _simulate(gen, name, None, rng)
            t += tick
            if not stream.wait_for_samples(start + a + int(round(t * fs)), timeout=tick * 8 + 5):
                raise RuntimeError("EEG data stopped during the protocol")
        phases.append((name, a, stream.total_samples - start))
    if gen is not None:
        gen.eyes_closed = False
    eeg, gyro = stream.stop_recording()
    n = len(eeg)
    phases = [(nm, min(a, n), min(b, n)) for nm, a, b in phases]
    say("[protocol] done, thank you")
    meta = {"player": player, "oscar": bool(oscar), "source": getattr(stream, "source", "?"),
            "stream": getattr(getattr(stream, "info", None), "name", lambda: None)(),
            "created": time.time(), "scale": scale}
    return Protocol(eeg, gyro, fs, phases, cues, meta)


def default_path(player: str, oscar: bool) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", player)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return SESSIONS / f"artifacts-{safe}{'-oscar' if oscar else ''}-{stamp}.npz"


def main(argv: list[str] | None = None) -> Path:
    from eeg.stream import EEGStream

    ap = argparse.ArgumentParser(description="Labelled artifact recording (blinks, jaw, head, ...)")
    ap.add_argument("--player", required=True)
    ap.add_argument("--source", choices=("lsl", "synthetic"), default="lsl")
    ap.add_argument("--name", default=os.environ.get("BRAINCRAFT_LSL_NAME"), help="LSL stream name")
    ap.add_argument("--oscar", action="store_true", help="label: the bridge ran with --oscar")
    ap.add_argument("--fast", action="store_true", help=f"phases x{FAST_SCALE} (testing)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    speed = 10.0 if args.source == "synthetic" and args.fast else 1.0
    stream = EEGStream(args.source, name=args.name, speed=speed, seconds=10).start()
    try:
        p = run_protocol(stream, args.player, args.oscar, FAST_SCALE if args.fast else 1.0)
    finally:
        stream.stop()
    out = save_protocol(p, args.out or default_path(args.player, args.oscar))
    print(f"saved {out} ({p.seconds:.0f} s, {len(p.cues)} cues)")
    print(f"check it: poetry run python -m eeg.replay {out}")
    return out


if __name__ == "__main__":
    main()
