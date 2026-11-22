"""Real-time link with Home Assistant over its WebSocket API (through the Supervisor).

- Listens to the alarm entity (subscribe_trigger), the Bridge call events and the
  mobile app notification buttons, and feeds them to the Engine.
- Implements the Engine's Port: running the call script with its response,
  notifications, and the Bridge's note/hangup commands.
- Reconnects forever with backoff. On every (re)connection it reads the alarm
  state, so an alarm that triggered while the App was restarting is not missed.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from typing import Any, Awaitable, Callable

import aiohttp

from .engine import Engine
from .store import Store, bridge_addon

log = logging.getLogger(__name__)

WS_URL = os.environ.get("HA_WS_URL", "http://supervisor/core/websocket")
CALL_TIMEOUT_S = 1500  # the call script itself gives up after answer time + 900 s


class HAError(Exception):
    pass


class HAConnection:
    def __init__(self, token: str | None = None, url: str = WS_URL) -> None:
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self.url = url
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subscriptions: dict[int, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        self._send_lock = asyncio.Lock()
        self.connected = asyncio.Event()

    async def run(self, on_connect: Callable[[], Awaitable[None]]) -> None:
        backoff = 1
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(self.url, heartbeat=30, max_msg_size=16 * 1024 * 1024) as ws:
                        await self._authenticate(ws)
                        self._ws = ws
                        self.connected.set()
                        backoff = 1
                        log.info("Connected to Home Assistant")
                        reader = asyncio.create_task(self._read(ws))
                        try:
                            await on_connect()
                            await reader
                        finally:
                            reader.cancel()
                except (aiohttp.ClientError, HAError, asyncio.TimeoutError, OSError) as err:
                    log.warning("Home Assistant connection lost: %s", err)
                finally:
                    self.connected.clear()
                    self._ws = None
                    self._subscriptions.clear()
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(HAError("connection lost"))
                    self._pending.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = await ws.receive_json(timeout=10)
        if msg.get("type") != "auth_required":
            raise HAError(f"unexpected greeting: {msg.get('type')}")
        await ws.send_json({"type": "auth", "access_token": self.token})
        msg = await ws.receive_json(timeout=10)
        if msg.get("type") != "auth_ok":
            raise HAError("authentication failed")

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            data = json.loads(raw.data)
            for msg in data if isinstance(data, list) else [data]:
                mid = msg.get("id")
                if msg.get("type") == "event" and mid in self._subscriptions:
                    handler = self._subscriptions[mid]
                    asyncio.create_task(self._safe(handler, msg.get("event") or {}))
                elif msg.get("type") == "result" and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if fut.done():
                        continue
                    if msg.get("success"):
                        fut.set_result(msg.get("result"))
                    else:
                        err = msg.get("error") or {}
                        fut.set_exception(HAError(f"{err.get('code')}: {err.get('message')}"))

    @staticmethod
    async def _safe(handler: Callable[[dict[str, Any]], Awaitable[None]], event: dict[str, Any]) -> None:
        try:
            await handler(event)
        except Exception:  # noqa: BLE001
            log.exception("Error handling a Home Assistant event")

    async def command(self, payload: dict[str, Any], timeout: float = 30) -> Any:
        ws = self._ws
        if ws is None:
            raise HAError("not connected")
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        async with self._send_lock:
            await ws.send_json({**payload, "id": mid})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def subscribe(self, payload: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        ws = self._ws
        if ws is None:
            raise HAError("not connected")
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        self._subscriptions[mid] = handler  # register first: events may arrive right after the result
        async with self._send_lock:
            await ws.send_json({**payload, "id": mid})
        try:
            await asyncio.wait_for(fut, 30)
        except Exception:
            self._subscriptions.pop(mid, None)
            raise

    async def call_service(self, domain: str, service: str, data: dict[str, Any] | None = None,
                           target: dict[str, Any] | None = None, return_response: bool = False,
                           timeout: float = 30) -> Any:
        payload: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service, "service_data": data or {}}
        if target:
            payload["target"] = target
        if return_response:
            payload["return_response"] = True
        return await self.command(payload, timeout)


class HAPort:
    """The Engine's Port on top of a real Home Assistant."""

    def __init__(self, conn: HAConnection, store: Store) -> None:
        self.conn = conn
        self.store = store

    async def call(self, params: dict[str, Any]) -> dict[str, Any]:
        # If Home Assistant is restarting, wait for it instead of failing every
        # contact in a row and burning through the chain.
        await asyncio.wait_for(self.conn.connected.wait(), timeout=300)
        script = self.store.settings()["call_script"]
        domain, name = script.split(".", 1)
        result = await self.conn.call_service(domain, name, params, return_response=True, timeout=CALL_TIMEOUT_S)
        response = (result or {}).get("response") or {}
        return response if isinstance(response, dict) else {}

    async def notify(self, service: str, title: str, message: str, data: dict[str, Any]) -> None:
        await self.conn.call_service("notify", service, {"title": title, "message": message, "data": data})

    async def _bridge(self, command: dict[str, Any]) -> None:
        addon = bridge_addon(self.store.settings())
        if not addon:
            raise HAError("Bridge App id unknown: set it in Ajustes")
        await self.conn.call_service("hassio", "addon_stdin", {"addon": addon, "input": command})

    async def bridge_note(self, call_id: str, text: str) -> None:
        await self._bridge({"action": "note", "call_id": call_id, "text": text})

    async def bridge_hangup(self, call_id: str = "") -> None:
        await self._bridge({"action": "hangup", "call_id": call_id})

    async def stop_queued_calls(self) -> None:
        await self.conn.call_service("script", "turn_off", target={"entity_id": self.store.settings()["call_script"]})

    async def friendly_name(self, entity_id: str) -> str:
        states = await self.conn.command({"type": "get_states"})
        for s in states or []:
            if s.get("entity_id") == entity_id:
                return (s.get("attributes") or {}).get("friendly_name") or entity_id
        return entity_id


async def wire(conn: HAConnection, engine: Engine, store: Store) -> None:
    """(Re)subscribe everything after each connection and catch up on the alarm state."""
    alarm_entity = store.settings()["alarm_entity"]

    async def on_alarm(event: dict[str, Any]) -> None:
        to_state = ((event.get("variables") or {}).get("trigger") or {}).get("to_state") or {}
        if to_state:
            await engine.on_alarm_state(str(to_state.get("state") or ""), to_state.get("attributes") or {})

    async def on_bridge(event: dict[str, Any]) -> None:
        await engine.on_bridge_event(event.get("data") or {})

    async def on_action(event: dict[str, Any]) -> None:
        await engine.on_notification_action(str((event.get("data") or {}).get("action") or ""))

    await conn.subscribe({"type": "subscribe_trigger", "trigger": {"platform": "state", "entity_id": alarm_entity}}, on_alarm)
    await conn.subscribe({"type": "subscribe_events", "event_type": "asterisk_realtime_bridge_call"}, on_bridge)
    await conn.subscribe({"type": "subscribe_events", "event_type": "mobile_app_notification_action"}, on_action)

    # Catch up: the alarm may have triggered while we were disconnected or restarting.
    states = await conn.command({"type": "get_states"})
    current = next((s for s in states or [] if s.get("entity_id") == alarm_entity), None)
    if current:
        first_time = engine.alarm_state == ""
        await engine.on_alarm_state(str(current.get("state") or ""), current.get("attributes") or {}, restarted=first_time)
    else:
        log.warning("Alarm entity %s not found", alarm_entity)
