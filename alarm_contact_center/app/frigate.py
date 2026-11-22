"""Frigate person alerts, kept in memory so an incident can include what happened
just before the alarm triggered.

The App does not listen to Frigate itself. A Home Assistant automation already
decides what counts as a person alert (``frigate/reviews`` with ``severity: alert``
and a ``person`` object, on the cameras the household cares about) and writes, per
camera, an ``input_text`` helper with::

    <review id>,<detection id>,<detection id>...

for example ``1795354401.831865-vqtljq,1795354411.489668-aradwo``. The review id
starts with the alert's start time (Unix seconds). The first detection id is the
Frigate event whose snapshot and clip represent the alert. The value grows while
the alert is open, as Frigate adds detections.

The App follows those helpers (configured, or detected by their value) and turns
every change into a :class:`PersonAlert`. The camera name comes from the helpers'
friendly names: the words they all share are dropped, so "Person event garden"
and "Person event garage" become "Garden" and "Garage".
"""

from __future__ import annotations

import re
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

KEEP_S = 2 * 3600  # long enough for the largest "minutes before the trigger" setting

ID_RE = re.compile(r"^\d{9,11}(\.\d+)?-[a-z0-9]{4,10}$")


@dataclass
class PersonAlert:
    review_id: str
    camera: str  # display name
    entity_id: str
    start: float
    detections: list[str] = field(default_factory=list)

    @property
    def event_id(self) -> str:
        """The detection whose snapshot/clip represents this alert."""
        return self.detections[0] if self.detections else ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_id"] = self.event_id
        return d


def parse_value(value: str) -> tuple[str, list[str]] | None:
    """'<review>,<det>,...' -> (review, [dets]); None if it is not that format."""
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    if not parts or not all(ID_RE.match(p) for p in parts):
        return None
    return parts[0], parts[1:]


def start_time(review_id: str) -> float | None:
    try:
        return float(review_id.split("-", 1)[0])
    except ValueError:
        return None


def looks_like_person_helper(state: dict[str, Any]) -> bool:
    return str(state.get("entity_id", "")).startswith("input_text.") and parse_value(state.get("state", "")) is not None


def camera_names(names: dict[str, str]) -> dict[str, str]:
    """entity_id -> friendly name without the words every helper shares."""
    words = {eid: (name or eid.split(".", 1)[-1].replace("_", " ")).split() for eid, name in names.items()}
    if len(words) > 1:
        common = 0
        lists = list(words.values())
        while all(len(w) > common + 1 for w in lists) and len({w[common].lower() for w in lists}) == 1:
            common += 1
        words = {eid: w[common:] for eid, w in words.items()}
    out = {}
    for eid, w in words.items():
        text = " ".join(w) or eid
        out[eid] = text[:1].upper() + text[1:]
    return out


class FrigateWatcher:
    def __init__(self, keep_s: int = KEEP_S) -> None:
        self.keep_s = keep_s
        self.alerts: dict[str, PersonAlert] = {}
        self.cameras: dict[str, str] = {}  # entity_id -> display name

    def set_helpers(self, names: dict[str, str]) -> None:
        """The helpers being followed, with their friendly names."""
        self.cameras = camera_names(names)

    @property
    def entities(self) -> list[str]:
        return list(self.cameras)

    def on_value(self, entity_id: str, value: str, now: float | None = None) -> tuple[PersonAlert | None, bool]:
        """Feed a helper value. Returns (alert, is_new); alert is None if the value is not an alert."""
        now = time.time() if now is None else now
        self._prune(now)
        parsed = parse_value(value)
        if parsed is None:
            return None, False
        rid, detections = parsed
        existing = self.alerts.get(rid)
        if existing:
            existing.detections = list(dict.fromkeys(existing.detections + detections))
            return existing, False
        start = start_time(rid) or now
        if start < now - self.keep_s:
            return None, False
        alert = PersonAlert(review_id=rid, camera=self.cameras.get(entity_id, entity_id), entity_id=entity_id,
                            start=start, detections=detections)
        self.alerts[rid] = alert
        return alert, True

    def window(self, start: float, end: float) -> list[PersonAlert]:
        return sorted((a for a in self.alerts.values() if start <= a.start <= end), key=lambda a: a.start)

    def latest(self) -> PersonAlert | None:
        return max(self.alerts.values(), key=lambda a: a.start, default=None)

    def _prune(self, now: float) -> None:
        for rid in [rid for rid, a in self.alerts.items() if a.start < now - self.keep_s]:
            del self.alerts[rid]


def pick_helpers(configured: Iterable[str], states: Iterable[dict[str, Any]]) -> dict[str, str]:
    """entity_id -> friendly name of the helpers to follow: the configured ones, or
    every input_text whose value looks like a Frigate alert when none are configured."""
    by_id = {s.get("entity_id"): s for s in states}
    configured = list(configured)
    ids = configured or [eid for eid, s in by_id.items() if looks_like_person_helper(s)]
    return {eid: str(((by_id.get(eid) or {}).get("attributes") or {}).get("friendly_name") or "") for eid in ids}


def url_from_title(title: str) -> str:
    """The Frigate integration titles its entry with the server's host:port."""
    title = (title or "").strip().rstrip("/")
    if not title or " " in title:
        return ""
    return title if title.startswith(("http://", "https://")) else f"http://{title}"


class FrigateAPI:
    """Snapshots, clips and retain straight from Frigate's API.

    Not through the Frigate integration's notification proxy: that view fails for
    requests that reach Home Assistant through the Supervisor, which is the only way
    an App can call it. The address comes from the settings or, when empty, from the
    Frigate integration (detected at every connection).
    """

    PATHS = {"snapshot.jpg": "snapshot.jpg?bbox=0", "clip.mp4": "clip.mp4"}

    def __init__(self, settings: Any) -> None:
        self.settings = settings  # callable returning the current settings
        self.detected = ""

    @property
    def base(self) -> str:
        return (self.settings().get("frigate_url") or self.detected or "").rstrip("/")

    def open(self, event_id: str, name: str, range_header: str = "", timeout: float = 30) -> Any:
        """urllib response for a snapshot or clip; the caller reads and closes it."""
        if not self.base:
            raise OSError("Frigate address unknown")
        headers = {"Range": range_header} if range_header else {}
        req = urllib.request.Request(f"{self.base}/api/events/{event_id}/{self.PATHS[name]}", headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)

    def read(self, event_id: str, name: str) -> bytes:
        with self.open(event_id, name, timeout=20) as resp:
            return resp.read()

    def retain(self, event_id: str) -> None:
        if not self.base:
            raise OSError("Frigate address unknown")
        req = urllib.request.Request(f"{self.base}/api/events/{event_id}/retain", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
