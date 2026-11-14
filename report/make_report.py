"""Training session report: one self-contained HTML file per session.

    python -m report.make_report sessions/<session>.csv [--state X] [--out Y] [--open]

Inputs are the logs written by ``log.events.EventLogger``:
  - events CSV ``sessions/<stamp>-<name>.csv`` (columns: contracts.EVENT_FIELDS)
  - state CSV ``sessions/<stamp>-<name>-state.csv`` (contracts.STATE_FIELDS),
    optional: without it the report still builds, just without EEG data.

Panels:
  1. z-scores (engagement, fatigue, lower alpha) with rest phases shaded
  2. signal quality: usable EEG share, artifacts per minute, advice
  3. hits / misses per side: cumulative counts + hit rate per minute
  4. left eccentricity over time (staircase, from left spawns)
  5. yaw histogram (scanning bias: how far left the player looked)
plus summary numbers at the top and the director decision log.

The HTML embeds plotly.js inline, so it opens offline (~4 MB).
This is a training session report, not a diagnostic instrument.
"""

from __future__ import annotations

import argparse
import html
import math
import time
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

Z_KEYS = [
    ("engagement_z", "Engagement (z)"),
    ("fatigue_z", "Fatigue (z)"),
    ("alpha_low_z", "Lower alpha 8-10 Hz (z)"),
]

# Colors (validated categorical slots 1 and 2 for sides; neutral ink otherwise).
COL_L = "#2a78d6"
COL_R = "#eb6834"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
REST_FILL = "rgba(137,135,129,0.18)"

PANEL_TITLES = {
    "z": "1. Brain state (z-scores vs. own baseline)",
    "quality": "2. Signal quality",
    "hits": "3. Hits and misses per side",
    "ecc": "4. Left target eccentricity (adaptive staircase)",
    "yaw": "5. Head direction (yaw) histogram - scanning bias",
    "decisions": "Director decisions",
}

# Signal-quality categories, one per brain-state second (state CSV):
#   usable  quality "ok": fresh z-scores
#   held    quality "bad" without an artifact this second: an artifact is
#           still inside the 4 s window, or no data / calibrating
#   blink / jaw / motion  artifact detected this second
QUALITY_CATS = [
    ("usable", "usable", "#3d9a5b"),
    ("held", "held (recent artifact, no data or calibrating)", "#c3c2b7"),
    ("blink", "blink", "#7c5cc4"),
    ("jaw", "jaw / muscle", "#d4a017"),
    ("motion", "head motion", "#d03b3b"),
]
USABLE_GOOD, USABLE_FAIR = 0.7, 0.4

NO_EEG_TEXT = "No EEG data recorded for this session"


# --- loading ---------------------------------------------------------------


