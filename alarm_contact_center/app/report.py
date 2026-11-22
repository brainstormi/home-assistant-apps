"""The incident summary Home Assistant (and Assist) can read.

The App publishes a sensor through MQTT discovery, using Home Assistant's own
``mqtt.publish`` action, so it needs no MQTT client or broker credentials:

* ``sensor.alarm_contact_center_incident``: a short Spanish summary of the latest
  real alarm as its state (what Assist reads out), the details as attributes,
  including the whole timeline the dashboard card shows.
* ``sensor.alarm_contact_center_drill``: the same for the latest drill, so the card
  can be tried without a real alarm. Not meant for Assist.

Test calls are not published.
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
NO_DRILL = "Sin simulacros registrados"
MAX_EVENTS = 60  # timeline entries in the attributes (they stay well under HA's 16 kB)

# kind -> (key, name, icon)
SENSORS = {
    "alarm": ("incident", "Último incidente de alarma", "mdi:shield-alert"),
    "drill": ("drill", "Último simulacro de alarma", "mdi:shield-check-outline"),
}


def topics(kind: str = "alarm") -> tuple[str, str, str]:
    key = SENSORS[kind][0]
    return (f"homeassistant/sensor/alarm_contact_center/{key}/config",
            f"alarm_contact_center/{key}/state", f"alarm_contact_center/{key}/attributes")


def discovery_config(kind: str = "alarm") -> dict[str, Any]:
    key, name, icon = SENSORS[kind]
    _, state_topic, attr_topic = topics(kind)
    return {
        "name": name,
        "unique_id": f"alarm_contact_center_{key}",
        "default_entity_id": f"sensor.alarm_contact_center_{key}",
        "state_topic": state_topic,
        "json_attributes_topic": attr_topic,
        "icon": icon,
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
    word = "Simulacro" if inc.get("kind") == "drill" else "Alarma"
    parts = [f"{word} {_when(inc.get('started', ''), now)}".strip()]
    if inc.get("sensor"):
        parts.append(f"por {inc['sensor']}")
    status = STATUS_TEXT.get(inc.get("status", ""), inc.get("status", ""))
    if inc.get("status") == "acknowledged" and inc.get("acknowledged_by"):
        status = f"atendida por {inc['acknowledged_by']}"
    if inc.get("phase") == "open":
        status += ", pendiente de desarmar"
    elif inc.get("phase") == "closing" and inc.get("status") != "disarmed":
        status += ", ya desarmada"
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
        "phase": inc.get("phase"),
        "sensor": inc.get("sensor"),
        "acknowledged_by": inc.get("acknowledged_by") or None,
        "people_seen": len(persons),
        "cameras": sorted({p["camera"] for p in persons}),
        "last_person_camera": (inc.get("last_person") or {}).get("camera"),
        "last_person_time": dt.datetime.fromtimestamp(inc["last_person"]["start"]).astimezone().isoformat(timespec="seconds")
        if inc.get("last_person") else None,
        "timeline": [f"{e['t'][11:16]} {e['text']}" for e in (inc.get("events") or [])][-20:],
        # For the dashboard card.
        "kind": inc.get("kind"),
        "disarmed": bool(inc.get("disarmed")),
        "recording_until": inc.get("recording_until"),
        "events": [card_event(e) for e in (inc.get("events") or [])][-MAX_EVENTS:],
    }


def card_event(e: dict[str, Any]) -> dict[str, Any]:
    media = e.get("media") or {}
    out = {"t": e.get("t"), "kind": e.get("kind"), "text": e.get("text"),
           "detail": str(e.get("detail") or "").replace("Alerta de Frigate", "Vista por la cámara")}
    if media.get("event_id"):
        out["event_id"] = media["event_id"]
        out["camera"] = media.get("camera")
    return out


def messages(inc: dict[str, Any] | None, kind: str = "alarm") -> list[tuple[str, str]]:
    """(topic, payload) pairs to publish, all retained."""
    discovery, state, attrs = topics(kind)
    state_text = summary(inc) if inc else (NO_DRILL if kind == "drill" else NO_INCIDENT)
    return [
        (discovery, json.dumps(discovery_config(kind), ensure_ascii=False)),
        (state, state_text),
        (attrs, json.dumps(attributes(inc), ensure_ascii=False)),
    ]


class Reporter:
    """Publishes the latest alarm (or drill) whenever it changes (coalescing bursts)."""

    def __init__(self, publish: Any, incidents: Any, delay: float = 2.0, kind: str = "alarm") -> None:
        self.kind = kind
        self.publish = publish  # async (topic, payload, retain) -> None
        self.incidents = incidents
        self.delay = delay
        self.latest: dict[str, Any] | None = None
        self._pending = None
        self._inc: Any = None
        self._last_sent: list[tuple[str, str]] = []

    def load(self) -> None:
        self.latest = next((i for i in self.incidents.items() if i.get("kind") == self.kind), None)
        if self.latest:
            self.latest["active"] = False

    def on_change(self, inc: Any) -> None:
        if inc.kind != self.kind:
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
        msgs = messages(self.latest, self.kind)
        if msgs == self._last_sent and not force:
            return
        for topic, payload in msgs:
            await self.publish(topic, payload, True)
        self._last_sent = msgs


class Reporters(list):
    """The alarm and the drill reporters behind one listener."""

    def on_change(self, inc: Any) -> None:
        for r in self:
            r.on_change(inc)

    async def send(self, force: bool = False) -> None:
        for r in self:
            await r.send(force)
