"""Is the signal sane? First milestone: read any signal, then one that makes sense.

    python -m eeg.check [--source lsl|synthetic] [--name STREAM] [--window 2]

Every second prints alpha (8-12 Hz) power per channel and the Fz/Cz
engagement ratio. Close your eyes for ~10 s: alpha at Oz/PO7/PO8 should
jump several times (the 'x' column compares to the first seconds).
With --source synthetic, type commands + Enter: e (eyes), b (blink),
j (jaw), m (motion), f+/f- (fatigue), q (quit).
"""

from __future__ import annotations

import argparse
import os
import threading
import time

import numpy as np

from eeg import features as F
from eeg.layout import CH, EEG_CHANNELS
from eeg.stream import EEGStream, StreamNotFound


def _line(bp: dict, ref_oz: float | None, art: str, raw: np.ndarray) -> str:
    alpha = bp["alpha"]
    cells = " ".join(f"{a:7.1f}" for a in alpha)
    eng = np.mean([bp["beta"][CH[c]] / (bp["alpha"][CH[c]] + bp["theta"][CH[c]]) for c in ("Fz", "Cz")])
    post = F.posterior_alpha(bp)
    rel = f"x{post / ref_oz:4.1f}" if ref_oz else "  ref"
    span = np.ptp(raw, axis=0).max()
    return f"{cells} | {rel} | eng {eng:5.2f} | {art:6} | p2p {span:7.0f} uV"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Print alpha power per channel every second")
    p.add_argument("--source", choices=("lsl", "synthetic"), default="lsl")
    p.add_argument("--name", default=os.environ.get("BRAINCRAFT_LSL_NAME"))
    p.add_argument("--window", type=float, default=2.0, help="analysis window, s")
    p.add_argument("--seconds", type=float, default=0, help="stop after N s (0 = never)")
    args = p.parse_args(argv)

    try:
        stream = EEGStream(args.source, name=args.name).start()
    except StreamNotFound as e:
        print(e)
        return
    stop = threading.Event()
    if stream.generator is not None:
        from eeg.synthetic import COMMANDS_HELP, start_command_thread

        print(COMMANDS_HELP)
        start_command_thread(stream.generator, stop.set)
    if stream.info is not None:
        print(f"stream {stream.info.name()!r}: {stream.info.channel_count()} ch, "
              f"EEG cols {stream.layout.eeg}, gyro cols {stream.layout.gyro}")
    print("alpha 8-12 Hz power (uV^2/Hz); x = PO7/Oz/PO8 alpha vs first 5 s")
    print(" ".join(f"{c:>7}" for c in EEG_CHANNELS) + " |   rel | engagement | artifact")
    detector = F.ArtifactDetector(fs=stream.fs)
    first: list[float] = []
    t0 = time.time()
    try:
        while not stop.wait(1.0):
            if args.seconds and time.time() - t0 > args.seconds:
                break
            eeg, gyro = stream.windows(args.window)
            if len(eeg) < stream.fs:
                print("waiting for data...")
                continue
            if stream.seconds_since_data > 2:
                print(f"no data for {stream.seconds_since_data:.0f} s")
                continue
            try:
                bp = F.band_powers(eeg, stream.fs)
                n1 = int(stream.fs)
                art = detector(eeg[-n1:], gyro[-n1:] if gyro is not None else None)
            except Exception as e:  # keep printing
                print(f"bad window: {e}")
                continue
            if len(first) < 5:
                first.append(F.posterior_alpha(bp))
            ref = float(np.median(first)) if len(first) >= 5 else None
            line = _line(bp, ref, art, eeg)
            if stream.generator is not None:
                line += "  " + ("eyes CLOSED" if stream.generator.eyes_closed else "eyes open")
            print(line, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()


if __name__ == "__main__":
    main()