def _load_events(path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(
            path,
            dtype={"target_id": str, "side": str, "value": str, "reason": str, "event": str},
            keep_default_na=False,
        )
    except pd.errors.EmptyDataError:
        df = pd.DataFrame()
    for col in ("t", "event", "target_id", "side", "ecc_deg", "rt_ms", "value", "reason"):
        if col not in df.columns:
            df[col] = ""
    df["t"] = pd.to_numeric(df["t"], errors="coerce")
    df["ecc_deg"] = pd.to_numeric(df["ecc_deg"], errors="coerce")
    df["rt_ms"] = pd.to_numeric(df["rt_ms"], errors="coerce")
    df["side"] = df["side"].astype(str).str.strip().str.upper()
    df["event"] = df["event"].astype(str).str.strip()
    df = df.dropna(subset=["t"]).sort_values("t", kind="stable").reset_index(drop=True)
    return df


def _load_state(path: Path | None) -> pd.DataFrame | None:
    if path is None or not Path(path).exists():
        return None
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return None
    if "t" not in df.columns:
        return None
    df["t"] = pd.to_numeric(df["t"], errors="coerce")
    for key, _ in Z_KEYS:
        df[key] = pd.to_numeric(df[key], errors="coerce") if key in df.columns else np.nan
    df = df.dropna(subset=["t"]).sort_values("t").reset_index(drop=True)
    return df if len(df) else None


def default_state_path(events_csv: Path) -> Path:
    return events_csv.with_name(events_csv.stem + "-state.csv")


# --- analysis --------------------------------------------------------------


def _rest_intervals(ev: pd.DataFrame, t_end: float) -> list[tuple[float, float]]:
    """Pair rest_start with the next rest_end (open rest runs to t_end)."""
    out: list[tuple[float, float]] = []
    start = None
    for _, row in ev[ev["event"].isin(["rest_start", "rest_end"])].iterrows():
        if row["event"] == "rest_start" and start is None:
            start = row["t"]
        elif row["event"] == "rest_end" and start is not None:
            out.append((start, row["t"]))
            start = None
    if start is not None:
        out.append((start, max(start, t_end)))
    return out


def _rate(hits: int, misses: int) -> float | None:
    n = hits + misses
    return hits / n if n else None


def _asym(rate_l: float | None, rate_r: float | None) -> float | None:
    if rate_l is None or rate_r is None or rate_l + rate_r == 0:
        return None
    return (rate_r - rate_l) / (rate_r + rate_l)


def _median(s: pd.Series) -> float | None:
    s = s.dropna()
    return float(s.median()) if len(s) else None


def _yaw_left_deg(yaw: pd.Series) -> pd.Series:
    """Minecraft yaw -> degrees from straight ahead, negative = left.

    Straight ahead is yaw 180 (north); 90 is left (west), 270 / -90 right.
    """
    return ((yaw - 180.0 + 180.0) % 360.0) - 180.0


def summarize(ev: pd.DataFrame, t0: float, t_end: float) -> dict:
    trials = ev[ev["event"].isin(["hit", "miss"])]
    side_stats = {}
    for side in ("L", "R"):
        sub = trials[trials["side"] == side]
        hits = int((sub["event"] == "hit").sum())
        misses = int((sub["event"] == "miss").sum())
        side_stats[side] = {
            "hits": hits,
            "misses": misses,
            "rate": _rate(hits, misses),
            "rt": _median(ev[(ev["event"] == "hit") & (ev["side"] == side)]["rt_ms"]),
            "found_rt": _median(ev[(ev["event"] == "found") & (ev["side"] == side)]["rt_ms"]),
        }

    duration = max(0.0, t_end - t0)
    thirds = {}
    for name, lo, hi in (("first", 0.0, 1 / 3), ("last", 2 / 3, 1.0 + 1e-9)):
        if duration <= 0:
            thirds[name] = None
            continue
        rel = (trials["t"] - t0) / duration
        part = trials[(rel >= lo) & (rel < hi)] if name == "first" else trials[rel >= lo]
        rates = {}
        for side in ("L", "R"):
            s = part[part["side"] == side]
            rates[side] = _rate(int((s["event"] == "hit").sum()), int((s["event"] == "miss").sum()))
        thirds[name] = _asym(rates["L"], rates["R"])

    left_spawns = ev[(ev["event"] == "spawn") & (ev["side"] == "L")]["ecc_deg"].dropna()
    yaw = pd.to_numeric(ev[ev["event"] == "yaw"]["value"], errors="coerce").dropna()
    yaw_rel = _yaw_left_deg(yaw)
    return {
        "duration_s": duration,
        "spawns": int((ev["event"] == "spawn").sum()),
        "trials": int(len(trials)),
        "false_tags": int((ev["event"] == "false_tag").sum()),
        "L": side_stats["L"],
        "R": side_stats["R"],
        "ai_first": thirds["first"],
        "ai_last": thirds["last"],
        "max_left_ecc": float(left_spawns.max()) if len(left_spawns) else None,
        "rest_phases": int((ev["event"] == "rest_start").sum()),
        "decisions": int((ev["event"] == "decision").sum()),
        "yaw_left_share": float((yaw_rel < 0).mean()) if len(yaw_rel) else None,
        "yaw_left_p95": float(-yaw_rel.quantile(0.05)) if len(yaw_rel) else None,
    }


# --- figures ---------------------------------------------------------------


def _layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=60, r=20, t=40, b=50),
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", size=13, color=INK_2),
        paper_bgcolor="#fcfcfb",
        plot_bgcolor="#fcfcfb",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=GRID, linecolor="#c3c2b7", zeroline=False)
    fig.update_yaxes(gridcolor=GRID, linecolor="#c3c2b7", zeroline=False)
    return fig


