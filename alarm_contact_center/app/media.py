"""Copies of Frigate snapshots, kept with each incident under /data/media.

Frigate deletes recordings according to its own retention; the incident keeps its
own copy of each snapshot so the history still shows who was there. Clips are not
copied (they are large): the App asks Frigate to retain those events instead.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class MediaStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def path(self, incident_id: str, event_id: str) -> Path | None:
        """Where a snapshot lives; None for ids that could escape the folder."""
        if not (_SAFE.match(incident_id) and _SAFE.match(event_id)) or ".." in incident_id + event_id:
            return None
        return self.root / incident_id / f"{event_id}.jpg"

    def save(self, incident_id: str, event_id: str, data: bytes) -> bool:
        target = self.path(incident_id, event_id)
        if target is None or not data:
            return False
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return True

    def read(self, incident_id: str, event_id: str) -> bytes | None:
        target = self.path(incident_id, event_id)
        try:
            return target.read_bytes() if target else None
        except OSError:
            return None

    def cleanup(self, days: int, now: float | None = None) -> int:
        """Delete incident folders older than ``days``. Returns how many were removed."""
        now = time.time() if now is None else now
        removed = 0
        if not self.root.is_dir():
            return 0
        with self._lock:
            for folder in self.root.iterdir():
                if folder.is_dir() and folder.stat().st_mtime < now - days * 86400:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed += 1
        return removed
