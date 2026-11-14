"""Session event logger: one CSV per session, columns from contracts."""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

from contracts import EVENT_FIELDS, EVENT_TYPES

SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"


class EventLogger:
    """Append-only CSV logger, safe to share between threads.

    >>> log = EventLogger("rehab-anna")
    >>> log.event("spawn", target_id="t1", side="L", ecc_deg=35)
    >>> log.decision("rest phase", reason="fatigue_z > 1.5 for 20 s")
    """

    def __init__(self, name: str | None = None, directory: Path = SESSIONS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = directory / (f"{stamp}-{name}.csv" if name else f"{stamp}.csv")
        self._lock = threading.Lock()
        self._file = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=EVENT_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def event(self, event: str, **fields) -> None:
        if event not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event!r}")
        unknown = set(fields) - set(EVENT_FIELDS)
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        row = {"t": f"{time.time():.3f}", "event": event, **fields}
        with self._lock:
            self._writer.writerow(row)
            self._file.flush()

    def decision(self, value: str, reason: str) -> None:
        """Log a director decision. Every decision needs a reason."""
        self.event("decision", value=value, reason=reason)

    def close(self) -> None:
        with self._lock:
            self._file.close()
