"""Unicorn Hybrid Black -> LSL with g.tec's g.Pype (no Unicorn Suite).

A separate process: it only reads the headset and publishes raw samples
as an LSL stream. The state service reads that stream as usual
(BRAINCRAFT_SOURCE=eeg); restart this bridge after a Bluetooth drop
without touching the service, HUD or game.

    poetry install --with gpype                  # once
    poetry run python -m eeg.gpype_bridge        # headset -> LSL + live scope
    poetry run python -m eeg.gpype_bridge --record sessions/raw.csv
    poetry run python -m eeg.gpype_bridge --fake # no headset: g.Pype sine

Stream: name "BrainCraft-Unicorn" (--name), type "EEG", 250 Hz. Columns
follow the Unicorn LSL app layout that eeg/layout.py expects, so no
BRAINCRAFT_EEG_COLS / BRAINCRAFT_GYRO_COLS are needed:

    0-7 EEG (uV), 8-10 accel (g), 11-13 gyro (deg/s),
    14 battery (%), 15 counter, 16 validation [, 17 g.Pype arrival stamp]

macOS: the Unicorn driver needs the main thread's event loop, which
only the window (g.Pype MainApp) provides, so --headless works on
Windows/Linux only. Close the window or press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

DEFAULT_NAME = "BrainCraft-Unicorn"
N_DEVICE_CHANNELS = 17  # 8 EEG + 3 accel + 3 gyro + battery, counter, validation
N_EEG = 8


def _import_gpype():
    try:
        import gpype as gp
    except ImportError:
        sys.exit(
            "g.Pype is not installed. Run:  poetry install --with gpype\n"
            "(wheels exist for Windows x64, macOS arm64 and Linux x86-64)"
        )
    return gp


def build(gp, args) -> tuple:
    """Pipeline: source -> LSLSender (+ CSV recorder, + scope). Returns (pipeline, scope)."""
    p = gp.Pipeline()
    if args.fake:
        # Plumbing test without a headset: 10 Hz sine plus noise on every
        # column. Not EEG-like; use eeg.synthetic for realistic data.
        source = gp.Generator(
            sampling_rate=250, channel_count=N_DEVICE_CHANNELS, signal_frequency=10,
            signal_shape="sine", signal_amplitude=20, noise_amplitude=5,
        )
    else:
        source = gp.HybridBlack(
            serial=args.serial,
            include_accel=True,
            include_gyro=True,
            include_aux=True,
            test_signal=args.test_signal,
            enable_oscar=args.oscar,
        )
    p.connect(source, gp.LSLSender(stream_name=args.name))

    if args.record:
        path = Path(args.record).with_suffix(".csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        # g.Pype appends a timestamp to the file name.
        p.connect(source, gp.CsvWriter(file_name=str(path)))

    scope = None
    if not args.headless and not args.no_scope:
        eeg = gp.ChannelSelector(indices=list(range(N_EEG)))
        view = gp.Bandpass(f_lo=1, f_hi=30)  # display only; LSL gets raw data
        scope = gp.TimeSeriesScope(amplitude_limit=100, time_window=10)
        p.connect(source, eeg)
        p.connect(eeg, view)
        p.connect(view, scope)
    return p, scope


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m eeg.gpype_bridge",
        description="Stream the Unicorn Hybrid Black to LSL with g.Pype.",
    )
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"LSL stream name (default {DEFAULT_NAME})")
    ap.add_argument("--serial", default=None, help="Unicorn serial, e.g. UN-2023.05.12 (default: first paired)")
    ap.add_argument("--record", metavar="FILE", help="also write raw samples to FILE.csv (timestamp appended)")
    ap.add_argument("--oscar", action="store_true",
                    help="g.tec OSCAR artifact removal; calibrate with the same setting")
    ap.add_argument("--test-signal", action="store_true", help="device test signal instead of electrodes")
    ap.add_argument("--fake", action="store_true", help="no headset: g.Pype sine generator")
    ap.add_argument("--no-scope", action="store_true", help="window without the signal scope")
    ap.add_argument("--headless", action="store_true", help="no window (not on macOS with a headset)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.headless and sys.platform == "darwin" and not args.fake:
        sys.exit("--headless can't read the Unicorn on macOS (the driver needs the window's "
                 "event loop). Run without --headless.")
    gp = _import_gpype()

    app = None if args.headless else gp.MainApp(caption=f"BrainCraft bridge: {args.name}")
    p, scope = build(gp, args)
    if scope is not None:
        app.add_widget(scope)

    src = "g.Pype sine generator" if args.fake else "Unicorn Hybrid Black"
    print(f"{src} -> LSL stream {args.name!r} (type EEG, 250 Hz)")
    if args.name != DEFAULT_NAME:
        print(f"Point the pipeline at it: BRAINCRAFT_LSL_NAME={args.name}")

    p.start()
    try:
        if app is None:
            stop = []
            signal.signal(signal.SIGINT, lambda *_: stop.append(1))
            print("Streaming. Ctrl+C to stop.")
            while not stop:
                time.sleep(0.2)
        else:
            # Python handles Ctrl+C between Qt callbacks (the scope's refresh).
            signal.signal(signal.SIGINT, lambda *_: gp.MainApp.end_run())
            print("Streaming. Close the window or press Ctrl+C to stop.")
            app.run()
    finally:
        p.stop()
        p.close()
        print("bridge stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
