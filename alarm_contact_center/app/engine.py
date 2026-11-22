"""The alert chain: what happens from the moment the alarm triggers.

Pure asyncio and standard library. Everything that touches Home Assistant goes
through a ``Port`` (see ha_ws.py for the real one and the tests for a fake), so the
whole chain can be tested without Home Assistant, Asterisk or a phone.

Flow of a real incident:
1. Alarmo goes to ``triggered``: open an incident and notify the household's app
   right away (critical notification with a "Lo atiendo" button).
2. Wait ``call_delay`` seconds. If the alarm is disarmed (or someone presses the
   button) in the meantime, nobody is called.
3. Give the alarm priority: drop other queued calls and hang up any call in progress.
4. Call the active contacts with a phone, in order, through script.llamada_saliente
   (one attempt per script run; retries are done here so they can be stopped).
   The chain is circular: after the last contact it starts again from the first,
   for ``rounds`` passes (0 = until someone confirms, capped by max_chain_minutes).
5. Someone confirms (on the phone or with the app button): stop the chain, tell
   whoever is on the phone, and update everyone's notification. Never disarms.
6. The trunk is down (CHANUNAVAIL/CONGESTION): stop calling, say so in the
   notifications.

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

from .store import Store

log = logging.getLogger(__name__)

TRUNK_DOWN = {"CHANUNAVAIL", "CONGESTION"}
ACK_PREFIX = "ACC_ACK"
DISARMED_STATES = {"disarmed"}
TRIGGERED_STATE = "triggered"
TEST_PREFIX = "Esto es una prueba de Alarm Contact Center. "
DRILL_PREFIX = "Esto es un simulacro de alarma. "
CHAIN_KINDS = ("alarm", "drill")  # incidents that run the full chain
PROFILE_TEXT = {"Management": "modo gestión (con PIN)", "QueryOnly": "solo consulta"}


class Port(Protocol):
    """What the engine needs from Home Assistant."""

    async def call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run script.llamada_saliente with these fields and return its response."""

    async def notify(self, service: str, title: str, message: str, data: dict[str, Any]) -> None: ...

    async def bridge_note(self, call_id: str, text: str) -> None: ...

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

    def add(self, kind: str, text: str, detail: str = "") -> None:
        entry = {"t": now_iso(), "kind": kind, "text": text, "detail": detail}
        self.events.append(entry)
        log.info("[%s] %s%s", self.id, text, f" ({detail})" if detail else "")

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
            "ack_via": self.ack_via,
            "events": copy.deepcopy(self.events),
        }


