"""Outbound calls: Home Assistant announces a call, Asterisk places it.

Flow
----
1. Home Assistant sends a command to the add-on's standard input
   (``hassio.addon_stdin``) with a call id (a UUID), the notice to read and a
   profile. Nothing listens on the network for this.
2. Home Assistant asks Asterisk to dial the phone; when it is answered, the
   dialplan connects it to the bridge with ``AudioSocket(<call id>, ...)``.
3. The bridge finds the call id in this registry and runs the call in outbound
   mode. If nobody answers before ``timeout`` seconds, the registration expires
   and the result ``no_answer`` is reported.

Profiles
--------
* ``QueryOnly``: questions only (read-only agent); changes are impossible,
  there is no PIN.
* ``Management``: questions right away; changes after a valid PIN (the global
  PIN or the personal PIN of the person called).

Confirmation
------------
The caller (an automation, the alarm) chooses per call what counts as the person
confirming the notice: ``implicit`` (they react to it), ``voice`` (an explicit
yes), ``dtmf`` (a key) or ``voice_or_dtmf``. The bridge only applies that rule
and reports how it was confirmed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import CONFIRM_KEYS, CONFIRM_MODES, PROFILES

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600
MAX_MESSAGE_CHARS = 1000
MAX_TEXT_CHARS = 300
MAX_NAME_CHARS = 80
# "realtime": OpenAI (with the local fallback if it fails); "local": straight to
# the local conversation (Home Assistant STT, Assist and TTS), no OpenAI at all.
ENGINES = ("realtime", "local")


class CommandError(ValueError):
    """A command from Home Assistant is not valid."""


@dataclass
class OutboundCall:
    call_id: str  # canonical UUID string
    message: str
    profile: str
    person: str = ""
    number: str = ""  # the number dialled (informative: reported in events)
    timeout: int = DEFAULT_TIMEOUT_S
    confirm: str = "implicit"
    confirm_key: str = "1"
    confirm_text: str = ""
    assistant_name: str = ""  # empty: the add-on's assistant_name
    engine: str = "realtime"
    created: float = field(default_factory=time.monotonic)

    @property
    def short_id(self) -> str:
        return self.call_id[:8]


def parse_command(data: Any) -> OutboundCall:
    """Validate an ``outbound`` command from Home Assistant."""
    if not isinstance(data, dict):
        raise CommandError("the command must be a JSON object")
    action = str(data.get("action", "")).strip()
    if action != "outbound":
        raise CommandError(f"unknown action {action!r}")
    try:
        call_id = str(uuid.UUID(str(data.get("call_id", "")).strip()))
    except ValueError:
        raise CommandError("call_id must be a UUID") from None
    message = str(data.get("message") or "").strip()
    if not message:
        raise CommandError("message is missing")
    profiles = {p.lower(): p for p in PROFILES}
    profile = profiles.get(str(data.get("profile") or "").strip().lower())
    if profile is None:
        raise CommandError(f"profile must be one of {', '.join(PROFILES)}")
    try:
        timeout = int(data.get("timeout") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        raise CommandError("timeout must be a number of seconds") from None
    confirm = str(data.get("confirm") or "implicit").strip().lower()
    if confirm not in CONFIRM_MODES:
        raise CommandError(f"confirm must be one of {', '.join(CONFIRM_MODES)}")
    confirm_key = str(data.get("confirm_key") or "1").strip()
    if len(confirm_key) != 1 or confirm_key not in CONFIRM_KEYS:
        raise CommandError("confirm_key must be one key: 0-9, * or #")
    engine = str(data.get("engine") or "realtime").strip().lower()
    if engine not in ENGINES:
        raise CommandError(f"engine must be one of {', '.join(ENGINES)}")
    return OutboundCall(
        call_id=call_id,
        engine=engine,
        message=message[:MAX_MESSAGE_CHARS],
        profile=profile,
        person=str(data.get("person") or "").strip()[:MAX_NAME_CHARS],
        number=str(data.get("number") or "").strip()[:32],
        timeout=max(10, min(timeout, MAX_TIMEOUT_S)),
        confirm=confirm,
        confirm_key=confirm_key,
        confirm_text=str(data.get("confirm_text") or "").strip()[:MAX_TEXT_CHARS],
        assistant_name=str(data.get("assistant_name") or "").strip()[:MAX_NAME_CHARS],
    )


ExpiredCallback = Callable[[OutboundCall], Awaitable[None]]


class Registry:
    """Outbound calls announced by Home Assistant and not answered yet."""

    def __init__(self, on_expired: ExpiredCallback | None = None) -> None:
        self._calls: dict[str, OutboundCall] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._on_expired = on_expired
        # Calls that expired or were cancelled: if Asterisk still connects one
        # of them, it must be hung up, never taken for an incoming call.
        self._retired: deque[str] = deque(maxlen=200)

    def __len__(self) -> int:
        return len(self._calls)

    def add(self, call: OutboundCall) -> None:
        self.discard(call.call_id)
        self._calls[call.call_id] = call
        self._timers[call.call_id] = asyncio.create_task(self._expire(call))
        log.info(
            "[%s] outbound call announced (profile %s%s, %ds to answer)",
            call.short_id,
            call.profile,
            f", person {call.person}" if call.person else "",
            call.timeout,
        )

    def take(self, call_id: str) -> OutboundCall | None:
        """Claim a call when Asterisk connects it; None if it is not outbound."""
        call = self._calls.pop(call_id, None)
        timer = self._timers.pop(call_id, None)
        if timer:
            timer.cancel()
        return call

    def discard(self, call_id: str) -> None:
        self.take(call_id)

    def pending(self) -> list[OutboundCall]:
        return list(self._calls.values())

    def cancel(self, call_id: str) -> OutboundCall | None:
        """Withdraw a call that has not been answered (it is retired)."""
        call = self.take(call_id)
        if call is not None:
            self._retired.append(call_id)
        return call

    def retired(self, call_id: str) -> bool:
        return call_id in self._retired

    def take_by_tail(self, tail: str) -> OutboundCall | None:
        """Claim a pending call by the end of its id (the dial status report
        carries the call id without its first 8 characters)."""
        for call_id in list(self._calls):
            if call_id[8:] == tail:
                call = self.take(call_id)
                self._retired.append(call_id)
                return call
        return None

    async def close(self) -> None:
        timers = list(self._timers.values())
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        self._calls.clear()
        self._timers.clear()

    async def _expire(self, call: OutboundCall) -> None:
        await asyncio.sleep(call.timeout)
        if self._calls.pop(call.call_id, None) is None:
            return
        self._timers.pop(call.call_id, None)
        self._retired.append(call.call_id)
        log.info("[%s] outbound call not answered", call.short_id)
        if self._on_expired:
            try:
                await self._on_expired(call)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] could not report the missed call: %r", call.short_id, err)


class CommandParser:
    """Splits a stream of text into JSON commands.

    Home Assistant JSON-encodes the ``input`` of ``hassio.addon_stdin``: an
    object arrives as an object, and a string arrives as a JSON string that
    itself contains the command. Commands may or may not end with a newline.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = json.JSONDecoder()

    def feed(self, text: str) -> list[Any]:
        self._buffer += text
        items: list[Any] = []
        while True:
            stripped = self._buffer.lstrip()
            if not stripped:
                self._buffer = ""
                return items
            try:
                value, end = self._decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                if "\n" in stripped:
                    # A complete line that is not JSON: skip it.
                    bad, _, rest = stripped.partition("\n")
                    log.warning("Ignoring a line from stdin that is not JSON: %.80r", bad)
                    self._buffer = rest
                    continue
                self._buffer = stripped  # wait for more data
                return items
            self._buffer = stripped[end:]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    log.warning("Ignoring a text command that is not JSON: %.80r", value)
                    continue
            items.append(value)


async def read_commands(
    reader: asyncio.StreamReader, handle: Callable[[Any], Awaitable[None]]
) -> None:
    """Read commands from the add-on's stdin until it closes."""
    parser = CommandParser()
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            log.debug("stdin closed: no more commands from Home Assistant")
            return
        for command in parser.feed(chunk.decode("utf-8", errors="replace")):
            try:
                await handle(command)
            except Exception as err:  # noqa: BLE001 - one bad command must not stop the reader
                log.warning("Command failed: %r", err)
