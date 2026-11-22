"""Minimal Home Assistant API client through the Supervisor proxy (read-only in this phase)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .store import DEFAULT_PIN_ENTITY, PinStatus

log = logging.getLogger(__name__)

# Status entities shown on the panel home page.
STATUS_ENTITIES = {
    "trunk": "binary_sensor.siptrunk_registered",
    "ami": "binary_sensor.ami_connected",
    "openai": "binary_sensor.asterisk_realtime_bridge_openai",
    "alarm": "alarm_control_panel.alarmo",
}


class HomeAssistant:
    def __init__(self, base_url: str | None = None, token: str | None = None, cache_seconds: float = 5.0) -> None:
        self.base_url = (base_url or os.environ.get("HA_API_URL") or "http://supervisor/core/api").rstrip("/")
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    def _get(self, path: str) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(path)
            if hit and now - hit[0] < self.cache_seconds:
                return hit[1]
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        with self._lock:
            self._cache[path] = (now, data)
        return data

    def states(self) -> list[dict[str, Any]]:
        try:
            return self._get("/states")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as err:
            log.warning("Could not read Home Assistant states: %s", err)
            return []

    def services(self) -> list[dict[str, Any]]:
        try:
            return self._get("/services")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as err:
            log.warning("Could not read Home Assistant services: %s", err)
            return []

    # ----------------------------------------------------------------- queries
    def persons(self, states: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
        states = self.states() if states is None else states
        out = []
        for s in states:
            eid = s.get("entity_id", "")
            if eid.startswith("person."):
                out.append({"entity_id": eid, "name": (s.get("attributes") or {}).get("friendly_name") or eid})
        return sorted(out, key=lambda p: p["name"].lower())

    def alarm_sensors(self, states: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
        """Door, window and motion sensors, to pick an example sensor for a drill.

        Read from Home Assistant's states, never from the alarm's own configuration.
        """
        states = self.states() if states is None else states
        classes = {"door", "window", "garage_door", "opening", "motion", "occupancy", "presence"}
        out = []
        for s in states:
            eid = s.get("entity_id", "")
            attrs = s.get("attributes") or {}
            if eid.startswith("binary_sensor.") and attrs.get("device_class") in classes:
                out.append({"entity_id": eid, "name": attrs.get("friendly_name") or eid})
        return sorted(out, key=lambda x: x["name"].lower())

    def notify_targets(self) -> list[str]:
        """Mobile app notify services (mobile_app_*)."""
        for domain in self.services():
            if domain.get("domain") == "notify":
                return sorted(name for name in (domain.get("services") or {}) if name.startswith("mobile_app_"))
        return []

    def status(self, states: list[dict[str, Any]] | None = None, entities: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
        states = self.states() if states is None else states
        by_id = {s.get("entity_id"): s for s in states}
        out = {}
        for key, eid in {**STATUS_ENTITIES, **(entities or {})}.items():
            s = by_id.get(eid)
            out[key] = {
                "entity_id": eid,
                "state": s.get("state") if s else "missing",
                "since": s.get("last_changed") if s else None,
            }
        return out

    def pin_status(self, entity_id: str, states: list[dict[str, Any]] | None = None) -> PinStatus:
        """Read the Bridge entity that reports the PIN mode and the persons with a PIN.

        The Bridge publishes ``sensor.asterisk_realtime_bridge_pin``: the mode in the
        state, the persons in ``people_with_pin`` and its own diagnostics in
        ``complete``, ``management`` and ``issues``. Older or alternative attribute
        names (``pin_mode``/``mode``, ``persons_with_pin``/``persons``) are accepted too.
        """
        entity_id = entity_id or DEFAULT_PIN_ENTITY
        states = self.states() if states is None else states
        s = next((x for x in states if x.get("entity_id") == entity_id), None)
        if not s or s.get("state") in ("unavailable", "unknown"):
            return PinStatus()
        attrs = s.get("attributes") or {}
        mode = str(attrs.get("pin_mode") or attrs.get("mode") or s.get("state") or "").lower()
        if mode not in ("global", "individual"):
            return PinStatus()
        persons = attrs.get("people_with_pin") or attrs.get("persons_with_pin") or attrs.get("persons") or []
        if not isinstance(persons, list):
            persons = []
        issues = attrs.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        complete = attrs.get("complete")
        return PinStatus(
            mode,
            [str(p) for p in persons],
            complete=complete if isinstance(complete, bool) else None,
            management=str(attrs.get("management") or ""),
            issues=[str(i) for i in issues],
        )
