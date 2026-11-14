"""Synthetic Unicorn-like EEG, for developing without the headset.

In-process:   gen = SyntheticEEG(seed=0); chunk = gen.generate(250)
As LSL:       python -m eeg.synthetic --lsl [--name BrainCraftSynthetic]

The signal (microvolts, 250 Hz) per EEG channel is
  1/f background + theta (4-8) + alpha (8.5-11.5) + beta (13-30)
  + 50 Hz mains + DC offset/slow drift + events (blink, jaw, motion).

- alpha is posterior (Oz > PO7/PO8 > Pz > central > Fz) and its
  amplitude x4 (power x16) with eyes closed;
- `engagement` (about -1..+1) raises beta and lowers theta/alpha, so
  beta/(alpha+theta) at Fz/Cz rises and (theta+alpha)/beta falls;
- `fatigue_rate` drifts the effective engagement down (units per minute);
- blink: ~0.3 s slow positive bump, ~200 uV at Fz, much smaller posterior;
- jaw clench: broadband EMG (> 20 Hz) burst on all channels, ~0.75 s;
- motion: gyro burst (deg/s) plus slow wobble on the EEG.

Column layout = the Unicorn LSL layout (eeg/layout.py): 8 EEG, 3 acc,
3 gyro, battery, counter, validation (17 columns) by default.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable

import numpy as np
from scipy import signal

from eeg.layout import (
    BATTERY_COL,
    COUNTER_COL,
    DEFAULT_ACC_COLS,
    DEFAULT_GYRO_COLS,
    EEG_CHANNELS,
    FS,
    N_UNICORN,
    UNICORN_CHANNELS,
    VALIDATION_COL,
)

#                    Fz   C3   Cz   C4   Pz  PO7   Oz  PO8
ALPHA_GAIN = np.array([.15, .30, .30, .30, .70, .90, 1.0, .90])
THETA_GAIN = np.array([1.0, .60, .90, .60, .50, .30, .30, .30])
BETA_GAIN = np.array([1.0, .90, 1.0, .90, .60, .50, .50, .50])
BLINK_GAIN = np.array([1.0, .35, .40, .35, .12, .05, .03, .05])
JAW_GAIN = np.array([.8, 1.0, .7, 1.0, .7, .9, .8, .9])

# Amplitudes (RMS, microvolts) at gain 1.
BACKGROUND_UV = 7.0
THETA_UV = 4.0
ALPHA_UV = 4.0
BETA_UV = 2.5
EYES_CLOSED_ALPHA = 4.0  # amplitude factor
# Engagement e scales amplitudes by exp(k * e).
K_THETA, K_ALPHA, K_BETA = -0.35, -0.30, 0.45

# Paul Kellet's "economy" pinking filter (-3 dB/octave, i.e. 1/f power).
_PINK_B = np.array([0.049922035, -0.095993537, 0.050612699, -0.004408786])
_PINK_A = np.array([1.0, -2.494956002, 2.017265875, -0.522189400])


def _rms_gain_sos(sos: np.ndarray, n: int = 8192) -> float:
    imp = np.zeros(n)
    imp[0] = 1.0
    return float(np.sqrt(np.sum(signal.sosfilt(sos, imp) ** 2)))


def _rms_gain_ba(b: np.ndarray, a: np.ndarray, n: int = 20000) -> float:
    imp = np.zeros(n)
    imp[0] = 1.0
    return float(np.sqrt(np.sum(signal.lfilter(b, a, imp) ** 2)))


class SyntheticEEG:
    """Stateful generator: consecutive generate() calls form one signal.

    Public knobs (safe to change from another thread between chunks):
    eyes_closed, engagement, fatigue_rate. Events: blink(), jaw(), motion().
    """

    def __init__(
        self,
        fs: int = FS,
        n_channels: int = N_UNICORN,
        eeg_cols: tuple[int, ...] = tuple(range(8)),
        gyro_cols: tuple[int, ...] | None = DEFAULT_GYRO_COLS,
        acc_cols: tuple[int, ...] | None = DEFAULT_ACC_COLS,
        seed: int | None = None,
        engagement: float = 0.0,
        eyes_closed: bool = False,
        fatigue_rate: float = 0.0,
        line_noise_uv: float = 4.0,
        dc_offset_uv: float = 300.0,
    ):
        self.fs = fs
        self.n_channels = n_channels
        self.eeg_cols = np.array(eeg_cols)
        has_extra = n_channels >= N_UNICORN
        self.gyro_cols = np.array(gyro_cols) if (gyro_cols and has_extra) else None
        self.acc_cols = np.array(acc_cols) if (acc_cols and has_extra) else None
        self.rng = np.random.default_rng(seed)
        self.engagement = float(engagement)
        self.eyes_closed = bool(eyes_closed)
        self.fatigue_rate = float(fatigue_rate)
        self.line_noise_uv = line_noise_uv
        self.n_eeg = len(eeg_cols)

        nyq = fs / 2
        self._bands = {
            "theta": signal.butter(2, [4 / nyq, 8 / nyq], "bandpass", output="sos"),
            "alpha": signal.butter(2, [8.5 / nyq, 11.5 / nyq], "bandpass", output="sos"),
            "beta": signal.butter(2, [13 / nyq, 30 / nyq], "bandpass", output="sos"),
        }
        self._band_norm = {k: _rms_gain_sos(s) for k, s in self._bands.items()}
        self._band_zi = {
            k: np.zeros((s.shape[0], 2, self.n_eeg)) for k, s in self._bands.items()
        }
        self._pink_norm = _rms_gain_ba(_PINK_B, _PINK_A)
        self._pink_zi = np.zeros((len(_PINK_A) - 1, self.n_eeg))
        self._emg_sos = signal.butter(4, [20 / nyq, min(110, nyq - 5) / nyq], "bandpass", output="sos")

        self._dc = self.rng.uniform(-dc_offset_uv, dc_offset_uv, self.n_eeg)
        self._drift_phase = self.rng.uniform(0, 2 * np.pi, self.n_eeg)
        self._line_phase = self.rng.uniform(0, 2 * np.pi, self.n_eeg)
        self._gyro_bias = self.rng.uniform(-1.0, 1.0, 3)
        self._n = 0  # samples generated so far
        self._events: list[list] = []  # [waveform (m, n_channels), position]
        self._lock = threading.Lock()
        self.generate(2 * fs)  # let the filters settle
        self._n = 0

    # --- knobs -----------------------------------------------------------

    @property
    def elapsed_s(self) -> float:
        return self._n / self.fs

    @property
    def effective_engagement(self) -> float:
        e = self.engagement - self.fatigue_rate * self.elapsed_s / 60.0
        return float(np.clip(e, -2.5, 2.5))

    def state(self) -> dict:
        return {
            "t_s": round(self.elapsed_s, 1),
            "eyes_closed": self.eyes_closed,
            "engagement": round(self.engagement, 2),
            "effective_engagement": round(self.effective_engagement, 2),
            "fatigue_rate": self.fatigue_rate,
        }

    def __str__(self) -> str:
        s = self.state()
        eyes = "CLOSED" if s["eyes_closed"] else "open"
        return (
            f"[synthetic t={s['t_s']:.0f}s] eyes {eyes}, engagement "
            f"{s['engagement']:+.2f} (effective {s['effective_engagement']:+.2f})"
        )

    # --- events ----------------------------------------------------------

    def _add_event(self, wave: np.ndarray) -> None:
        with self._lock:
            self._events.append([wave, 0])

    def blink(self, amplitude_uv: float = 200.0, duration_s: float = 0.3) -> None:
        m = max(int(duration_s * self.fs), 2)
        shape = np.hanning(m) ** 1.5
        wave = np.zeros((m, self.n_channels))
        wave[:, self.eeg_cols] = amplitude_uv * shape[:, None] * BLINK_GAIN[None, :]
        self._add_event(wave)

    def jaw(self, amplitude_uv: float = 40.0, duration_s: float = 0.75) -> None:
        m = int(duration_s * self.fs)
        env = np.ones(m)
        ramp = min(int(0.05 * self.fs), m // 4)
        if ramp:
            env[:ramp] = np.linspace(0, 1, ramp)
            env[-ramp:] = np.linspace(1, 0, ramp)
        noise = self.rng.standard_normal((m + self.fs, self.n_eeg))
        emg = signal.sosfilt(self._emg_sos, noise, axis=0)[self.fs :]
        emg /= emg.std(axis=0, keepdims=True) + 1e-12
        wave = np.zeros((m, self.n_channels))
        wave[:, self.eeg_cols] = amplitude_uv * emg * env[:, None] * JAW_GAIN[None, :]
        self._add_event(wave)

    def motion(self, deg_s: float = 80.0, duration_s: float = 1.5) -> None:
        m = int(duration_s * self.fs)
        t = np.arange(m) / self.fs
        env = np.sin(np.pi * t / duration_s)
        wave = np.zeros((m, self.n_channels))
        if self.gyro_cols is not None:
            axes = self.rng.normal(size=3)
            axes /= np.linalg.norm(axes)
            g = deg_s * env * np.sin(2 * np.pi * 1.2 * t)
            wave[:, self.gyro_cols] = g[:, None] * axes[None, :]
        wobble = 60.0 * env * np.sin(2 * np.pi * 1.2 * t)
        wave[:, self.eeg_cols] = wobble[:, None] * self.rng.uniform(0.5, 1.0, self.n_eeg)
        self._add_event(wave)

    # --- signal ----------------------------------------------------------

    def _band(self, name: str, n: int) -> np.ndarray:
        white = self.rng.standard_normal((n, self.n_eeg))
        out, self._band_zi[name] = signal.sosfilt(
            self._bands[name], white, axis=0, zi=self._band_zi[name]
        )
        return out / self._band_norm[name]

    def generate(self, n: int) -> np.ndarray:
        """Next n samples, shape (n, n_channels), EEG in microvolts."""
        n = int(n)
        out = np.zeros((n, self.n_channels))
        if n <= 0:
            return out
        e = self.effective_engagement
        t = (self._n + np.arange(n)) / self.fs

        white = self.rng.standard_normal((n, self.n_eeg))
        pink, self._pink_zi = signal.lfilter(_PINK_B, _PINK_A, white, axis=0, zi=self._pink_zi)
        eeg = BACKGROUND_UV * pink / self._pink_norm
        alpha_amp = ALPHA_UV * (EYES_CLOSED_ALPHA if self.eyes_closed else 1.0)
        eeg += THETA_UV * np.exp(K_THETA * e) * THETA_GAIN * self._band("theta", n)
        eeg += alpha_amp * np.exp(K_ALPHA * e) * ALPHA_GAIN * self._band("alpha", n)
        eeg += BETA_UV * np.exp(K_BETA * e) * BETA_GAIN * self._band("beta", n)
        eeg += 0.5 * self.rng.standard_normal((n, self.n_eeg))  # amplifier noise
        eeg += self.line_noise_uv * np.sin(2 * np.pi * 50 * t[:, None] + self._line_phase)
        eeg += self._dc + 15.0 * np.sin(2 * np.pi * 0.07 * t[:, None] + self._drift_phase)
        out[:, self.eeg_cols] = eeg

        if self.acc_cols is not None:
            out[:, self.acc_cols] = np.array([0.0, 0.0, 1.0]) + 0.005 * self.rng.standard_normal((n, 3))
        if self.gyro_cols is not None:
            out[:, self.gyro_cols] = self._gyro_bias + 0.3 * self.rng.standard_normal((n, 3))
        if self.n_channels >= N_UNICORN:
            out[:, BATTERY_COL] = 100.0
            out[:, COUNTER_COL] = self._n + np.arange(n) + 1
            out[:, VALIDATION_COL] = 1.0

        with self._lock:
            remaining = []
            for ev in self._events:
                wave, pos = ev
                k = min(n, len(wave) - pos)
                out[:k] += wave[pos : pos + k]
                ev[1] = pos + k
                if ev[1] < len(wave):
                    remaining.append(ev)
            self._events = remaining
        self._n += n
        return out


# --- stdin commands (shared with eeg.check) ------------------------------

COMMANDS_HELP = (
    "commands: e = eyes closed/open, b = blink, j = jaw clench, m = head motion, "
    "f+ = more fatigue (engagement down), f- = less fatigue (engagement up), "
    "s = show state, q = quit"
)


def apply_command(gen: SyntheticEEG, cmd: str) -> str | None:
    """Apply one text command. Returns a message, or None for 'q'."""
    cmd = cmd.strip().lower()
    if cmd == "q":
        return None
    if cmd == "e":
        gen.eyes_closed = not gen.eyes_closed
    elif cmd == "b":
        gen.blink()
    elif cmd == "j":
        gen.jaw()
    elif cmd == "m":
        gen.motion()
    elif cmd == "f+":
        gen.engagement = round(gen.engagement - 0.25, 2)
    elif cmd == "f-":
        gen.engagement = round(gen.engagement + 0.25, 2)
    elif cmd in ("s", ""):
        pass
    else:
        return f"unknown command {cmd!r}; {COMMANDS_HELP}"
    return f"{cmd or 's'} -> {gen}"


def start_command_thread(
    gen: SyntheticEEG, on_quit: Callable[[], None], out: Callable[[str], None] = print
) -> threading.Thread:
    """Read commands from stdin lines in a daemon thread (works on Windows)."""

    def loop() -> None:
        for line in sys.stdin:
            msg = apply_command(gen, line)
            if msg is None:
                on_quit()
                return
            out(msg)
        # stdin closed (e.g. no console): keep running without commands

    th = threading.Thread(target=loop, name="synthetic-commands", daemon=True)
    th.start()
    return th


# --- LSL outlet ----------------------------------------------------------


def make_outlet(name: str, n_channels: int = N_UNICORN, fs: int = FS):
    from pylsl import StreamInfo, StreamOutlet, cf_float32

    info = StreamInfo(name, "EEG", n_channels, fs, cf_float32, f"{name}-synthetic")
    chans = info.desc().append_child("channels")
    labels = UNICORN_CHANNELS if n_channels == N_UNICORN else EEG_CHANNELS[:n_channels]
    for label in labels:
        ch = chans.append_child("channel")
        ch.append_child_value("label", label)
        ch.append_child_value("unit", "microvolts" if label in EEG_CHANNELS else "")
    return StreamOutlet(info, chunk_size=10)


def run_lsl(name: str, seed: int | None = None, fatigue_rate: float = 0.0) -> None:
    gen = SyntheticEEG(seed=seed, fatigue_rate=fatigue_rate)
    outlet = make_outlet(name, gen.n_channels, gen.fs)
    stop = threading.Event()
    print(f"LSL outlet '{name}' (type EEG, {gen.n_channels} ch, {gen.fs} Hz) is live.")
    print(COMMANDS_HELP)
    print(gen)
    start_command_thread(gen, stop.set)
    t0 = time.perf_counter()
    sent = 0
    last_print = t0
    try:
        while not stop.is_set():
            now = time.perf_counter()
            due = int((now - t0) * gen.fs) - sent
            if due > 0:
                chunk = gen.generate(due)
                outlet.push_chunk(chunk.astype(np.float32).tolist())
                sent += due
            if now - last_print >= 10.0:
                print(gen)
                last_print = now
            stop.wait(0.02)
    except KeyboardInterrupt:
        pass
    print("synthetic stream stopped")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Synthetic Unicorn-like EEG stream")
    p.add_argument("--lsl", action="store_true", help="push a real-time LSL outlet")
    p.add_argument("--name", default="BrainCraftSynthetic", help="LSL stream name")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--fatigue-rate", type=float, default=0.0,
                   help="engagement drift down per minute (e.g. 0.2)")
    args = p.parse_args(argv)
    if not args.lsl:
        p.error("nothing to do: pass --lsl (or use EEGStream(source='synthetic') in-process)")
    run_lsl(args.name, seed=args.seed, fatigue_rate=args.fatigue_rate)


if __name__ == "__main__":
    main()