class Engine:
    def __init__(self, store: Store, port: Port, incidents: IncidentLog, time_scale: float = 1.0) -> None:
        self.store = store
        self.port = port
        self.incidents = incidents
        self.time_scale = time_scale  # tests use 0 to skip real waiting
        self.current: Incident | None = None
        self.alarm_state = ""

    # ------------------------------------------------------------- inputs
    async def on_alarm_state(self, state: str, attributes: dict[str, Any] | None = None, restarted: bool = False) -> None:
        """Feed every state of the alarm_control_panel entity."""
        previous, self.alarm_state = self.alarm_state, state
        attributes = attributes or {}
        if state == TRIGGERED_STATE and previous != TRIGGERED_STATE:
            if self.current and self.current.kind == "alarm" and self._active(self.current):
                return
            await self._start_alarm(attributes, restarted)
        elif state in DISARMED_STATES and self.current and self._active(self.current) and self.current.kind == "alarm":
            await self._halt(self.current, "disarmed", "Alarma desarmada")

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
        inc = self.current
        if not inc or inc.id != incident_id or not self._active(inc):
            return
        contact = next((c for c in self.store.contacts() if c["id"] == contact_id), None)
        await self._acknowledge(inc, contact["name"] if contact else "Alguien", "la app")

    async def start_test(self, contact_id: str) -> dict[str, Any]:
        """Call one contact with a test message. Never while a real alarm is running."""
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

    async def start_drill(self, contact_ids: list[str], sensor_entity: str) -> dict[str, Any]:
        """Run the whole chain as if the alarm had triggered, without touching Alarmo.

        Only the chosen contacts are notified and called; messages say it is a drill.
        Does not take the phone line from other calls (no priority step).
        """
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
        names = join_names([c["name"] for c in self.store.contacts() if c["id"] in inc.only_contacts])
        inc.add("triggered", "Simulacro: alarma disparada", f"Sensor: {sensor} · participan {names}")
        self._launch(inc)
        return inc.to_dict()

    async def simulate_disarm(self) -> None:
        """Drill only: behave as if Alarmo had been disarmed."""
        inc = self.current
        if not inc or not self._active(inc) or inc.kind != "drill":
            raise RuntimeError("No hay ningún simulacro en curso.")
        if inc.stop.is_set():
            raise RuntimeError("El simulacro ya ha terminado: solo falta que acabe la llamada en curso.")
        await self._halt(inc, "disarmed", "Simulacro: alarma desarmada")

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
        self.current = inc
        inc.task = asyncio.get_running_loop().create_task(self._run(inc))
        self.incidents.save(inc.to_dict())

    async def _start_alarm(self, attributes: dict[str, Any], restarted: bool) -> None:
        # A test or a drill in progress gives way to the real alarm.
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

    async def _run(self, inc: Incident) -> None:
        try:
            if inc.kind in CHAIN_KINDS:
                await self._run_alarm(inc)
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
            final = inc.to_dict()
            final["active"] = False  # the task is still running at this point
            self.incidents.save(final)

    # ------------------------------------------------------------ alarm
    async def _run_alarm(self, inc: Incident) -> None:
        settings = self.store.settings()
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
            for contact in contacts:
                if inc.stop.is_set():
                    break
                if out_of_time():
                    timed_out = True
                    break
                outcome = await self._call_contact(inc, contact, settings)
                if outcome in ("confirmed", "trunk_down"):
                    break
            if inc.stop.is_set() or timed_out:
                break
            if round_no < rounds or not rounds:
                if await self._wait(inc, settings["round_pause_min"] * 60):
                    break

        if not inc.stop.is_set():
            self._finish(inc, "no_answer")
            inc.add("no_answer", "Nadie ha confirmado el aviso")
            await self._notify_household(inc, "Nadie ha confirmado", "La cadena de llamadas ha terminado sin confirmación.", ack_button=False)

    async def _call_contact(self, inc: Incident, contact: dict[str, Any], settings: dict[str, Any], test: bool = False) -> str:
        """Call one contact with its retries. Returns confirmed, trunk_down or not_confirmed."""
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
                "intentos": 1,
                "tiempo_respuesta": settings["answer_timeout"],
                "requiere_confirmacion": True,
                "confirmacion": contact["confirm"],
                "texto_confirmacion": settings["confirm_text"],
            }
            inc.current_contact, inc.calling_number, inc.current_call_id = contact["name"], contact["phone"], ""
            inc.requested_profile = params["perfil"]
            inc.call_answered = False
            suffix = f" ({attempt}/{contact['attempts']})" if contact["attempts"] > 1 else ""
            inc.add("call", f"Llamando a {contact['name']}{suffix}", PROFILE_TEXT[params["perfil"]].capitalize())
            self.incidents.save(inc.to_dict())
            try:
                response = await self.port.call(params)
            except Exception as err:  # noqa: BLE001
                response = {"resultado": "failed", "detalle": {"error": str(err)}}
            finally:
                inc.calling_number = ""
            result = str(response.get("resultado") or "failed")
            detail = response.get("detalle") or {}
            dialstatus = str(detail.get("dialstatus") or "").upper()

            if inc.stop.is_set():
                # The incident was already settled (disarmed, acknowledged by someone
                # else, cancelled) while this call was going on: record how the call
                # ended, but it changes nothing.
                inc.add("after", f"Termina la llamada a {contact['name']}",
                        {"confirmed": "Confirmó el aviso", "answered": "Contestó sin confirmar"}.get(result, result))
                return "stopped"

            if result == "confirmed":
                how = {"dtmf": "con la tecla", "voice": "por voz"}.get(str(detail.get("confirmed_by") or ""), "")
                inc.add("confirmed", f"{contact['name']} confirma el aviso", f"Por llamada {how}".strip())
                if test:
                    inc.status = "acknowledged"
                    inc.acknowledged_by, inc.ack_via = contact["name"], "llamada"
                else:
                    await self._acknowledge(inc, contact["name"], "llamada", from_call=True)
                return "confirmed"
            if dialstatus in TRUNK_DOWN:
                inc.add("trunk_down", "La línea telefónica no funciona", f"Asterisk: {dialstatus}")
                if not test:
                    self._finish(inc, "trunk_down")
                    inc.stop.set()
                    inc.stop_reason = "trunk_down"
                    await self._notify_household(inc, "¡Alarma! Las llamadas no funcionan",
                                                 f"{inc.sensor} a las {hhmm(inc.started)}. La línea telefónica no funciona: no se puede llamar a nadie.",
                                                 ack_button=True)
                else:
                    inc.status = "trunk_down"
                return "trunk_down"
            text = {
                "BUSY": f"{contact['name']} rechaza la llamada o comunica",
                "NOANSWER": f"{contact['name']} no contesta",
            }.get(dialstatus) or {
                "answered": f"{contact['name']} contesta pero no confirma",
                "no_answer": f"{contact['name']} no contesta",
                "cancelled": f"Llamada a {contact['name']} cancelada",
                "timeout": f"Sin respuesta del Bridge en la llamada a {contact['name']}",
            }.get(result, f"Falla la llamada a {contact['name']}")
            inc.add(result, text, f"Asterisk: {dialstatus}" if dialstatus else "")
            if result != "cancelled" and contact["name"] not in inc.not_answered:
                inc.not_answered.append(contact["name"])
            if attempt < contact["attempts"] and await self._wait(inc, contact["retry_wait"]):
                return "stopped"
        return "not_confirmed"

    async def _acknowledge(self, inc: Incident, who: str, via: str, from_call: bool = False) -> None:
        if inc.acknowledged_by or inc.stop.is_set():
            return
        inc.acknowledged_by, inc.ack_via = who, via
        inc.status = "acknowledged"
        if not from_call:
            inc.add("acknowledged", f"{who} atiende la alarma", f"Desde {via}")
        if not from_call:
            await self._tell_current_call(inc, f"Te informo de que {who} ya está atendiendo la alarma.")
        inc.stop_reason = "acknowledged"
        inc.stop.set()
        await self._notify_household(inc, "Alarma atendida", f"{who} está atendiendo la alarma ({inc.sensor}, {hhmm(inc.started)}).", ack_button=False)

    async def _halt(self, inc: Incident, status: str, text: str) -> None:
        if inc.stop.is_set():
            return  # already stopping (a second press, a late event...): nothing new to say
        self._finish(inc, status)
        inc.add(status, text)
        if status == "disarmed":
            await self._tell_current_call(inc, "Te informo de que la alarma se acaba de desarmar.")
            await self._notify_household(inc, "Alarma desarmada",
                                         f"Desarmada a los {int(time.time() - inc.started)} s ({inc.sensor}).", ack_button=False)
        elif inc.current_call_id and inc.calling_number:
            with contextlib.suppress(Exception):
                await self.port.bridge_hangup(inc.current_call_id)
        inc.stop_reason = status
        inc.stop.set()
        self.incidents.save(inc.to_dict())

    async def _tell_current_call(self, inc: Incident, text: str) -> None:
        """Someone is on the phone: tell them. Still ringing: withdraw the call."""
        if not (inc.current_call_id and inc.calling_number):
            return
        with contextlib.suppress(Exception):
            if inc.call_answered:
                await self.port.bridge_note(inc.current_call_id, text)
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
                data["actions"] = [{"action": f"{ACK_PREFIX}:{inc.id}:{c['id']}", "title": "Lo atiendo"}]
            try:
                await self.port.notify(c["notify_service"], title, message, data)
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
