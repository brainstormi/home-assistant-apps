"""The alert chain: what happens from the moment the alarm triggers.

Pure asyncio and standard library. Everything that touches Home Assistant goes
through a ``Port`` (see ha_ws.py for the real one and the tests for a fake), so the
whole chain can be tested without Home Assistant, Asterisk or a phone.

Flow of a real incident:
1. Alarmo goes to ``triggered``: open an incident and notify the household's app
   right away (critical notification with a "Yo me encargo" button).
2. Wait ``call_delay`` seconds. If the alarm is disarmed (or someone presses the
   button) in the meantime, nobody is called.
3. Give the alarm priority: drop other queued calls and hang up any call in progress.
4. Call the active contacts with a phone, in order, through script.llamada_saliente
   (one attempt per script run; retries are done here so they can be stopped).
   The chain is circular: after the last contact it starts again from the first,
   for ``rounds`` passes (0 = until someone confirms, capped by max_chain_minutes).
5. Someone confirms (on the phone or with the app button): stop the chain, tell
   whoever is on the phone, and update everyone's notification. Never disarms.
6. The line is down (registration lost, or CHANUNAVAIL/CONGESTION twice more
   after a short wait): instead of calling, management contacts get a growing
   burst of HA app notifications (1+10, 2+20, 3+30) following their attempts, and
   other contacts with Telegram get a message. If the line comes back while the
   alarm is open and nobody has taken charge, the calls resume.

Messages are in Spanish: they are read and heard by the household.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime as dt
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .frigate import FrigateWatcher, PersonAlert
from .store import Store

log = logging.getLogger(__name__)

TRUNK_DOWN = {"CHANUNAVAIL", "CONGESTION"}
ACK_PREFIX = "ACC_ACK"
ACK_BUTTON = "Yo me encargo"
ACK_EXPLAIN = "Si pulsas «Yo me encargo», dejaremos de llamar y avisar al resto de contactos. La alarma no se desarma."
LINE_RETRIES = 2  # extra tries before a call failure counts as the line being down
LINE_RETRY_S = 5
BURST_CRITICAL_GAP_S = 2  # critical notifications need this much to each play their sound
BURST_NORMAL_GAP_S = 1  # normal ones vibrate one by one at this pace
BURST_CRITICAL_VOLUME = 0.6
DISARMED_STATES = {"disarmed"}
TRIGGERED_STATE = "triggered"
TEST_PREFIX = "Esto es una prueba de Alarm Contact Center. "
DRILL_PREFIX = "Esto es un simulacro de alarma. "
SNAPSHOT_WAITS_S = (8, 20, 40)  # when to try copying a snapshot after the alert
CHAIN_KINDS = ("alarm", "drill")
CONTEXT_ENTITY = {"alarm": "sensor.alarm_contact_center_incident", "drill": "sensor.alarm_contact_center_drill"}
AFTER_CLOSE = "después del desarme"
CLOSE_POLL_S = 3600  # the closer re-checks at least this often  # incidents that run the full chain
AFTER_TEXT = {
    "confirmed": "Confirmó el aviso", "answered": "Contestó sin confirmar", "resolved": "Se le informó y colgó",
    "cancelled": "Llamada retirada", "voicemail": "Buzón de voz", "no_answer": "No contestó",
}
PROFILE_TEXT = {"Management": "modo gestión (con PIN)", "QueryOnly": "solo consulta"}


class Port(Protocol):
    """What the engine needs from Home Assistant."""

    async def call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run script.llamada_saliente with these fields and return its response."""

    async def notify(self, service: str, title: str, message: str, data: dict[str, Any]) -> None: ...

    async def bridge_note(self, call_id: str, text: str, resolve: bool = False) -> None:
        """Tell the person on the phone. resolve=True also closes the notice (Bridge 0.9+)."""

    async def bridge_hangup(self, call_id: str = "") -> None: ...

    async def stop_queued_calls(self) -> None:
        """Stop script.llamada_saliente runs started by others (queued or running)."""

    async def friendly_name(self, entity_id: str) -> str: ...


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def hhmm(ts: float | None = None) -> str:
    return time.strftime("%H:%M", time.localtime(ts if ts is not None else time.time()))


def render_message(template: str, **values: str) -> str:
    text = template
    for key in ("nombre", "sensor", "zona", "hora", "atendido"):
        text = text.replace("{" + key + "}", values.get(key, ""))
    return re.sub(r"\s{2,}", " ", text).strip()


def join_names(names: list[str]) -> str:
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " y " + names[-1]


OPENING_CLASSES = {"door", "window", "garage_door", "opening"}
LOCK_TEXT = {"locked": "se cierra con llave", "unlocked": "se abre la cerradura", "open": "abierta",
             "jammed": "atascada", "unlocking": "", "locking": "", "opening": ""}


# Doors with one sensor per end position ("Garage door closed", "Garage door open"):
# the name says which position the sensor detects.
POSITION_WORDS = {"cerrada": "closed", "cerrado": "closed", "closed": "closed",
                  "abierta": "open", "abierto": "open", "open": "open"}
POSITION_TEXT = {("closed", "on"): "abierta", ("closed", "off"): "cerrada",
                 ("open", "on"): "abierta del todo", ("open", "off"): "se está cerrando"}


def describe_position(name: str, state: str, device_class: str) -> tuple[str, str]:
    """(name without the position word, text) for an end-position sensor; (name, '') otherwise."""
    words = name.split()
    if device_class not in OPENING_CLASSES or len(words) < 2 or state not in ("on", "off"):
        return name, ""
    position = POSITION_WORDS.get(words[-1].lower())
    if not position:
        return name, ""
    return " ".join(words[:-1]), POSITION_TEXT[(position, state)]


def describe_change(entity_id: str, state: str, device_class: str) -> str:
    """Short Spanish text for a sensor change worth putting in the timeline ('' = skip)."""
    if state in ("unavailable", "unknown", ""):
        return "no disponible" if state == "unavailable" else ""
    if entity_id.startswith("lock."):
        return LOCK_TEXT.get(state, state)
    if entity_id.startswith("binary_sensor."):
        if state != "on":
            return "se cierra" if device_class in OPENING_CLASSES else ""
        if device_class in OPENING_CLASSES:
            return "se abre"
        if "person" in entity_id or "persona" in entity_id:
            return "detecta una persona"
        if device_class == "motion":
            return "detecta movimiento"
        if device_class in ("occupancy", "presence"):
            return "detecta presencia"
        return "activo"
    return state


def fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    return f"{seconds // 60} min"