def _shade_rests(fig: go.Figure, rests_min: list[tuple[float, float]], rows: int = 1) -> None:
    for a, b in rests_min:
        if rows == 1 and fig._grid_ref is None:
            fig.add_vrect(x0=a, x1=b, fillcolor=REST_FILL, line_width=0, layer="below")
            continue
        for r in range(1, rows + 1):
            fig.add_vrect(
                x0=a, x1=b, fillcolor=REST_FILL, line_width=0, layer="below", row=r, col=1
            )


def _empty_fig(text: str, height: int = 220) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=text, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
        font=dict(size=16, color=MUTED),
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _layout(fig, height)


def _fig_z(st: pd.DataFrame | None, t0: float, rests_min) -> go.Figure:
    if st is None or st[[k for k, _ in Z_KEYS]].dropna(how="all").empty:
        return _empty_fig(NO_EEG_TEXT)
    x = (st["t"] - t0) / 60.0
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        subplot_titles=[label for _, label in Z_KEYS],
    )
    for i, (key, label) in enumerate(Z_KEYS, start=1):
        fig.add_trace(
            go.Scatter(
                x=x, y=st[key], mode="lines", name=label, line=dict(color=INK_2, width=2),
                hovertemplate="%{y:.2f}<extra>" + label + "</extra>", showlegend=False,
            ),
            row=i, col=1,
        )
        fig.add_hline(y=0, line=dict(color="#c3c2b7", width=1), row=i, col=1)
    _shade_rests(fig, rests_min, rows=3)
    fig.update_xaxes(title_text="session time (min)", row=3, col=1)
    return _layout(fig, 560)


