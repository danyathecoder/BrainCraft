"""Session report: one HTML file from a sessions/*.csv events log (P4).

    python -m report.make_report sessions/<session>.csv

Panels:
  1. z-scores timeline with rest phases shaded
  2. hits / misses per side over time
  3. left eccentricity over time (staircase)
  4. yaw histogram + summary numbers: left vs right hit rate and RT,
     asymmetry index (R - L) / (R + L) start vs end, max left
     eccentricity reached, number of rest phases
"""

from __future__ import annotations

import sys
from pathlib import Path


def make_report(events_csv: Path) -> Path:
    # TODO(P4): pandas.read_csv -> plotly subplots -> write_html next to
    # the CSV. Brain-state history needs its own log (TODO with P2).
    raise NotImplementedError


if __name__ == "__main__":
    print(make_report(Path(sys.argv[1])))
