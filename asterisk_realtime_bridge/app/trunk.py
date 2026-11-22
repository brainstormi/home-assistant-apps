"""Real test of the SIP trunk: call our own line and see the call come back.

Flow (see DOCS, "Trunk test"):

1. Home Assistant sends ``{"action": "trunk_test", "test_id": ..., "number":
   <our own line>, "timeout": 20}`` to the add-on's stdin.
2. The bridge asks Asterisk (through Home Assistant's ``hassio.addon_stdin`` to
   the Asterisk app) to run ``Local/<number>_<test uuid>@bridge-trunk-test``.
   That context marks the test as pending (global variables), dials our own
   number through the trunk, and reports the ``DIALSTATUS`` to the bridge.
3. If the call comes back in through the trunk, the incoming context sees the
   caller id of our own line while a test is pending, reports the arrival to the
   bridge and hangs up WITHOUT answering (nothing rings, no call is set up).

Both reports use the AudioSocket UUID, as the dial status report does::

    7e5700CC-<test uuid without its first 8 characters>   dial status (CC as in DIAL_STATUSES)
    7e57a100-<test uuid without its first 8 characters>   the call came back

Result: ``ok`` (it went out and came back), ``out_only`` (the operator took it:
BUSY, NOANSWER, ANSWER... but it did not come back; some operators reject calls
to the line itself) or ``failed`` (it never went out: CHANUNAVAIL,
CONGESTION... or no report at all).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .dialplan import DIAL_STATUSES

log = logging.getLogger(__name__)

PREFIX = "7e57"
DIAL_MARK = "00"
ARRIVAL_MARK = "a1"
EVENT_TYPE = "asterisk_realtime_bridge_trunk"
DEFAULT_TIMEOUT_S = 20
# The operator accepted the call: the way out works.
ACCEPTED = {"BUSY", "NOANSWER", "ANSWER", "CANCEL"}
# After the dial status, how long a late arrival is still waited for.
ARRIVAL_GRACE_S = 2.0
_TEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def trunk_report(call_uuid: str) -> tuple[str, str, str] | None:
    """("dial", status, tail) or ("arrived", "", tail) for a trunk test report."""
    text = call_uuid.lower()
    if not text.startswith(PREFIX):
        return None
    mark, code, tail = text[4:6], text[6:8], text[8:]
    if mark == ARRIVAL_MARK and code == "00":
        return "arrived", "", tail
    if mark == DIAL_MARK and code.isdigit():
        index = int(code)
        status = DIAL_STATUSES[index - 1] if 1 <= index <= len(DIAL_STATUSES) else "UNKNOWN"
        return "dial", status, tail
    return None


def result_for(arrived: bool, dialstatus: str) -> str:
    if arrived:
        return "ok"
    return "out_only" if dialstatus in ACCEPTED else "failed"


class TrunkTestError(ValueError):
    pass


@dataclass
class TrunkTest:
    test_id: str
    number: str
    timeout: int = DEFAULT_TIMEOUT_S
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    arrived: bool = False
    dialstatus: str = ""
    started: float = field(default_factory=time.monotonic)

    @property
    def tail(self) -> str:
        return self.uuid[8:]


def parse_trunk_test(data: dict[str, Any]) -> TrunkTest:
    test_id = str(data.get("test_id") or "").strip()
    if not _TEST_ID_RE.match(test_id):
        raise TrunkTestError("test_id must be 1-64 letters, digits or _.:-")
    number = str(data.get("number") or "").strip()
    if not number or not all(ch.isdigit() or ch == "+" for ch in number) or len(number) > 20:
        raise TrunkTestError("number must be a phone number (digits)")
    try:
        timeout = int(data.get("timeout") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        raise TrunkTestError("timeout must be a number of seconds") from None
    return TrunkTest(test_id, number, max(5, min(timeout, 60)))


Fire = Callable[[str, dict[str, Any]], Awaitable[None]]
Originate = Callable[[str], Awaitable[None]]


class TrunkTester:
    """One trunk test at a time."""

    def __init__(self, fire: Fire, originate: Originate) -> None:
        self._fire = fire
        self._originate = originate
        self.current: TrunkTest | None = None
        self._done = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self, data: dict[str, Any]) -> None:
        try:
            test = parse_trunk_test(data)
        except TrunkTestError as err:
            log.warning("Trunk test refused: %s", err)
            await self._fire(EVENT_TYPE, {"test_id": str(data.get("test_id") or ""), "result": "failed",
                                          "dialstatus": "", "error": str(err)})
            return
        if self.current is not None:
            log.warning("Trunk test %s refused: test %s is still running", test.test_id, self.current.test_id)
            await self._fire(EVENT_TYPE, {"test_id": test.test_id, "result": "failed", "dialstatus": "",
                                          "error": "another trunk test is running"})
            return
        self.current = test
        self._done = asyncio.Event()
        self._task = asyncio.create_task(self._run(test))

    async def _run(self, test: TrunkTest) -> None:
        log.info("Trunk test %s: calling %s through the trunk", test.test_id, test.number)
        try:
            await self._originate(
                f"channel originate Local/{test.number}_{test.uuid}@bridge-trunk-test application Wait 1"
            )
        except Exception as err:  # noqa: BLE001
            log.warning("Trunk test %s: could not ask Asterisk to dial: %r", test.test_id, err)
            await self._finish(test, error="could not reach Asterisk")
            return
        try:
            await asyncio.wait_for(self._done.wait(), test.timeout)
        except asyncio.TimeoutError:
            log.info("Trunk test %s: no complete report in %ss", test.test_id, test.timeout)
        await self._finish(test)

    async def on_report(self, kind: str, status: str, tail: str) -> None:
        test = self.current
        if test is None or tail != test.tail:
            log.debug("Trunk test report (%s) for no pending test", kind)
            return
        if kind == "arrived":
            test.arrived = True
            log.info("Trunk test %s: the call came back in", test.test_id)
            if test.dialstatus:
                self._done.set()
        else:
            test.dialstatus = status
            log.info("Trunk test %s: dial status %s", test.test_id, status)
            if test.arrived:
                self._done.set()
            else:
                asyncio.get_running_loop().call_later(ARRIVAL_GRACE_S, self._done.set)

    async def _finish(self, test: TrunkTest, error: str = "") -> None:
        if self.current is not test:
            return
        self.current = None
        result = result_for(test.arrived, test.dialstatus)
        data = {"test_id": test.test_id, "result": result, "dialstatus": test.dialstatus,
                "arrived": test.arrived, "duration": round(time.monotonic() - test.started, 1)}
        if error:
            data["error"] = error
        log.info("Trunk test %s: %s%s", test.test_id, result,
                 f" ({test.dialstatus})" if test.dialstatus else "")
        await self._fire(EVENT_TYPE, data)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