class IncidentLog:
    """Last incidents, kept in /data so the panel can show them after a restart."""

    def __init__(self, path: str | os.PathLike[str], keep: int = 50) -> None:
        self.path = Path(path)
        self.keep = keep
        self._lock = threading.Lock()
        try:
            self._items: list[dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._items, list):
                self._items = []
        except (OSError, json.JSONDecodeError):
            self._items = []

    def save(self, incident: dict[str, Any]) -> None:
        with self._lock:
            self._items = [i for i in self._items if i.get("id") != incident["id"]]
            self._items.insert(0, copy.deepcopy(incident))
            del self._items[self.keep:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def items(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._items)


class Incident:
    def __init__(self, kind: str, sensor: str, sensor_entity: str = "", test_contact: str = "") -> None:
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind  # "alarm", "drill" (full chain, Alarmo untouched) or "test" (one call)
        self.sensor = sensor
        self.sensor_entity = sensor_entity
        self.test_contact = test_contact
        self.only_contacts: set[str] | None = None  # drill: the contacts taking part
        self.line_down = False  # calls do not go out: contacts are alerted by notifications
        self.simulate_line_down = False  # drill: behave as if the line were down
        self.next_index = 0  # contact the chain would go to next (to resume after the line returns)
        self.acknowledged_at = 0.0
        self.burst_seq = 0
        self.requested_profile = ""
        self.started = time.time()
        self.status = "waiting"  # waiting, calling, acknowledged, disarmed, no_answer, trunk_down, cancelled, failed, timeout
        self.acknowledged_by = ""
        self.ack_via = ""
        self.events: list[dict[str, Any]] = []
        self.stop = asyncio.Event()  # set when the chain must stop
        self.stop_reason = ""
        self.current_call_id = ""
        self.call_answered = False
        self.current_contact = ""
        self.calling_number = ""
        self.not_answered: list[str] = []
        self.task: asyncio.Task | None = None
        self.persons: list[dict[str, Any]] = []  # Frigate person alerts in this incident
        self.media_jobs: set[str] = set()
        self.final: dict[str, Any] | None = None  # set once the chain has ended
        # Lifecycle: the chain (calls) ends when someone takes charge, but the incident stays
        # open, recording cameras and sensors, until the alarm is disarmed plus the minutes
        # set in the settings, or until the maximum time open if nobody disarms it.
        self.record_until = 0.0
        self.disarmed_at = 0.0
        self.closed = False
        self.chain_done = False  # the calls are over (set before the task itself finishes)
        self.wake = asyncio.Event()  # the close time changed
        self.on_change: Any = None  # set by the engine: called after every change

    # ------------------------------------------------------- persistence
    SNAPSHOT_FIELDS = ("id", "kind", "sensor", "sensor_entity", "started", "status", "acknowledged_by", "ack_via",
                       "acknowledged_at", "events", "persons", "line_down", "simulate_line_down", "next_index",
                       "burst_seq", "not_answered", "record_until", "disarmed_at", "closed", "stop_reason",
                       "calling_number", "current_contact", "requested_profile")

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to carry on after the App restarts."""
        data = {k: copy.deepcopy(getattr(self, k)) for k in self.SNAPSHOT_FIELDS}
        data["only_contacts"] = sorted(self.only_contacts) if self.only_contacts is not None else None
        data["chain_done"] = bool(self.chain_done or self.task is None or self.task.done())
        data["saved_at"] = time.time()
        return data

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "Incident":
        inc = cls(str(data["kind"]), str(data.get("sensor") or ""), str(data.get("sensor_entity") or ""))
        for k in cls.SNAPSHOT_FIELDS:
            if k in data:
                setattr(inc, k, copy.deepcopy(data[k]))
        only = data.get("only_contacts")
        inc.only_contacts = set(only) if only is not None else None
        inc.chain_done = bool(data.get("chain_done", True))
        return inc

    def add(self, kind: str, text: str, detail: str = "", t: str | None = None, media: dict[str, Any] | None = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"t": t or now_iso(), "kind": kind, "text": text, "detail": detail}
        if media:
            entry["media"] = media
        self.events.append(entry)
        log.info("[%s] %s%s", self.id, text, f" ({detail})" if detail else "")
        self.changed()
        return entry

    def changed(self) -> None:
        if self.on_change:
            self.on_change(self)

    def phase(self) -> str:
        """calling: the chain runs; open: handled or not, waiting for the disarm;
        closing: disarmed, still recording; closed."""
        if self.task is not None and not self.task.done() and not self.chain_done:
            return "calling"
        if self.closed or self.kind not in CHAIN_KINDS:
            return "closed"
        return "closing" if self.disarmed_at else "open"

    def last_person(self) -> dict[str, Any] | None:
        return max(self.persons, key=lambda p: p["start"]) if self.persons else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "sensor": self.sensor,
            "sensor_entity": self.sensor_entity,
            "started": dt.datetime.fromtimestamp(self.started).astimezone().isoformat(timespec="seconds"),
            "status": self.status,
            "active": self.task is not None and not self.task.done(),
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": dt.datetime.fromtimestamp(self.acknowledged_at).astimezone().isoformat(timespec="seconds")
            if self.acknowledged_at else None,
            "participants": sorted(self.only_contacts) if self.only_contacts is not None else None,
            "ack_via": self.ack_via,
            "events": copy.deepcopy(self.events),
            "persons": copy.deepcopy(self.persons),
            "last_person": copy.deepcopy(self.last_person()),
            "recording_until": dt.datetime.fromtimestamp(self.record_until).astimezone().isoformat(timespec="seconds")
            if self.record_until > time.time() and not self.closed else None,
            "disarmed": bool(self.disarmed_at),
            "phase": self.phase(),
        }


class Engine:
    def __init__(self, store: Store, port: Port, incidents: IncidentLog, time_scale: float = 1.0,
                 media: Any = None) -> None:
        self.store = store
        self.port = port
        self.incidents = incidents
        self.time_scale = time_scale  # tests use 0 to skip real waiting
        self.current: Incident | None = None
        self.alarm_state = ""
        self.frigate = FrigateWatcher()
        self.media = media  # MediaStore for snapshot copies (None: no copies)
        self.listeners: list[Any] = []  # called with the Incident after every change
        self.state_path: Path | None = None  # where the open incident is kept across restarts
        self.restored: Incident | None = None  # open incident found at start, not reconciled yet
        self.restored_saved_at = 0.0
        self.line_down_hint: Any = lambda: False  # set by the trunk monitor: registration lost
        self.telegram_alert: Any = None  # async (inc, contact): Telegram message instead of a call

    # ----------------------------------------------------------- frigate
    async def on_person_helper(self, entity_id: str, value: str) -> None:
        """A Frigate person helper changed: the alert goes into the running incident."""
        alert, _ = self.frigate.on_value(entity_id, value)
        inc = self.current
        if alert and self._listening(inc):
            late = self._late(inc)
            self._add_person(inc, alert, after=late)
            if late:
                self._save_late(inc)

    def _listening(self, inc: Incident | None) -> bool:
        """Cameras and sensors go into the incident while it runs and for the minutes
        after it ends set in the settings ("seguir registrando")."""
        return bool(inc and inc.kind in CHAIN_KINDS and not inc.closed
                    and (self._active(inc) or time.time() < inc.record_until))

    @staticmethod
    def _late(inc: Incident) -> bool:
        """After the alarm was disarmed."""
        return bool(inc.disarmed_at)

    def _save_late(self, inc: Incident) -> None:
        if inc.task is not None and inc.task.done():
            inc.final = inc.to_dict()
            self.incidents.save(inc.final)

    def _add_person(self, inc: Incident, alert: PersonAlert, before: bool = False, after: bool = False) -> None:
        existing = next((p for p in inc.persons if p["review_id"] == alert.review_id), None)
        data = alert.to_dict()
        if existing:
            existing.update(data)
            for e in inc.events:
                if (e.get("media") or {}).get("review_id") == alert.review_id:
                    e["detail"] = self._person_detail(alert, before, AFTER_CLOSE in e["detail"])
                    e["media"]["event_id"] = alert.event_id
            inc.changed()
        else:
            inc.persons.append(data)
            when = dt.datetime.fromtimestamp(alert.start).astimezone().isoformat(timespec="seconds")
            inc.add("person", f"Persona en {alert.camera}", self._person_detail(alert, before, after), t=when,
                    media={"review_id": alert.review_id, "event_id": alert.event_id, "camera": alert.camera, "snapshot": False})
        if alert.event_id:
            asyncio.get_running_loop().create_task(self._keep_media(inc, alert))

    @staticmethod
    def _person_detail(alert: PersonAlert, before: bool, after: bool = False) -> str:
        parts = ["Vista por la cámara"]
        if before:
            parts.append("antes del disparo")
        if after:
            parts.append(AFTER_CLOSE)
        return " · ".join(parts)

    async def _keep_media(self, inc: Incident, alert: PersonAlert) -> None:
        """Copy the snapshot into the incident and ask Frigate to keep the event.

        Frigate keeps improving an event's snapshot for a few seconds, so the copy is
        taken after a short wait, and retried if Frigate has none yet.
        """
        event_id = alert.event_id
        key = f"{alert.review_id}:{event_id}"
        if key in inc.media_jobs:
            return
        inc.media_jobs.add(key)
        entry = next((e for e in inc.events if (e.get("media") or {}).get("review_id") == alert.review_id), None)
        if entry is None:
            return
        retain = getattr(self.port, "frigate_retain", None)
        if retain:
            try:
                await retain(event_id)
            except Exception as err:  # noqa: BLE001 - older Frigate integrations lack the command
                log.info("[%s] Frigate could not retain %s: %s", inc.id, event_id, err)
        fetch = getattr(self.port, "frigate_snapshot", None)
        if fetch is None or self.media is None:
            return
        for wait in SNAPSHOT_WAITS_S:
            await asyncio.sleep(wait * self.time_scale)
            try:
                data = await fetch(event_id)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] snapshot %s not ready: %s", inc.id, event_id, err)
                continue
            if data and self.media.save(inc.id, event_id, data):
                entry["media"]["snapshot"] = event_id
                inc.changed()
                self.incidents.save(inc.to_dict())
                return
        log.warning("[%s] no snapshot for %s", inc.id, event_id)

    # ------------------------------------------------- incident sensors
    async def on_watched_entity(self, entity_id: str, new: dict[str, Any], old: dict[str, Any] | None = None) -> None:
        """Sensors listed in the settings: their changes go into the running incident."""
        inc = self.current
        if not self._listening(inc):
            return
        if entity_id not in self.store.settings()["incident_entities"]:
            return
        state = str(new.get("state") or "")
        if old is not None and str(old.get("state") or "") == state:
            return
        attrs = new.get("attributes") or {}
        name = attrs.get("friendly_name") or entity_id
        name, text = describe_position(name, state, str(attrs.get("device_class") or ""))
        if not text:
            text = describe_change(entity_id, state, str(attrs.get("device_class") or ""))
        if text:
            late = self._late(inc)
            inc.add("sensor", f"{name}: {text}", AFTER_CLOSE if late else "")
            if late:
                self._save_late(inc)

    # ------------------------------------------------------------- inputs
    async def on_alarm_state(self, state: str, attributes: dict[str, Any] | None = None, restarted: bool = False) -> None:
        """Feed every state of the alarm_control_panel entity."""
        previous, self.alarm_state = self.alarm_state, state
        attributes = attributes or {}
        if restarted and self.restored is not None:
            await self._reconcile(state)
            return
        if state == TRIGGERED_STATE and previous != TRIGGERED_STATE:
            if self.current and self.current.kind == "alarm" and self._active(self.current):
                return
            await self._start_alarm(attributes, restarted)
        elif state in DISARMED_STATES and self.current and self.current.kind == "alarm" and not self.current.closed:
            if self._active(self.current) and not self.current.stop.is_set():
                await self._halt(self.current, "disarmed", "Alarma desarmada")
            else:
                await self._disarmed_later(self.current, "Alarma desarmada")

    async def on_bridge_event(self, data: dict[str, Any]) -> None:
        """asterisk_realtime_bridge_call events: learn the call_id of our call."""
        inc = self.current
        if not inc or not self._active(inc):
            return
        if data.get("direction", "outbound") != "outbound":
            return
        stage = data.get("stage")
        if stage == "started":
            number = re.sub(r"\D", "", str(data.get("number") or ""))[-9:]
            if inc.calling_number and number == inc.calling_number:
                inc.current_call_id = str(data.get("call_id") or "")
                inc.call_answered = False
                effective = str(data.get("effective_profile") or "")
                if effective and inc.requested_profile and effective != inc.requested_profile:
                    inc.add("profile", f"El Bridge hace la llamada en {PROFILE_TEXT.get(effective, effective)}",
                            f"Pedido: {PROFILE_TEXT.get(inc.requested_profile, inc.requested_profile)}. "
                            "Suele ser porque la persona no tiene un PIN válido en el Bridge.")
        elif stage == "answered" and inc.current_call_id and data.get("call_id") == inc.current_call_id:
            inc.call_answered = True

    async def on_notification_action(self, action: str) -> None:
        """mobile_app_notification_action: the "Lo atiendo" button."""
        parts = action.split(":")
        if len(parts) != 3 or parts[0] != ACK_PREFIX:
            return
        _, incident_id, contact_id = parts
        await self.take_charge(incident_id, contact_id, "la app")

    async def take_charge(self, incident_id: str, contact_id: str, via: str) -> dict[str, Any]:
        """Someone pressed "Yo me encargo" (HA app or Telegram).

        Returns {"result": ok | already | disarmed | closed | unknown, "by", "at"} so
        Telegram can answer the person.
        """
        inc = self.current
        if not inc or inc.id != incident_id or inc.kind not in CHAIN_KINDS:
            return {"result": "unknown"}
        if self.only_contacts_check(inc, contact_id) is False:
            return {"result": "unknown"}
        contact = next((c for c in self.store.contacts() if c["id"] == contact_id), None)
        result = await self._acknowledge(inc, contact["name"] if contact else "Alguien", via)
        return {"result": result, "by": inc.acknowledged_by, "at": hhmm(inc.acknowledged_at) if inc.acknowledged_at else ""}

    def only_contacts_check(self, inc: Incident, contact_id: str) -> bool:
        return inc.only_contacts is None or contact_id in inc.only_contacts

    def contact_by_chat(self, chat_id: str) -> dict[str, Any] | None:
        """The participating contact behind a Telegram chat (for take-charge by Telegram)."""
        inc = self.current
        for c in self.store.contacts():
            if c["telegram_chat_id"] and str(c["telegram_chat_id"]) == str(chat_id):
                if inc is None or self.only_contacts_check(inc, c["id"]):
                    return c
        return None

    async def start_test(self, contact_id: str) -> dict[str, Any]:
        """Call one contact with a test message. Never while a real alarm is running."""
        if self.current and self.current.kind == "alarm" and self.current.phase() != "closed":
            raise RuntimeError("Hay una alarma abierta: no se puede hacer una prueba hasta que se desarme y se cierre.")
        if self.current and self._active(self.current):
            if self.current.kind == "alarm":
                raise RuntimeError("Hay una alarma en curso: no se puede hacer una prueba.")
            raise RuntimeError("Ya hay una prueba o un simulacro en curso.")
        contact = next((c for c in self.store.contacts() if c["id"] == contact_id), None)
        if not contact:
            raise KeyError(contact_id)
        if not contact["phone"]:
            raise RuntimeError("Este contacto no tiene teléfono.")
        inc = Incident("test", "sensor de prueba", test_contact=contact_id)
        inc.add("start", f"Prueba de llamada a {contact['name']}")
        self._launch(inc)
        return inc.to_dict()

    async def start_drill(self, contact_ids: list[str], sensor_entity: str, line_down: bool = False) -> dict[str, Any]:
        """Run the whole chain as if the alarm had triggered, without touching Alarmo.

        Only the chosen contacts are notified and called; messages say it is a drill.
        Does not take the phone line from other calls (no priority step).
        """
        if self.current and self.current.kind == "alarm" and self.current.phase() != "closed":
            raise RuntimeError("Hay una alarma abierta: no se puede hacer un simulacro hasta que se desarme y se cierre.")
        if self.current and self._active(self.current):
            if self.current.kind == "alarm":
                raise RuntimeError("Hay una alarma en curso: no se puede hacer un simulacro.")
            raise RuntimeError("Ya hay una prueba o un simulacro en curso.")
        known = {c["id"] for c in self.store.contacts()}
        chosen = [cid for cid in dict.fromkeys(contact_ids) if cid in known]
        if not chosen:
            raise RuntimeError("Elige al menos un contacto para el simulacro.")
        sensor = "sensor de prueba"
        if sensor_entity:
            try:
                sensor = (await self.port.friendly_name(sensor_entity)) or sensor_entity
            except Exception:  # noqa: BLE001
                sensor = sensor_entity
        inc = Incident("drill", sensor, sensor_entity)
        inc.only_contacts = set(chosen)
        inc.simulate_line_down = bool(line_down)
        names = join_names([c["name"] for c in self.store.contacts() if c["id"] in inc.only_contacts])
        inc.add("triggered", "Simulacro: alarma disparada",
                f"Sensor: {sensor} · participan {names}" + (" · simulando línea caída" if line_down else ""))
        self._launch(inc)
        return inc.to_dict()

    async def simulate_disarm(self) -> None:
        """Drill only: behave as if Alarmo had been disarmed."""
        inc = self.current
        if not inc or inc.kind != "drill" or inc.closed:
            raise RuntimeError("No hay ningún simulacro en curso.")
        if inc.disarmed_at:
            raise RuntimeError("El simulacro ya está desarmado.")
        if self._active(inc) and not inc.stop.is_set():
            await self._halt(inc, "disarmed", "Simulacro: alarma desarmada")
        else:
            await self._disarmed_later(inc, "Simulacro: alarma desarmada")

    async def cancel(self) -> None:
        """Stop whatever is running (panel button). Does not touch Alarmo."""
        if self.current and self._active(self.current):
            await self._halt(self.current, "cancelled", "Cancelado desde el panel")

    def status(self) -> dict[str, Any] | None:
        return self.current.to_dict() if self.current else None

    # ------------------------------------------------------------ internals
    @staticmethod
    def _active(inc: Incident) -> bool:
        return inc.task is not None and not inc.task.done()

    def _launch(self, inc: Incident) -> None:
        previous = self.current
        if previous is not None and previous is not inc and not previous.closed and previous.kind in CHAIN_KINDS \
                and not self._active(previous):
            self._close(previous, "Cerrado: ha empezado otro incidente")
        self.current = inc
        inc.on_change = self._changed
        if inc.kind in CHAIN_KINDS:
            # What the cameras saw just before the trigger, ahead of it in the timeline.
            minutes = self.store.settings()["frigate_pre_minutes"]
            before = self.frigate.window(inc.started - minutes * 60, inc.started)
            later = self.frigate.window(inc.started, time.time() + 1)
            trigger_events, inc.events = inc.events, []
            for alert in before:
                self._add_person(inc, alert, before=True)
            inc.events.extend(trigger_events)
            for alert in later:
                self._add_person(inc, alert)
        inc.task = asyncio.get_running_loop().create_task(self._run(inc))
        self.incidents.save(inc.to_dict())
        self._changed(inc)

    def _changed(self, inc: Incident) -> None:
        self.persist(inc)
        for listener in list(self.listeners):
            try:
                listener(inc)
            except Exception:  # noqa: BLE001
                log.exception("Incident listener failed")

    async def _start_alarm(self, attributes: dict[str, Any], restarted: bool) -> None:
        # A test or a drill in progress gives way to the real alarm (an open one is closed by _launch).
        if self.current and self._active(self.current) and self.current.kind in ("test", "drill"):
            await self._halt(self.current, "cancelled", "Cancelado: ha saltado la alarma de verdad")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.current.task
        sensor_entity, sensor = await self._triggering_sensor(attributes)
        inc = Incident("alarm", sensor, sensor_entity)
        inc.add("triggered", "Alarma disparada", f"Sensor: {sensor}" + (" · reanudado tras reiniciar la App" if restarted else ""))
        self._launch(inc)

    async def _triggering_sensor(self, attributes: dict[str, Any]) -> tuple[str, str]:
        sensors = attributes.get("open_sensors") or {}
        entity = next(iter(sensors), "") if isinstance(sensors, dict) else ""
        if not entity:
            return "", "sensor desconocido"
        try:
            return entity, (await self.port.friendly_name(entity)) or entity
        except Exception:  # noqa: BLE001
            return entity, entity

    async def _wait(self, inc: Incident, seconds: float) -> bool:
        """Sleep, but wake up as soon as the incident must stop. True if it must stop."""
        if inc.stop.is_set():
            return True
        delay = max(0.0, seconds * self.time_scale)
        try:
            await asyncio.wait_for(inc.stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        return inc.stop.is_set()

    def _finish(self, inc: Incident, status: str) -> None:
        if inc.status in ("waiting", "calling"):
            inc.status = status

    async def _run(self, inc: Incident, resume: bool = False) -> None:
        try:
            if inc.kind in CHAIN_KINDS:
                await self._run_alarm(inc, resume)
            else:
                await self._run_test(inc)
        except asyncio.CancelledError:
            self._finish(inc, "cancelled")
            raise
        except Exception as err:  # noqa: BLE001 - never die silently during an alarm
            log.exception("[%s] chain failed", inc.id)
            inc.add("error", "Error interno en la cadena", str(err))
            self._finish(inc, "failed")
            with contextlib.suppress(Exception):
                await self._notify_household(inc, "Error en los avisos", "La cadena de llamadas ha fallado. Revisa la alarma.")
        finally:
            inc.chain_done = True
            if inc.kind in CHAIN_KINDS:
                inc.record_until = self._close_at(inc)
                if self.time_scale > 0:  # tests close incidents themselves
                    asyncio.get_running_loop().create_task(self._closer(inc))
            final = inc.to_dict()
            final["active"] = False  # the task is still running at this point
            self.incidents.save(final)
            inc.final = final
            self._changed(inc)

    # ------------------------------------------------------------ alarm
    async def _run_alarm(self, inc: Incident, resume: bool = False) -> None:
        settings = self.store.settings()
        if not resume:
            await self._notify_household(inc, "¡Alarma disparada!", f"{inc.sensor} a las {hhmm(inc.started)}.", ack_button=True)
            if settings["call_delay"]:
                inc.add("wait", f"Esperando {settings['call_delay']} s antes de llamar")
            if await self._wait(inc, settings["call_delay"]):
                return
        inc.status = "calling"

        if inc.kind == "alarm":
            # Priority: nothing else may hold the only phone line.
            with contextlib.suppress(Exception):
                await self.port.stop_queued_calls()
            with contextlib.suppress(Exception):
                await self.port.bridge_hangup("")

        contacts = [c for c in self._participants(inc) if c["phone"]]
        if not contacts:
            inc.add("no_contacts", "No hay contactos activos con teléfono")
            self._finish(inc, "no_answer")
            await self._notify_household(inc, "Alarma sin llamadas", "No hay contactos con teléfono a los que llamar.")
            return

        rounds = settings["rounds"]
        max_minutes = settings["max_chain_minutes"]

        def out_of_time() -> bool:
            # Only for unlimited rounds; a fixed number of rounds ends by itself.
            if rounds or time.time() <= inc.started + max_minutes * 60:
                return False
            inc.add("limit", f"Tiempo máximo de la cadena alcanzado ({max_minutes} min)")
            return True

        round_no = 0
        timed_out = False
        while not inc.stop.is_set() and not timed_out:
            round_no += 1
            if rounds and round_no > rounds:
                break
            if out_of_time():
                break
            if round_no > 1:
                inc.add("round", f"Ronda {round_no}: volviendo al primer contacto")
                await self._notify_household(inc, "Nadie ha confirmado todavía", f"Empieza la ronda {round_no} de llamadas.", ack_button=True)
            start = inc.next_index % len(contacts) if round_no == 1 else 0
            for index in range(start, len(contacts)):
                contact = contacts[index]
                if inc.stop.is_set():
                    break
                if out_of_time():
                    timed_out = True
                    break
                inc.next_index = (index + 1) % len(contacts)
                outcome = await self._call_contact(inc, contact, settings)
                if outcome == "confirmed":
                    break
            if inc.stop.is_set() or timed_out:
                break
            if round_no < rounds or not rounds:
                if await self._wait(inc, settings["round_pause_min"] * 60):
                    break

        if not inc.stop.is_set():
            if inc.line_down:
                self._finish(inc, "trunk_down")
                inc.add("no_answer", "Nadie ha aceptado gestionar la alarma", "Sin línea telefónica: se avisó por notificaciones")
                await self._notify_household(inc, "Nadie ha aceptado gestionar la alarma",
                                             "No se ha podido llamar (la línea no funciona) y nadie ha respondido a los avisos.",
                                             ack_button=True)
            else:
                self._finish(inc, "no_answer")
                inc.add("no_answer", "Nadie ha aceptado gestionar la alarma")
                await self._notify_household(inc, "Nadie ha aceptado gestionar la alarma",
                                             "La cadena de llamadas ha terminado sin que nadie la acepte.", ack_button=True)

    async def _call_contact(self, inc: Incident, contact: dict[str, Any], settings: dict[str, Any], test: bool = False) -> str:
        """Call one contact with its retries. Returns confirmed, trunk_down, notified, skipped,
        stopped or not_confirmed."""
        if not test and self._line_is_down(inc):
            await self._mark_line_down(inc, "Sin registro en el operador" if not inc.simulate_line_down else "Simulacro: línea caída")
            return await self._alert_without_line(inc, contact)
        for attempt in range(1, contact["attempts"] + 1):
            if inc.stop.is_set():
                return "stopped"
            atendido = "" if test else (f"{join_names(inc.not_answered)} no han contestado." if len(set(inc.not_answered)) > 1
                                        else f"{inc.not_answered[0]} no ha contestado." if inc.not_answered else "")
            message = render_message(settings["message"], nombre=contact["name"], sensor=inc.sensor,
                                     zona="", hora=hhmm(inc.started), atendido=atendido)
            if test:
                message = TEST_PREFIX + message
            elif inc.kind == "drill":
                message = DRILL_PREFIX + message
            manage = bool(contact["can_manage"] and contact["person"])
            params = {
                "numero": contact["phone"],
                "mensaje": message,
                "perfil": "Management" if manage else "QueryOnly",
                "persona": contact["person"] or contact["name"],
                # The assistant reads the incident's timeline from this sensor (Bridge 0.11+).
                **({"contexto_entidad": CONTEXT_ENTITY[inc.kind]} if inc.kind in CONTEXT_ENTITY else {}),
                "intentos": 1,
                "tiempo_respuesta": settings["answer_timeout"],
                "requiere_confirmacion": True,
                "confirmacion": contact["confirm"],
                "texto_confirmacion": settings["confirm_text"],
            }
            if settings["detect_voicemail"]:
                params["detectar_buzon"] = True
                vm = render_message(settings["voicemail_message"], nombre=contact["name"], sensor=inc.sensor,
                                    zona="", hora=hhmm(inc.started), atendido="")
                if vm:
                    params["mensaje_buzon"] = (TEST_PREFIX if test else DRILL_PREFIX if inc.kind == "drill" else "") + vm
            inc.current_contact, inc.calling_number, inc.current_call_id = contact["name"], contact["phone"], ""
            inc.requested_profile = params["perfil"]
            inc.call_answered = False
            suffix = f" ({attempt}/{contact['attempts']})" if contact["attempts"] > 1 else ""
            inc.add("call", f"Llamando a {contact['name']}{suffix}", PROFILE_TEXT[params["perfil"]].capitalize())
            self.incidents.save(inc.to_dict())
            retries = 0 if inc.line_down else LINE_RETRIES  # once known to be down, one try is enough
            for retry in range(retries + 1):
                try:
                    response = await self.port.call(params)
                except Exception as err:  # noqa: BLE001
                    response = {"resultado": "failed", "detalle": {"error": str(err)}}
                finally:
                    inc.calling_number = ""
                result = str(response.get("resultado") or "failed")
                detail = response.get("detalle") or {}
                dialstatus = str(detail.get("dialstatus") or "").upper()
                if dialstatus not in TRUNK_DOWN or retry == retries or inc.stop.is_set():
                    break
                # A one-off failure is not a line down: try again shortly.
                inc.add("retry", f"La llamada a {contact['name']} no sale: se reintenta", f"Asterisk: {dialstatus}")
                if await self._wait(inc, LINE_RETRY_S):
                    break
                inc.calling_number = contact["phone"]

            if inc.line_down and dialstatus not in TRUNK_DOWN and result != "failed":
                inc.line_down = False
                inc.add("trunk_ok", "La línea vuelve a funcionar: se vuelve a llamar")
            if inc.stop.is_set():
                # The incident was already settled (disarmed, acknowledged by someone
                # else, cancelled) while this call was going on: record how the call
                # ended, but it changes nothing.
                inc.add("after", f"Termina la llamada a {contact['name']}", AFTER_TEXT.get(result, result))
                return "stopped"

            if result == "confirmed":
                how = {"dtmf": "con la tecla", "voice": "por voz"}.get(str(detail.get("confirmed_by") or ""), "")
                inc.add("confirmed", f"{contact['name']} confirma el aviso" if test else f"{contact['name']} ha aceptado gestionar la alarma",
                        f"Por llamada {how}".strip())
                if test:
                    inc.status = "acknowledged"
                    inc.acknowledged_by, inc.ack_via = contact["name"], "llamada"
                else:
                    await self._acknowledge(inc, contact["name"], "llamada", from_call=True)
                return "confirmed"
            if dialstatus in TRUNK_DOWN:
                if test:
                    inc.add("trunk_down", "La línea telefónica no funciona", f"Asterisk: {dialstatus}")
                    inc.status = "trunk_down"
                    return "trunk_down"
                await self._mark_line_down(inc, f"Asterisk: {dialstatus}")
                return await self._alert_without_line(inc, contact)
            name = contact["name"]
            silent = detail.get("ended_by") == "silence"
            text = {
                "BUSY": f"{name} rechaza la llamada o comunica",
                "NOANSWER": f"{name} no contesta",
            }.get(dialstatus) or {
                "answered": f"{name} contesta pero no responde" if silent else f"{name} contesta pero no confirma",
                "voicemail": f"Salta el buzón de voz de {name}",
                "no_answer": f"{name} no contesta",
                "cancelled": f"Llamada a {name} retirada",
                "resolved": f"Llamada a {name} cerrada",
                "timeout": f"Sin respuesta del Bridge en la llamada a {name}",
            }.get(result, f"Falla la llamada a {name}")
            extra = []
            if dialstatus:
                extra.append(f"Asterisk: {dialstatus}")
            if silent:
                extra.append("Nadie habló: el asistente colgó tras volver a preguntar")
            if result == "voicemail":
                extra.append("Mensaje dejado en el buzón" if detail.get("voicemail_message_left") else "Sin mensaje en el buzón")
                if detail.get("detected_by") == "amd":
                    extra.append("Detectado por Asterisk")
            inc.add(result, text, " · ".join(extra))
            if result not in ("cancelled", "resolved") and contact["name"] not in inc.not_answered:
                inc.not_answered.append(contact["name"])
            if attempt < contact["attempts"] and await self._wait(inc, contact["retry_wait"]):
                return "stopped"
        return "not_confirmed"

    # ---------------------------------------------------- restarts
    def persist(self, inc: Incident | None = None) -> None:
        """Keep the open incident on disk (every change, and a heartbeat): if the App
        restarts, it carries on instead of starting over or getting lost."""
        inc = inc or self.current
        if self.state_path is None or inc is None or inc.kind not in CHAIN_KINDS:
            return
        if inc is not self.current:
            return
        try:
            if inc.closed:
                self.state_path.unlink(missing_ok=True)
                return
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(inc.snapshot(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as err:
            log.warning("Could not keep the open incident: %s", err)

    def restore(self) -> Incident | None:
        """At start: the incident that was open when the App stopped, if any."""
        if self.state_path is None:
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            inc = Incident.from_snapshot(data)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as err:
            log.warning("Ignoring the saved incident: %s", err)
            return None
        settings = self.store.settings()
        too_old = time.time() - inc.started > (settings["max_open_minutes"] + settings["post_minutes"] + 30) * 60
        if inc.closed or inc.kind not in CHAIN_KINDS or too_old:
            with contextlib.suppress(OSError):
                self.state_path.unlink()
            return None
        self.restored, self.restored_saved_at = inc, float(data.get("saved_at") or time.time())
        self.current = inc
        inc.on_change = self._changed
        log.info("[%s] open incident restored after a restart", inc.id)
        return inc

    async def reconcile_restored(self, alarm_state: str) -> None:
        if self.restored is not None:
            self.alarm_state = alarm_state
            await self._reconcile(alarm_state)

    async def _reconcile(self, state: str) -> None:
        """Carry on with the restored incident according to where the alarm is now."""
        inc, self.restored = self.restored, None
        if inc is None:
            return
        self.current = inc
        inc.on_change = self._changed
        now = time.time()
        inc.add("restart", f"La App se ha reiniciado: sin registro entre las {hhmm(self.restored_saved_at)} y las {hhmm(now)}")
        if inc.calling_number and not inc.chain_done:
            inc.add("after", f"La llamada a {inc.current_contact} se interrumpió con el reinicio")
        inc.calling_number, inc.current_call_id = "", ""
        if inc.kind == "alarm" and state in DISARMED_STATES and not inc.disarmed_at:
            await self._disarmed_later(inc, "Alarma desarmada mientras la App se reiniciaba")
        resume = (not inc.chain_done and not inc.acknowledged_by and not inc.disarmed_at
                  and inc.status not in ("cancelled", "acknowledged", "disarmed"))
        if resume:
            inc.add("resume", "Se retoman los avisos donde se quedaron")
            inc.chain_done = False
            inc.task = asyncio.get_running_loop().create_task(self._run(inc, resume=True))
        else:
            inc.chain_done = True
        if not inc.closed:
            if inc.record_until <= 0:
                inc.record_until = self._close_at(inc)
            if self.time_scale > 0:
                asyncio.get_running_loop().create_task(self._closer(inc))
        self.incidents.save(inc.to_dict())
        self._changed(inc)

    # ------------------------------------------------------ without line
    def _line_is_down(self, inc: Incident) -> bool:
        if inc.simulate_line_down:
            return True
        try:
            return bool(self.line_down_hint())
        except Exception:  # noqa: BLE001
            return False

    async def _mark_line_down(self, inc: Incident, detail: str) -> None:
        if inc.line_down:
            return
        inc.line_down = True
        inc.add("trunk_down", "La línea telefónica no funciona: se avisa por notificaciones", detail)
        await self._notify_household(inc, "¡Alarma! Las llamadas no funcionan",
                                     f"{inc.sensor} a las {hhmm(inc.started)}. La línea telefónica no funciona: "
                                     "se avisa por notificaciones.", ack_button=True)

    async def _alert_without_line(self, inc: Incident, contact: dict[str, Any]) -> str:
        """The contact's turn while the line is down: a burst (management contacts with
        the HA app), a Telegram message (others with Telegram), or the next contact."""
        name = contact["name"]
        if contact["can_manage"] and contact["person"] and contact["notify_service"]:
            for attempt in range(1, contact["attempts"] + 1):
                if inc.stop.is_set():
                    return "stopped"
                critical, normal = min(attempt, 3), min(10 * attempt, 30)
                suffix = f" ({attempt}/{contact['attempts']})" if contact["attempts"] > 1 else ""
                inc.add("burst", f"Avisando a {name} con una ráfaga de notificaciones{suffix}",
                        f"{critical} {'crítica' if critical == 1 else 'críticas'} y {normal} normales")
                inc.current_contact = name
                await self._burst(inc, contact, critical, normal)
                if inc.stop.is_set():
                    return "stopped"
                if attempt < contact["attempts"] and await self._wait(inc, contact["retry_wait"]):
                    return "stopped"
            if name not in inc.not_answered:
                inc.not_answered.append(name)
            return "notified"
        if contact["telegram_chat_id"] and self.telegram_alert is not None:
            inc.add("telegram", f"Avisando a {name} por Telegram", "Sin línea telefónica")
            try:
                await self.telegram_alert(inc, contact)
            except Exception as err:  # noqa: BLE001
                inc.add("notify_error", f"No se pudo avisar a {name} por Telegram", str(err)[:200])
            if inc.stop.is_set():
                return "stopped"
            if contact["retry_wait"] and await self._wait(inc, contact["retry_wait"]):
                return "stopped"
            return "notified"
        inc.add("skip", f"{name}: sin línea ni otro aviso posible, se pasa al siguiente")
        return "skipped"

    async def _burst(self, inc: Incident, contact: dict[str, Any], critical: int, normal: int) -> None:
        """Critical notifications every 2 s (each plays its sound), then normal ones
        every second (each vibrates). Each needs its own tag: one that replaces another
        is shown silently. They are all removed afterwards, leaving the main one."""
        service = contact["notify_service"]
        prefix = "SIMULACRO · " if inc.kind == "drill" else ""
        title = f"{prefix}¡Alarma! {inc.sensor}"
        message = f"Sin línea telefónica: te avisamos así. {hhmm(inc.started)}.\n{ACK_EXPLAIN}"
        actions = [{"action": f"{ACK_PREFIX}:{inc.id}:{contact['id']}", "title": ACK_BUTTON}]
        url = self.store.settings()["notify_url"]
        tags: list[str] = []
        try:
            for n in range(critical + normal):
                if inc.stop.is_set():
                    break
                inc.burst_seq += 1
                tag = f"acc_{inc.id}_b{inc.burst_seq}"
                tags.append(tag)
                data: dict[str, Any] = {"tag": tag, "actions": actions, "ttl": 0, "priority": "high"}
                if n < critical:
                    data.update(channel="alarm_stream", push={"sound": {"name": "default", "critical": 1, "volume": BURST_CRITICAL_VOLUME},
                                                               "interruption-level": "critical"})
                else:
                    data["push"] = {"sound": "default", "interruption-level": "time-sensitive"}
                if url:
                    data["url"] = data["clickAction"] = url
                with contextlib.suppress(Exception):
                    await self.port.notify(service, title, message, data)
                gap = BURST_CRITICAL_GAP_S if n < critical else BURST_NORMAL_GAP_S
                if n < critical + normal - 1 and await self._wait(inc, gap):
                    break
        finally:
            for tag in tags:
                with contextlib.suppress(Exception):
                    await self.port.notify(service, "", "clear_notification", {"tag": tag})

    async def on_line_recovered(self) -> None:
        """The line is back. During the chain the next contact is simply called again;
        if the chain already ended and the alarm is still open with nobody in charge,
        the calls resume from the contact that was next."""
        inc = self.current
        if not (inc and inc.kind in CHAIN_KINDS and inc.line_down and not inc.simulate_line_down):
            return
        if inc.closed or inc.acknowledged_by or inc.disarmed_at:
            return
        inc.line_down = False
        if self._active(inc) and not inc.chain_done:
            inc.add("trunk_ok", "La línea vuelve a funcionar: se vuelve a llamar")
            return
        inc.add("trunk_ok", "La línea vuelve a funcionar: se retoman las llamadas")
        await self._notify_household(inc, "Línea recuperada", "Se retoman las llamadas de la alarma.", ack_button=True)
        inc.chain_done = False
        inc.stop.clear()
        inc.task = asyncio.get_running_loop().create_task(self._run(inc, resume=True))
        self._changed(inc)

    async def _acknowledge(self, inc: Incident, who: str, via: str, from_call: bool = False) -> str:
        """Someone takes charge: no one else is called or alerted. Never disarms."""
        if inc.closed:
            return "closed"
        if inc.acknowledged_by:
            return "already"
        if inc.disarmed_at:
            return "disarmed"
        inc.acknowledged_by, inc.ack_via = who, via
        inc.acknowledged_at = time.time()
        inc.status = "acknowledged"
        if not from_call:
            inc.add("acknowledged", f"{who} ha aceptado gestionar la alarma", f"Desde {via}")
            text = ("Gracias. Has aceptado gestionar la alarma: ya no avisaremos a nadie más. La alarma sigue activa hasta que se desarme."
                    if who == inc.current_contact else f"Te informo de que {who} ha aceptado gestionar la alarma.")
            await self._tell_current_call(inc, text)
        inc.stop_reason = "acknowledged"
        inc.stop.set()
        self._save_late(inc)
        await self._notify_household(inc, "Alarma gestionada",
                                     f"{who} ha aceptado gestionar la alarma ({inc.sensor}, {hhmm(inc.started)}). "
                                     "Ya no se avisará a nadie más. La alarma sigue activa hasta que se desarme.",
                                     ack_button=False)
        return "ok"

    async def _halt(self, inc: Incident, status: str, text: str) -> None:
        if inc.stop.is_set():
            return  # already stopping (a second press, a late event...): nothing new to say
        self._finish(inc, status)
        inc.add(status, text)
        if status == "disarmed":
            inc.disarmed_at = time.time()
            await self._tell_current_call(inc, "Te informo de que la alarma se acaba de desarmar.")
            await self._notify_household(inc, "Alarma desarmada",
                                         f"Desarmada a los {int(time.time() - inc.started)} s ({inc.sensor}).", ack_button=False)
        elif inc.current_call_id and inc.calling_number:
            with contextlib.suppress(Exception):
                await self.port.bridge_hangup(inc.current_call_id)
        inc.stop_reason = status
        inc.stop.set()
        self.incidents.save(inc.to_dict())

    # ---------------------------------------------------------- closing
    def _close_at(self, inc: Incident) -> float:
        settings = self.store.settings()
        if inc.disarmed_at:
            return inc.disarmed_at + settings["post_minutes"] * 60
        return max(inc.started + settings["max_open_minutes"] * 60, time.time())

    async def _disarmed_later(self, inc: Incident, text: str) -> None:
        """Disarmed after the chain ended (someone had taken charge): the incident now
        closes after the minutes set in the settings."""
        if inc.disarmed_at or inc.closed:
            return
        inc.disarmed_at = time.time()
        if inc.status not in ("disarmed",):
            inc.status = "disarmed" if inc.status in ("waiting", "calling") else inc.status
        inc.add("disarmed", text)
        inc.record_until = self._close_at(inc)
        inc.wake.set()
        self._save_late(inc)
        if inc.kind == "alarm" or inc.kind == "drill":
            with contextlib.suppress(Exception):
                await self._notify_household(inc, "Alarma desarmada" if inc.kind == "alarm" else "Simulacro desarmado",
                                             f"Desarmada a los {fmt_elapsed(time.time() - inc.started)} ({inc.sensor}).",
                                             ack_button=False)
        if inc.record_until <= time.time():
            self._close(inc)

    async def _closer(self, inc: Incident) -> None:
        """Close the incident when its time comes (the time moves when it is disarmed)."""
        while not inc.closed:
            remaining = inc.record_until - time.time()
            if remaining <= 0:
                break
            inc.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(inc.wake.wait(), timeout=min(remaining, CLOSE_POLL_S))
        self._close(inc)

    def _close(self, inc: Incident, text: str = "") -> None:
        if inc.closed:
            return
        if not text:
            text = "Incidente cerrado" if inc.disarmed_at else "Incidente cerrado sin desarmar (tiempo máximo abierto)"
        inc.add("closed", text)
        inc.closed = True
        inc.record_until = 0.0
        inc.wake.set()
        inc.final = inc.to_dict()
        self.incidents.save(inc.final)
        self._changed(inc)

    async def _tell_current_call(self, inc: Incident, text: str) -> None:
        """Someone is on the phone: tell them. Still ringing: withdraw the call."""
        if not (inc.current_call_id and inc.calling_number):
            return
        with contextlib.suppress(Exception):
            if inc.call_answered:
                # resolve: the notice no longer needs confirming (Bridge 0.9+).
                await self.port.bridge_note(inc.current_call_id, text, resolve=True)
            else:
                await self.port.bridge_hangup(inc.current_call_id)

    async def _notify_household(self, inc: Incident, title: str, message: str, ack_button: bool = False) -> None:
        """Critical notification to every contact with the HA app (same tag: it is updated, not stacked)."""
        if inc.kind not in CHAIN_KINDS:
            return
        if inc.kind == "drill":
            title = "SIMULACRO · " + title
        for c in self._participants(inc):
            if not c["notify_service"]:
                continue
            data: dict[str, Any] = {
                "tag": f"acc_{inc.id}",
                "ttl": 0,
                "priority": "high",
                "channel": "alarm_stream",
                "push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}, "interruption-level": "critical"},
            }
            if ack_button:
                data["actions"] = [{"action": f"{ACK_PREFIX}:{inc.id}:{c['id']}", "title": ACK_BUTTON}]
            url = self.store.settings()["notify_url"]
            if url:
                data["url"] = url  # iOS: tapping the notification opens this dashboard view
                data["clickAction"] = url  # Android
            try:
                await self.port.notify(c["notify_service"], title, f"{message}\n{ACK_EXPLAIN}" if ack_button else message, data)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] notification to %s failed: %s", inc.id, c["name"], err)

    def _participants(self, inc: Incident) -> list[dict[str, Any]]:
        """Active contacts, in order; a drill only includes the chosen ones."""
        return [c for c in self.store.contacts()
                if c["active"] and (inc.only_contacts is None or c["id"] in inc.only_contacts)]

    # ------------------------------------------------------------ test
    async def _run_test(self, inc: Incident) -> None:
        contact = next((c for c in self.store.contacts() if c["id"] == inc.test_contact), None)
        if not contact:
            inc.add("error", "El contacto ya no existe")
            self._finish(inc, "failed")
            return
        inc.status = "calling"
        settings = self.store.settings()
        test_contact = {**contact, "attempts": 1}
        outcome = await self._call_contact(inc, test_contact, settings, test=True)
        if outcome == "not_confirmed":
            self._finish(inc, "no_answer")
