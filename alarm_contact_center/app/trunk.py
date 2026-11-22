"""Trunk monitoring: is the phone line able to call out right now?

Two independent signals:

1. Registration. The SIP trunk and Asterisk's AMI connection are binary_sensors in
   Home Assistant. If either stays off for ``trunk_alert_after_min`` minutes, the
   household is told (and again when it comes back). Registered does not prove that
   calls go out, so there is also:

2. A real test call. On a schedule (or from the panel) the App asks the Asterisk
   Realtime Bridge to call the line's own number through the trunk
   (``trunk_test`` command). The call goes out to the operator and comes back in;
   the Bridge reports with an ``asterisk_realtime_bridge_trunk`` event:

       {"test_id": "...", "result": "ok" | "out_only" | "failed", "dialstatus": "..."}

   ``ok``: it went out and came back. ``out_only``: the operator took it (BUSY,
   NOANSWER...) but it did not come back. ``failed``: it never left
   (CHANUNAVAIL, CONGESTION...). No event at all means the Bridge does not support
   the test (or is down): that is shown, but only alerts once the Bridge has
   answered a test before, so an old Bridge does not cause a daily false alarm.

Messages are in Spanish: they are read by the household.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from .engine import IncidentLog, now_iso
from .store import Store

log = logging.getLogger(__name__)

TRUNK_EVENT = "asterisk_realtime_bridge_trunk"
TEST_TIMEOUT_S = 90
POSTPONE_S = 600  # the line is busy with an alarm, a drill or a test call
OK_RESULTS = ("ok", "out_only")
RESULT_TEXT = {
    "ok": "La llamada sale y vuelve a entrar",
    "out_only": "La llamada sale, pero no vuelve a entrar",
    "failed": "La llamada no sale",
    "timeout": "El Bridge no responde a la prueba",
}


class TrunkPort(Protocol):
    async def bridge_command(self, command: dict[str, Any]) -> None: ...

    async def notify(self, service: str, title: str, message: str, data: dict[str, Any]) -> None: ...


def next_run(now: dt.datetime, mode: str, hour: int) -> dt.datetime | None:
    """When the next scheduled test is due (local time). None when tests are off."""
    if mode == "off":
        return None
    minute = 7  # not on the hour: plenty of other things run at :00
    if mode == "every_6h":
        slots = sorted({(hour + 6 * i) % 24 for i in range(4)})
        for day in range(2):
            base = (now + dt.timedelta(days=day)).replace(minute=minute, second=0, microsecond=0)
            for h in slots:
                candidate = base.replace(hour=h)
                if candidate > now:
                    return candidate
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if mode == "weekly":
        candidate += dt.timedelta(days=(0 - candidate.weekday()) % 7)  # Mondays
        if candidate <= now:
            candidate += dt.timedelta(days=7)
        return candidate
    if candidate <= now:  # daily
        candidate += dt.timedelta(days=1)
    return candidate


class TrunkMonitor:
    def __init__(self, store: Store, port: TrunkPort, incidents: IncidentLog, engine: Any,
                 state_path: str | os.PathLike[str], time_scale: float = 1.0) -> None:
        self.store = store
        self.port = port
        self.incidents = incidents
        self.engine = engine
        self.path = Path(state_path)
        self.time_scale = time_scale
        self._pending: dict[str, asyncio.Future] = {}
        self._down: dict[str, dict[str, Any]] = {}  # entity_id -> {"since", "task", "alerted"}
        self._testing = False
        self.state = self._load()

    # ---------------------------------------------------------------- state
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"last_test": None, "bridge_answers": False, "test_failing": False, "entities": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def status(self) -> dict[str, Any]:
        settings = self.store.settings()
        nxt = next_run(dt.datetime.now().astimezone(), settings["trunk_test"], settings["trunk_test_hour"])
        down = {eid: d["since"] for eid, d in self._down.items()}
        return {
            "last_test": self.state.get("last_test"),
            "bridge_answers": bool(self.state.get("bridge_answers")),
            "test_failing": bool(self.state.get("test_failing")),
            "testing": self._testing,
            "number_set": bool(settings["trunk_test_number"]),
            "next_test": nxt.isoformat(timespec="minutes") if nxt and settings["trunk_test_number"] else None,
            "down_since": down,
        }

    # --------------------------------------------------------- notifications
    async def _tell_household(self, title: str, message: str) -> None:
        for c in self.store.contacts():
            if not (c["active"] and c["notify_service"]):
                continue
            data = {"tag": "acc_trunk", "push": {"interruption-level": "time-sensitive"}, "priority": "high", "ttl": 0}
            try:
                await self.port.notify(c["notify_service"], title, message, data)
            except Exception as err:  # noqa: BLE001
                log.warning("Trunk notification to %s failed: %s", c["name"], err)

    def _log(self, status: str, text: str, detail: str = "") -> None:
        entry = {"t": now_iso(), "kind": "trunk_down" if status == "failed" else "trunk_ok", "text": text, "detail": detail}
        self.incidents.save({
            "id": uuid.uuid4().hex[:10], "kind": "trunk", "sensor": "Línea telefónica",
            "sensor_entity": "", "started": entry["t"], "status": status, "active": False,
            "acknowledged_by": "", "ack_via": "", "events": [entry],
        })

    # ---------------------------------------------------------- registration
    def _label(self, entity_id: str) -> str:
        settings = self.store.settings()
        if entity_id == settings["ami_entity"]:
            return "la conexión con Asterisk"
        return "el registro de la línea telefónica"

    async def on_entity_state(self, entity_id: str, state: str) -> None:
        settings = self.store.settings()
        if entity_id not in (settings["trunk_entity"], settings["ami_entity"]):
            return
        if state == "on":
            down = self._down.pop(entity_id, None)
            if down:
                if down["task"] and not down["task"].done():
                    down["task"].cancel()
                if down["alerted"]:
                    minutes = max(1, int((time.time() - down["since_ts"]) / 60))
                    self._log("ok", f"Se recupera {self._label(entity_id)}", f"Tras {minutes} min caído")
                    await self._tell_household("Línea telefónica recuperada",
                                               f"Se ha recuperado {self._label(entity_id)} tras {minutes} min.")
            return
        if entity_id in self._down:
            return
        info = {"since": now_iso(), "since_ts": time.time(), "alerted": False, "task": None}
        self._down[entity_id] = info
        info["task"] = asyncio.get_running_loop().create_task(self._alert_later(entity_id, settings["trunk_alert_after_min"]))

    async def _alert_later(self, entity_id: str, minutes: int) -> None:
        await asyncio.sleep(minutes * 60 * self.time_scale)
        info = self._down.get(entity_id)
        if not info:
            return
        info["alerted"] = True
        what = self._label(entity_id)
        self._log("failed", f"Falla {what}", f"Desde las {time.strftime('%H:%M', time.localtime(info['since_ts']))}")
        await self._tell_household("Línea telefónica caída",
                                   f"Falla {what} desde hace {minutes} min. Si salta la alarma no se podrá llamar a nadie; "
                                   "las notificaciones sí llegan.")

    # ------------------------------------------------------------ the test
    async def on_bridge_event(self, data: dict[str, Any]) -> None:
        fut = self._pending.get(str(data.get("test_id") or ""))
        if fut and not fut.done():
            fut.set_result(data)

    def _line_busy(self) -> bool:
        cur = getattr(self.engine, "current", None)
        return bool(cur and cur.task is not None and not cur.task.done())

    async def run_test(self, manual: bool = False) -> dict[str, Any]:
        settings = self.store.settings()
        number = settings["trunk_test_number"]
        if not number:
            raise RuntimeError("Falta el número propio de la línea en Ajustes → Vigilancia de la troncal.")
        if self._testing:
            raise RuntimeError("Ya hay una prueba de la troncal en curso.")
        if self._line_busy():
            raise RuntimeError("La línea está ocupada con un aviso o una prueba: inténtalo cuando termine.")
        self._testing = True
        test_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[test_id] = fut
        started = now_iso()
        try:
            await self.port.bridge_command({"action": "trunk_test", "test_id": test_id, "number": number, "timeout": 20})
            data = await asyncio.wait_for(fut, TEST_TIMEOUT_S * self.time_scale if self.time_scale else 0.05)
            result = str(data.get("result") or "failed")
            if result not in ("ok", "out_only", "failed"):
                result = "failed"
            dialstatus = str(data.get("dialstatus") or "")
        except asyncio.TimeoutError:
            result, dialstatus = "timeout", ""
        except Exception as err:  # noqa: BLE001 - e.g. Home Assistant unreachable
            log.warning("Trunk test could not be started: %s", err)
            result, dialstatus = "timeout", ""
        finally:
            self._pending.pop(test_id, None)
            self._testing = False

        if result != "timeout":
            self.state["bridge_answers"] = True
        test = {"t": started, "result": result, "dialstatus": dialstatus, "manual": manual,
                "text": RESULT_TEXT[result] + (f" (Asterisk: {dialstatus})" if dialstatus else "")}
        self.state["last_test"] = test
        await self._after_test(result, test)
        self._save()
        log.info("Trunk test: %s", test["text"])
        return test

    async def _after_test(self, result: str, test: dict[str, Any]) -> None:
        was_failing = bool(self.state.get("test_failing"))
        # A missing answer only counts once we know the Bridge does answer tests.
        failing = result == "failed" or (result == "timeout" and self.state.get("bridge_answers"))
        if failing and not was_failing:
            self.state["test_failing"] = True
            self._log("failed", "Falla la prueba de la línea telefónica", test["text"])
            await self._tell_household("Falla la prueba de la línea",
                                       f"{test['text']}. Si salta la alarma puede que no se pueda llamar a nadie.")
        elif result in OK_RESULTS and was_failing:
            self.state["test_failing"] = False
            self._log("ok", "La prueba de la línea vuelve a funcionar", test["text"])
            await self._tell_household("Línea telefónica recuperada", f"La prueba vuelve a funcionar: {test['text'].lower()}.")

    async def scheduler(self) -> None:
        """Run the scheduled tests forever. Re-reads the settings every minute."""
        while True:
            settings = self.store.settings()
            now = dt.datetime.now().astimezone()
            nxt = next_run(now, settings["trunk_test"], settings["trunk_test_hour"])
            if nxt is None or not settings["trunk_test_number"]:
                await asyncio.sleep(60)
                continue
            wait = (nxt - now).total_seconds()
            if wait > 60:
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(max(0.0, wait))
            while self._line_busy() or self._testing:
                await asyncio.sleep(POSTPONE_S)
            with contextlib.suppress(Exception):
                await self.run_test()
            await asyncio.sleep(61)  # never twice in the same slot
