"""Directors: rehab (warm-up, ALI side bias, staircase, fatigue rest) and fun."""

from __future__ import annotations


def make_director(mode: str, seed: int | None = None):
    """Director for a mode name ("rehab" | "fun")."""
    if mode == "rehab":
        from director.rehab import RehabDirector

        return RehabDirector(seed=seed)
    if mode == "fun":
        from director.fun import FunDirector

        return FunDirector(seed=seed)
    raise ValueError(f"unknown mode {mode!r}")
