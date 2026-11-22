"""The incident summary Home Assistant (and Assist) can read.

The App publishes a sensor through MQTT discovery, using Home Assistant's own
``mqtt.publish`` action, so it needs no MQTT client or broker credentials:

* ``sensor.alarm_contact_center_incident``: a short Spanish summary of the latest
  incident as its state (what Assist reads out), the details as attributes.

Drills and test calls are not published: the sensor is about real alarms.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

DISCOVERY_TOPIC = "homeassistant/sensor/alarm_contact_center/incident/config"
STATE_TOPIC = "alarm_contact_center/incident/state"
ATTR_TOPIC = "alarm_contact_center/incident/attributes"

STATUS_TEXT = {
    "waiting": "en curso, esperando antes de llamar",
    "calling": "en curso, llamando",
    "acknowledged": "atendida",
    "disarmed": "desactivada",
    "no_answer": "nadie la atendió",
    "trunk_down": "sin línea telefónica",
    "cancelled": "cancelada",
    "failed": "con errores",
    "timeout": "sin respuesta en el tiempo máximo",
}

NO_INCIDENT = "Sin alarmas registradas"


def discovery_config() -> dict[str, Any]:
    return {
        "name": "Último incidente de alarma",
        "unique_id": "alarm_contact_center_incident",
        "default_entity_id": "sensor.alarm_contact_center_incident",
        "state_topic": STATE_TOPIC,
        "json_attributes_topic": ATTR_TOPIC,
        "icon": "mdi:shield-alert",
        "device": {"identifiers": ["alarm_contact_center"], "name": "Alarm Contact Center",
                   "manufacturer": "Home Assistant Apps by brainstormi", "model": "Alarm Contact Center"},
    }


def _when(iso: str, now: dt.datetime | None = None) -> str:
    try:
        t = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    now = now or dt.datetime.now(t.tzinfo)
    if t.date() == now.date():
        return f"hoy a las {t:%H:%M}"
    if t.date() == (now - dt.timedelta(days=1)).date():
        return f"ayer a las {t:%H:%M}"
    return f"el {t:%d/%m} a las {t:%H:%M}"


def _clock(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def summary(inc: dict[str, Any] | None, now: dt.datetime | None = None) -> str:
    """One sentence, under 255 characters (the limit of a sensor state)."""
    if not inc:
        return NO_INCIDENT
    parts = [f"Alarma {_when(inc.get('started', ''), now)}".strip()]
    if inc.get("sensor"):
        parts.append(f"por {inc['sensor']}")
    status = STATUS_TEXT.get(inc.get("status", ""), inc.get("status", ""))
    if inc.get("status") == "acknowledged" and inc.get("acknowledged_by"):
        status = f"atendida por {inc['acknowledged_by']}"
    parts.append(status)
    person = inc.get("last_person")
    if person:
        parts.append(f"última persona vista en {person['camera']} a las {_clock(person['start'])}")
    text = ", ".join(p for p in parts if p) + "."
    return text[:250]


def attributes(inc: dict[str, Any] | None) -> dict[str, Any]:
    if not inc:
        return {}
    persons = inc.get("persons") or []
    return {
        "incident_id": inc.get("id"),
        "started": inc.get("started"),
        "status": inc.get("status"),
        "active": inc.get("active"),
        "sensor": inc.get("sensor"),
        "acknowledged_by": inc.get("acknowledged_by") or None,
        "people_seen": len(persons),
        "cameras": sorted({p["camera"] for p in persons}),
        "last_person_camera": (inc.get("last_person") or {}).get("camera"),
        "last_person_time": dt.datetime.fromtimestamp(inc["last_person"]["start"]).astimezone().isoformat(timespec="seconds")
        if inc.get("last_person") else None,
        "timeline": [f"{e['t'][11:16]} {e['text']}" for e in (inc.get("events") or [])][-20:],
    }


def messages(inc: dict[str, Any] | None) -> list[tuple[str, str]]:
    """(topic, payload) pairs to publish, all retained."""
    return [
        (DISCOVERY_TOPIC, json.dumps(discovery_config(), ensure_ascii=False)),
        (STATE_TOPIC, summary(inc)),
        (ATTR_TOPIC, json.dumps(attributes(inc), ensure_ascii=False)),
    ]


class Reporter:
    """Publishes the latest real alarm whenever it changes (coalescing bursts)."""

    def __init__(self, publish: Any, incidents: Any, delay: float = 2.0) -> None:
        self.publish = publish  # async (topic, payload, retain) -> None
        self.incidents = incidents
        self.delay = delay
        self.latest: dict[str, Any] | None = None
        self._pending = None
        self._inc: Any = None
        self._last_sent: list[tuple[str, str]] = []

    def load(self) -> None:
        self.latest = next((i for i in self.incidents.items() if i.get("kind") == "alarm"), None)
        if self.latest:
            self.latest["active"] = False

    def on_change(self, inc: Any) -> None:
        if inc.kind != "alarm":
            return
        self._inc = inc
        if self._pending is None or self._pending.done():
            self._pending = asyncio.get_running_loop().create_task(self._later())

    async def _later(self) -> None:
        await asyncio.sleep(self.delay)
        await self.send()

    async def send(self, force: bool = False) -> None:
        inc = self._inc
        if inc is not None:
            self.latest = inc.final or inc.to_dict()
        msgs = messages(self.latest)
        if msgs == self._last_sent and not force:
            return
        for topic, payload in msgs:
            await self.publish(topic, payload, True)
        self._last_sent = msgs
