"""Directors: rehab (spatial staircase), rehab_01 (hemifield + ALI), fun."""

from __future__ import annotations


def make_director(mode: str, seed: int | None = None):
    """Director for a mode name ("rehab" | "rehab_01" | "fun")."""
    if mode == "rehab":
        from director.rehab import RehabDirector

        return RehabDirector(seed=seed)
    if mode == "rehab_01":
        from director.rehab_01 import Rehab01Director

        return Rehab01Director(seed=seed)
    if mode == "fun":
        from director.fun import FunDirector

        return FunDirector(seed=seed)
    raise ValueError(f"unknown mode {mode!r}")
