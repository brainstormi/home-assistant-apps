"""Periodic OpenAI check, published to Home Assistant as a binary sensor.

The check opens a Realtime session and closes it as soon as OpenAI confirms it
(``session.created``): no audio and no tokens are sent. That detects an invalid
key, exhausted credit (``insufficient_quota``), no access to the model and
outages, which a free endpoint such as the model list would not (it answers
even without credit). Real calls also update the state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

ENTITY_ID = "binary_sensor.asterisk_realtime_bridge_openai"
CHECK_TIMEOUT_S = 15.0


class _States(Protocol):
    async def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None: ...


def describe_error(err: BaseException) -> str:
    """A short reason from a connection error (HTTP status and OpenAI error code)."""
    response = getattr(err, "response", None)
    status = getattr(response, "status_code", None)
    body = getattr(response, "body", b"") or b""
    code = ""
    try:
        data = json.loads(body.decode("utf-8", "ignore")) if body else {}
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "")
    except (ValueError, AttributeError):
        pass
    if status:
        return f"HTTP {status}" + (f" {code}" if code else "")
    return type(err).__name__ + (f": {err}" if str(err) else "")


async def realtime_probe(connect: Callable[..., Any], url: str, api_key: str) -> tuple[bool, str]:
    """Open a Realtime session and close it at once. Returns (ok, reason)."""
    try:
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            open_timeout=10,
            max_size=None,
        ) as ws:
            async for raw in ws:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.created":
                    return True, ""
                if kind == "error":
                    err = event.get("error") or {}
                    return False, str(err.get("code") or err.get("type") or "error")
            return False, "closed by OpenAI"
    except Exception as err:  # noqa: BLE001
        return False, describe_error(err)


class OpenAIHealth:
    def __init__(
        self,
        states: _States,
        probe: Callable[[], Awaitable[tuple[bool, str]]],
        interval_minutes: int,
    ) -> None:
        self._states = states
        self._probe = probe
        self.interval_minutes = interval_minutes
        self.ok: bool | None = None
        self.reason = ""
        self._last_check: datetime | None = None

    async def check(self) -> bool:
        try:
            ok, reason = await asyncio.wait_for(self._probe(), CHECK_TIMEOUT_S)
        except asyncio.TimeoutError:
            ok, reason = False, "timeout"
        await self.report(ok, reason, source="check")
        return ok

    async def report(self, ok: bool, reason: str = "", source: str = "call") -> None:
        changed = ok != self.ok
        self.ok, self.reason = ok, reason
        self._last_check = datetime.now(timezone.utc)
        if changed:
            if ok:
                log.info("OpenAI available")
            else:
                log.warning("OpenAI NOT available (%s)", reason)
        await self.publish(source)

    async def publish(self, source: str) -> None:
        attributes: dict[str, Any] = {
            "friendly_name": "Asterisk Realtime Bridge: OpenAI",
            "device_class": "connectivity",
            "icon": "mdi:cloud-check" if self.ok else "mdi:cloud-alert",
            "reason": self.reason or None,
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "source": source,
            "check_interval_minutes": self.interval_minutes,
        }
        try:
            await self._states.set_state(ENTITY_ID, "on" if self.ok else "off", attributes)
        except Exception as err:  # noqa: BLE001
            log.debug("Could not publish the OpenAI state: %r", err)

    async def run(self) -> None:
        """Check now and then every ``interval_minutes`` (0 = only at start)."""
        while True:
            await self.check()
            if self.interval_minutes <= 0:
                return
            await asyncio.sleep(self.interval_minutes * 60)