def _fig_hits(ev: pd.DataFrame, t0: float, t_end: float, rests_min) -> go.Figure:
    trials = ev[ev["event"].isin(["hit", "miss"]) & ev["side"].isin(["L", "R"])]
    if trials.empty:
        return _empty_fig("No hits or misses recorded")
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.1,
        subplot_titles=["Cumulative count", "Hit rate per 1-minute bin"],
    )
    names = {"L": "left", "R": "right"}
    colors = {"L": COL_L, "R": COL_R}
    plural = {"hit": "hits", "miss": "misses"}
    for side in ("L", "R"):
        for kind, dash in (("hit", "solid"), ("miss", "dot")):
            sub = trials[(trials["side"] == side) & (trials["event"] == kind)]
            x = np.concatenate([[0.0], (sub["t"].to_numpy() - t0) / 60.0, [(t_end - t0) / 60.0]])
            y = np.concatenate([[0], np.arange(1, len(sub) + 1), [len(sub)]])
            fig.add_trace(
                go.Scatter(
                    x=x, y=y, mode="lines", line_shape="hv",
                    name=f"{plural[kind]} {names[side]}",
                    line=dict(color=colors[side], width=2, dash=dash),
                    hovertemplate="%{y}<extra>" + f"{plural[kind]} {names[side]}" + "</extra>",
                ),
                row=1, col=1,
            )
    minute = ((trials["t"] - t0) // 60).astype(int)
    for side in ("L", "R"):
        sub = trials[trials["side"] == side]
        if sub.empty:
            continue
        g = sub.assign(m=minute[sub.index]).groupby("m")["event"]
        rate = g.apply(lambda s: (s == "hit").mean())
        n = g.size()
        fig.add_trace(
            go.Scatter(
                x=rate.index + 0.5, y=rate.values * 100, mode="lines+markers",
                name=f"hit rate {names[side]}", marker=dict(size=8, color=colors[side]),
                line=dict(color=colors[side], width=2), customdata=n.values,
                hovertemplate="%{y:.0f}% (n=%{customdata})<extra>"
                + f"hit rate {names[side]}" + "</extra>",
            ),
            row=2, col=1,
        )
    fig.update_yaxes(title_text="count", rangemode="tozero", row=1, col=1)
    fig.update_yaxes(title_text="hit rate (%)", range=[-5, 105], row=2, col=1)
    fig.update_xaxes(title_text="session time (min)", row=2, col=1)
    _shade_rests(fig, rests_min, rows=2)
    fig = _layout(fig, 580)
    fig.update_layout(margin=dict(t=80), legend=dict(y=1.08))
    return fig


def _fig_ecc(ev: pd.DataFrame, t0: float, rests_min) -> go.Figure:
    spawns = ev[(ev["event"] == "spawn") & (ev["side"] == "L")].dropna(subset=["ecc_deg"])
    if spawns.empty:
        return _empty_fig("No left targets spawned")
    outcome = (
        ev[ev["event"].isin(["hit", "miss"]) & (ev["target_id"] != "")]
        .drop_duplicates("target_id", keep="first")
        .set_index("target_id")["event"]
    )
    res = spawns["target_id"].map(outcome.to_dict()).astype(object)
    res = res.where(res.notna(), "open")
    x = (spawns["t"] - t0) / 60.0
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x, y=spawns["ecc_deg"], mode="lines", line_shape="hv", name="left eccentricity",
            line=dict(color=COL_L, width=2), hoverinfo="skip", showlegend=False,
        )
    )
    for kind, symbol, label in (
        ("hit", "circle", "left target hit"),
        ("miss", "x-thin-open", "left target missed"),
        ("open", "circle-open", "no outcome logged"),
    ):
        m = res == kind
        if not m.any():
            continue
        fig.add_trace(
            go.Scatter(
                x=x[m], y=spawns["ecc_deg"][m], mode="markers", name=label,
                marker=dict(
                    symbol=symbol, size=9, color=COL_L if kind != "miss" else INK,
                    line=dict(width=2, color=COL_L if kind != "miss" else INK),
                ),
                hovertemplate="%{y:.0f} deg<extra>" + label + "</extra>",
            )
        )
    _shade_rests(fig, rests_min)
    fig.update_yaxes(title_text="degrees left of straight ahead", rangemode="tozero")
    fig.update_xaxes(title_text="session time (min)")
    fig = _layout(fig, 380)
    fig.update_layout(hovermode="closest")
    return fig


def _fig_yaw(ev: pd.DataFrame) -> go.Figure:
    yaw = pd.to_numeric(ev[ev["event"] == "yaw"]["value"], errors="coerce").dropna()
    if yaw.empty:
        return _empty_fig("No head-direction (yaw) samples recorded")
    rel = _yaw_left_deg(yaw)
    lim = float(min(180.0, max(60.0, math.ceil(rel.abs().max() / 10.0) * 10.0)))
    fig = go.Figure()
    fig.add_trace(
        go.Histogram(
            x=rel, xbins=dict(start=-lim, end=lim, size=5), histnorm="percent",
            marker=dict(color=INK_2, line=dict(color="#fcfcfb", width=2)),
            name="yaw", hovertemplate="%{x} deg: %{y:.1f}% of samples<extra></extra>",
            showlegend=False,
        )
    )
    fig.add_vline(x=0, line=dict(color=MUTED, width=1, dash="dot"))
    fig.add_annotation(x=-lim, y=1.0, yref="paper", text="<- LEFT", showarrow=False,
                       xanchor="left", font=dict(color=COL_L, size=13))
    fig.add_annotation(x=lim, y=1.0, yref="paper", text="RIGHT ->", showarrow=False,
                       xanchor="right", font=dict(color=COL_R, size=13))
    fig.update_xaxes(title_text="head direction, degrees from straight ahead (negative = left)",
                     range=[-lim, lim])
    fig.update_yaxes(title_text="% of samples")
    fig = _layout(fig, 340)
    fig.update_layout(hovermode="closest", bargap=0)
    return fig


# --- HTML ------------------------------------------------------------------


def signal_quality(st: pd.DataFrame | None) -> dict | None:
    """Per-second quality categories and summary numbers (None without EEG)."""
    if st is None or "quality" not in st.columns or st.empty:
        return None
    q = st["quality"].astype(str).str.strip().str.lower()
    art = (st["artifact"].astype(str).str.strip().str.lower()
           if "artifact" in st.columns else pd.Series("none", index=st.index))
    cat = np.where(q == "ok", "usable",
                   np.where(art.isin(["blink", "jaw", "motion"]), art, "held"))
    cat = pd.Series(cat, index=st.index)
    dt = float(np.median(np.diff(st["t"]))) if len(st) > 1 else 1.0
    dt = dt if math.isfinite(dt) and dt > 0 else 1.0
    longest = run = 0
    for c in cat:
        run = 0 if c == "usable" else run + 1
        longest = max(longest, run)
    counts = cat.value_counts().to_dict()
    n = len(cat)
    return {
        "cat": cat,
        "n": n,
        "seconds": n * dt,
        "usable_share": counts.get("usable", 0) / n,
        "counts": {k: int(counts.get(k, 0)) for k, _, _ in QUALITY_CATS},
        "longest_bad_s": longest * dt,
        "dt": dt,
    }


def _fig_quality(st: pd.DataFrame | None, sq: dict | None, t0: float, rests_min) -> go.Figure:
    if sq is None:
        return _empty_fig(NO_EEG_TEXT)
    minute = ((st["t"] - t0) // 60).astype(int)
    table = pd.crosstab(minute, sq["cat"])
    fig = go.Figure()
    for key, label, color in QUALITY_CATS:
        if key not in table.columns:
            continue
        share = table[key] / table.sum(axis=1) * 100
        fig.add_trace(go.Bar(
            x=table.index + 0.5, y=share, name=label, marker_color=color,
            hovertemplate="%{y:.0f}%<extra>" + label + "</extra>",
        ))
    fig.update_layout(barmode="stack", bargap=0.15)
    fig.update_yaxes(title_text="% of seconds", range=[0, 100])
    fig.update_xaxes(title_text="session time (min)")
    _shade_rests(fig, rests_min)
    return _layout(fig, 320)


def _quality_html(sq: dict | None) -> str:
    if sq is None:
        return f'<p class="muted">{NO_EEG_TEXT}.</p>'
    share, c = sq["usable_share"], sq["counts"]
    if share >= USABLE_GOOD:
        verdict = "Good: the director had fresh brain data most of the time."
    elif share >= USABLE_FAIR:
        verdict = ("Fair: brain-driven decisions were often based on held values. "
                   "Interpret the z-scores with care.")
    else:
        verdict = ("Poor: most seconds were unusable, so the brain-driven part of the "
                   "session (rest phases, levels) mostly ran on stale values.")
    artifacts = {k: c[k] for k in ("motion", "jaw", "blink")}
    worst = max(artifacts, key=artifacts.get)
    advice = {
        "motion": "Most artifacts were head motion: keep the head still and move only the mouse; "
                  "make sure the headset sits firmly.",
        "jaw": "Most artifacts were jaw / muscle activity: relax the jaw and face, no chewing or "
               "talking; check the electrodes near the temples (C3/C4).",
        "blink": "Most artifacts were blinks: normal in small numbers; many blinks can mean "
                 "dry eyes or tiredness.",
    }[worst] if artifacts[worst] else "No artifacts were detected."
    tiles = [
        _tile("Usable EEG", _fmt_pct(share), f'{c["usable"]} of {sq["n"]} seconds'),
        _tile("Head motion", f'{c["motion"]} s', "gyroscope above threshold"),
        _tile("Jaw / muscle", f'{c["jaw"]} s', "20-120 Hz power burst"),
        _tile("Blinks", f'{c["blink"]} s', "large frontal deflection"),
        _tile("Held seconds", f'{c["held"]} s',
              "artifact still in the 4 s window, no data, or calibrating"),
        _tile("Longest unusable stretch", _fmt_dur(sq["longest_bad_s"])),
    ]
    return (
        '<div class="tiles">' + "".join(tiles) + "</div>"
        f'<p><b>{verdict}</b> {advice}</p>'
        '<p class="muted">While the signal is not usable the z-scores are held at their last '
        "good values, so the director does not react to noise. Live check during a session: "
        "the HUD's raw EEG scope (amber channels need attention). Shaded = rest phase.</p>"
    )


def _fmt_pct(v: float | None) -> str:
    return "&mdash;" if v is None else f"{v * 100:.0f}%"


def _fmt_ms(v: float | None) -> str:
    return "&mdash;" if v is None else f"{v / 1000:.2f} s"


def _fmt_ai(v: float | None) -> str:
    return "&mdash;" if v is None else f"{v:+.2f}"


def _fmt_dur(s: float) -> str:
    m, sec = divmod(int(round(s)), 60)
    return f"{m}:{sec:02d} min"


def _tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="note">{note}</div>' if note else ""
    return f'<div class="tile"><div class="label">{label}</div><div class="value">{value}</div>{note_html}</div>'


def _summary_html(s: dict) -> str:
    L, R = s["L"], s["R"]
    tiles = [
        _tile("Session length", _fmt_dur(s["duration_s"])),
        _tile("Trials (hit + miss)", str(s["trials"]),
              f'{s["spawns"]} targets spawned' + (f', {s["false_tags"]} false tags' if s["false_tags"] else "")),
        _tile("Hit rate left", _fmt_pct(L["rate"]), f'{L["hits"]} hits / {L["misses"]} misses'),
        _tile("Hit rate right", _fmt_pct(R["rate"]), f'{R["hits"]} hits / {R["misses"]} misses'),
        _tile("Median time to hit L / R", f'{_fmt_ms(L["rt"])} / {_fmt_ms(R["rt"])}'),
        _tile("Median search time L / R", f'{_fmt_ms(L["found_rt"])} / {_fmt_ms(R["found_rt"])}',
              "spawn &rarr; first looked at"),
        _tile("Asymmetry index first &rarr; last third",
              f'{_fmt_ai(s["ai_first"])} &rarr; {_fmt_ai(s["ai_last"])}',
              "(R&minus;L)/(R+L) of hit rates; 0 = balanced, &gt;0 = left weaker"),
        _tile("Max left eccentricity",
              "&mdash;" if s["max_left_ecc"] is None else f'{s["max_left_ecc"]:.0f}&deg;'),
        _tile("Rest phases", str(s["rest_phases"]), f'{s["decisions"]} director decisions'),
        _tile("Usable EEG", _fmt_pct(s.get("usable_share")), "see 2. Signal quality"),
        _tile("Time looking left",
              _fmt_pct(s["yaw_left_share"]),
              "" if s["yaw_left_p95"] is None else f'reached {s["yaw_left_p95"]:.0f}&deg; left (95th pct)'),
    ]
    return '<div class="tiles">' + "".join(tiles) + "</div>"


def _decisions_html(ev: pd.DataFrame, t0: float) -> str:
    dec = ev[ev["event"] == "decision"]
    if dec.empty:
        return '<p class="muted">No director decisions logged.</p>'
    rows = []
    for _, r in dec.iterrows():
        rows.append(
            f"<tr><td>{_fmt_dur(r['t'] - t0).replace(' min', '')}</td>"
            f"<td>{html.escape(str(r['value']))}</td><td>{html.escape(str(r['reason']))}</td></tr>"
        )
    return (
        '<table class="dec"><thead><tr><th>time</th><th>change</th><th>reason</th></tr></thead>'
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


CSS = """
:root { --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
        --line:#e1e0d9; --left:#2a78d6; --right:#eb6834; }
* { box-sizing: border-box; }
body { margin:0; background:var(--page); color:var(--ink);
       font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 32px 0 8px; }
.sub { color: var(--ink2); margin: 0 0 20px; }
.muted { color: var(--muted); }
.tiles { display:grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.tile .label { font-size: 12px; color: var(--ink2); text-transform: uppercase; letter-spacing: .03em; }
.tile .value { font-size: 24px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
.tile .note { font-size: 12px; color: var(--muted); margin-top: 2px; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 8px; }
table.dec { width:100%; border-collapse: collapse; background: var(--surface); font-size: 14px; }
table.dec th, table.dec td { text-align:left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
table.dec td:first-child { font-variant-numeric: tabular-nums; color: var(--ink2); white-space: nowrap; }
.legend-sides span { display:inline-block; margin-right: 16px; }
.sw { display:inline-block; width:12px; height:12px; border-radius:3px; vertical-align:-1px; margin-right:6px; }
footer { margin-top: 40px; font-size: 12px; color: var(--muted); }
"""


def make_report(
    events_csv: Path, state_csv: Path | None = None, out: Path | None = None
) -> Path:
    """Build the HTML training session report and return its path."""
    events_csv = Path(events_csv)
    if state_csv is None:
        cand = default_state_path(events_csv)
        state_csv = cand if cand.exists() else None
    out = Path(out) if out is not None else events_csv.with_name(events_csv.stem + "-report.html")

    ev = _load_events(events_csv)
    st = _load_state(state_csv)

    times = [ev["t"].min(), ev["t"].max()] if len(ev) else []
    if st is not None:
        times += [st["t"].min(), st["t"].max()]
    times = [t for t in times if t is not None and not pd.isna(t)]
    t0 = float(min(times)) if times else 0.0
    t_end = float(max(times)) if times else 0.0

    rests = _rest_intervals(ev, t_end)
    rests_min = [((a - t0) / 60.0, (b - t0) / 60.0) for a, b in rests]
    summary = summarize(ev, t0, t_end)
    sq = signal_quality(st)
    summary["usable_share"] = sq["usable_share"] if sq else None

    figs = [
        ("z", _fig_z(st, t0, rests_min)),
        ("quality", _fig_quality(st, sq, t0, rests_min)),
        ("hits", _fig_hits(ev, t0, t_end, rests_min)),
        ("ecc", _fig_ecc(ev, t0, rests_min)),
        ("yaw", _fig_yaw(ev)),
    ]
    config = {"displaylogo": False, "responsive": True}
    panels = []
    for i, (key, fig) in enumerate(figs):
        div = fig.to_html(full_html=False, include_plotlyjs=(i == 0), config=config)
        extra = '<p class="muted">Shaded = rest phase.</p>' if key in ("z", "ecc") else ""
        if key == "quality":
            extra = _quality_html(sq)
        if key == "hits":
            extra = (
                '<p class="legend-sides muted"><span><i class="sw" style="background:var(--left)"></i>left</span>'
                '<span><i class="sw" style="background:var(--right)"></i>right</span>'
                "solid = hits, dotted = misses. Shaded = rest phase.</p>"
            )
        panels.append(f'<h2>{PANEL_TITLES[key]}</h2>{extra}<div class="panel">{div}</div>')

    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(t0)) if times else "unknown"
    sources = html.escape(events_csv.name) + (
        f" + {html.escape(Path(state_csv).name)}" if st is not None else " (no brain-state log)"
    )
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BrainCraft session report</title>
<style>{CSS}</style></head>
<body><main>
<h1>BrainCraft &mdash; training session report</h1>
<p class="sub">Session started {started} &middot; source: {sources}</p>
{_summary_html(summary)}
{''.join(panels)}
<h2>{PANEL_TITLES["decisions"]}</h2>
{_decisions_html(ev, t0)}
<footer>Training session report generated by BrainCraft on {time.strftime("%Y-%m-%d %H:%M")}.
Spatial-scanning training summary for the therapist; not a diagnostic assessment.
Times are relative to session start. z-scores are relative to the player's own calibration baseline.</footer>
</main></body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description="Build a BrainCraft training session report (HTML).")
    ap.add_argument("events_csv", type=Path, help="sessions/<session>.csv")
    ap.add_argument("--state", type=Path, default=None, help="brain-state CSV (default: <stem>-state.csv)")
    ap.add_argument("--out", type=Path, default=None, help="output HTML (default: <stem>-report.html)")
    ap.add_argument("--open", action="store_true", help="open the report in the browser")
    args = ap.parse_args(argv)
    path = make_report(args.events_csv, args.state, args.out)
    print(path)
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return path


if __name__ == "__main__":
    main()
