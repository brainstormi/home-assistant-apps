"""AudioSocket server: accepts calls from Asterisk and connects them to OpenAI."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
import uuid
from typing import Any

import aiohttp
from websockets.asyncio.client import connect

from . import __version__
from .audiosocket import FRAME_BYTES, FRAME_MS, KIND_AUDIO, KIND_HANGUP, KIND_UUID, pack_frame, read_frame
from .config import DEPRECATED_MODELS, PROFILE_INBOUND, Config, ConfigError, load
from .dialplan import caller_number, dial_report, result_for, voicemail_report
from .fallback import Fallback
from .ha import HomeAssistant
from .health import OpenAIHealth, describe_error, realtime_probe
from .outbound import CommandError, OutboundCall, Registry, parse_command, read_commands
from .pins import MODE_OFF, CallAuth
from .session import CallSession, call_auth, keepalive
from .trunk import TrunkTester, trunk_report
from .local import LocalConversation
from .status import HomeStatus
from .stt import SttTest

log = logging.getLogger("bridge")

# Event fired on the Home Assistant bus for call results.
EVENT_TYPE = "asterisk_realtime_bridge_call"
PIN_ENTITY_ID = "sensor.asterisk_realtime_bridge_pin"
PIN_STATUS_MINUTES = 10
PIN_MODE_FILE = "/data/pin_mode"
MAX_NOTE_CHARS = 500


class LiveCall:
    """A call in progress: its task (to hang it up) and who can take a note."""

    def __init__(self, task: asyncio.Task | None) -> None:
        self.task = task
        self.handler: Any = None  # CallSession or LocalConversation, once running
        self.hung_up = False


class Bridge:
    def __init__(self, cfg: Config, http: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.home = HomeAssistant(
            http,
            cfg.ha_api_urls,
            cfg.ha_token,
            cfg.conversation_agent,
            cfg.language,
            cfg.fallback_agent,
        )
        # Read-only view of the home for outbound calls without a PIN.
        self.status = HomeStatus(self.home, cfg.query_include, cfg.query_exclude)
        self.registry = Registry(on_expired=self._report_missed)
        self._active = 0
        self._tasks: set[asyncio.Task] = set()
        # Fallback when OpenAI is unavailable (Home Assistant TTS, e.g. Piper).
        self.fallback = Fallback(self.home, cfg.fallback_tts, cfg.language, cfg.fallback_tts_voice)
        self.health = OpenAIHealth(self.home, self._probe_openai, cfg.openai_check_minutes)
        self.stt_test: SttTest | None = None
        self.live: dict[str, LiveCall] = {}
        self.trunk = TrunkTester(self._fire_event, self._asterisk)
        self.people: list[str] = []  # person.* entities in Home Assistant

    @property
    def realtime_url(self) -> str:
        return f"{self.cfg.realtime_url}?model={self.cfg.model}"

    async def _probe_openai(self) -> tuple[bool, str]:
        return await realtime_probe(connect, self.realtime_url, self.cfg.openai_api_key)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.registry.close()
        await self.trunk.close()

    # ------------------------------------------------------- HA commands
    async def on_command(self, data: Any) -> None:
        """A command from Home Assistant (add-on stdin)."""
        action = data.get("action") if isinstance(data, dict) else None
        if action == "note":
            await self.note(str(data.get("call_id") or ""), str(data.get("text") or ""),
                            bool(data.get("resolve")))
            return
        if action == "hangup":
            await self.hangup(str(data.get("call_id") or ""))
            return
        if action == "trunk_test":
            await self.trunk.start(data)
            return
        if isinstance(data, dict) and data.get("action") == "stt_test":
            engine = str(data.get("engine") or "stt.faster_whisper")
            voice = self.fallback if self.fallback.enabled else None
            self.stt_test = SttTest(self.home, engine, str(data.get("language") or self.cfg.language), voice)
            self.stt_test.arm()
            return
        try:
            call = parse_command(data)
        except CommandError as err:
            log.warning("Rejected command from Home Assistant: %s", err)
            call_id = data.get("call_id") if isinstance(data, dict) else None
            if call_id:
                await self._fire(
                    {"call_id": str(call_id), "direction": "outbound", "stage": "ended",
                     "result": "failed", "error": str(err)}
                )
            return
        if call.engine == "local" and not self.fallback.enabled:
            log.warning("[%s] engine local needs fallback_tts; call not placed", call.short_id)
            await self._fire({**self._event(call, "ended", "failed"),
                              "error": "engine local needs fallback_tts"})
            return
        self.registry.add(call)
        auth = call_auth(self.cfg, call)
        if call.profile != auth.profile:
            log.warning("[%s] Management call placed as query only: %s", call.short_id, auth.reason)
        await self._fire({**self._event(call, "started"), "effective_profile": auth.profile})
        if self.fallback.enabled:
            # Ready before the phone is answered, in case OpenAI fails then.
            self.spawn(self.fallback.prepare_call(call))

    async def note(self, call_id: str, text: str, resolve: bool = False) -> bool:
        """Tell the person on the phone something, without hanging up.

        ``resolve``: the notice no longer needs confirming (the alarm was
        disarmed, someone else is dealing with it): the assistant stops, says
        the note and winds the call up; the call ends as ``resolved``.
        """
        text = text.strip()[:MAX_NOTE_CHARS]
        call_id = self._canonical(call_id)
        live = self.live.get(call_id)
        delivered = False
        if not text:
            log.warning("Note without text: ignored")
        elif live is None or live.handler is None or not hasattr(live.handler, "note"):
            log.warning("[%s] note not delivered: no conversation in progress", call_id[:8] or "-")
        else:
            try:
                delivered = await live.handler.note(text, resolve=resolve)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] note failed: %r", call_id[:8], err)
        if call_id:
            await self._fire({"call_id": call_id, "stage": "note", "delivered": delivered,
                              "resolve": resolve})
        return delivered

    async def hangup(self, call_id: str = "") -> int:
        """Hang up one call (by id) or every call: in progress or still ringing.

        A call that is still ringing is withdrawn here; the phone itself stops
        ringing when Home Assistant hangs it up in Asterisk.
        """
        call_id = self._canonical(call_id)
        count = 0
        targets = [call_id] if call_id else list(self.live)
        for cid in targets:
            live = self.live.get(cid)
            if live is not None and live.task is not None and not live.hung_up:
                live.hung_up = True
                live.task.cancel()
                count += 1
        pending = [call_id] if call_id else [c.call_id for c in self.registry.pending()]
        for cid in pending:
            call = self.registry.cancel(cid)
            if call is not None:
                self.fallback.forget(call)
                await self._stop_ringing(call)
                await self._fire({**self._event(call, "ended", "cancelled"), "ended_by": "hangup"})
                count += 1
        log.info("Hang-up requested (%s): %d call(s)", call_id[:8] if call_id else "all", count)
        return count

    async def _stop_ringing(self, call: OutboundCall) -> None:
        """Hang up in Asterisk a withdrawn call that is still ringing.

        Asterisk does not know the call was withdrawn: its Dial would ring until
        its own timeout. The ``bridge-kill`` context hangs up the channels of
        ``Local/<number>_<call id>@bridge-outbound`` (see DOCS). A failure here
        is only logged: the call is cancelled anyway.
        """
        number = call.number.strip()
        if not self.cfg.asterisk_app:
            log.info("[%s] asterisk_app not set: the phone keeps ringing until Asterisk gives up",
                     call.short_id)
            return
        if not number or not all(ch.isdigit() or ch == "+" for ch in number):
            log.warning("[%s] cannot stop the ringing in Asterisk: the call has no valid number",
                        call.short_id)
            return
        command = f"channel originate Local/{number}_{call.call_id}@bridge-kill application Wait 1"
        try:
            await self._asterisk(command)
            log.info("[%s] asked Asterisk to stop the ringing", call.short_id)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] could not ask Asterisk to stop the ringing: %r", call.short_id, err)

    @staticmethod
    def _canonical(call_id: str) -> str:
        try:
            return str(uuid.UUID(call_id.strip())) if call_id.strip() else ""
        except ValueError:
            return call_id.strip()

    async def _report_missed(self, call: OutboundCall) -> None:
        self.fallback.forget(call)
        await self._fire(self._event(call, "ended", "no_answer"))

    def _event(self, call: OutboundCall, stage: str, result: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "call_id": call.call_id,
            "direction": "outbound",
            "stage": stage,
            "profile": call.profile,
            "person": call.person,
            "number": call.number,
            "engine": call.engine,
        }
        if result:
            data["result"] = result
        return data

    async def _fire(self, data: dict[str, Any]) -> None:
        await self._fire_event(EVENT_TYPE, data)

    async def _fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self.home.fire_event(event_type, data)
            log.debug("Event %s: %s", event_type, data)
        except Exception as err:  # noqa: BLE001
            log.warning("Could not send the event %s to Home Assistant: %r", event_type, err)

    async def _asterisk(self, command: str) -> None:
        """Send a CLI command to the Asterisk app (through Home Assistant)."""
        if not self.cfg.asterisk_app:
            raise RuntimeError("asterisk_app is not set")
        await self.home.call_service("hassio", "addon_stdin",
                                     {"addon": self.cfg.asterisk_app, "input": command})

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            kind, payload = await asyncio.wait_for(read_frame(reader), 5)
            if kind != KIND_UUID or len(payload) != 16:
                log.warning("Connection from %s without a valid UUID; closing it", peer)
                return
            full_id = str(uuid.UUID(bytes=payload))
            call_id = full_id[:8]
            trunk = trunk_report(full_id)
            if trunk is not None:
                await self._hang_up(writer)
                await self.trunk.on_report(*trunk)
                return
            report = dial_report(full_id)
            if report is not None:
                await self._on_dial_report(writer, *report)
                return
            machine = voicemail_report(full_id)
            if machine is not None:
                await self._on_voicemail(writer, *machine)
                return
            outbound = self.registry.take(full_id)
            if outbound is None and self.registry.retired(full_id):
                log.warning("[%s] answered after it expired or was cancelled: hung up", call_id)
                await self._hang_up(writer)
                return
            number = "" if outbound else (caller_number(full_id) or "")
            auth = call_auth(self.cfg, outbound, number)

            if self._active >= self.cfg.max_concurrent_calls:
                log.warning("[%s] rejected: %d call(s) already active", call_id, self._active)
                writer.write(pack_frame(KIND_HANGUP))
                await writer.drain()
                if outbound:
                    await self._fire({**self._event(outbound, "ended", "failed"),
                                      "error": "too many calls"})
                return

            if outbound is None and self.stt_test is not None and self.stt_test.take():
                self._active += 1
                log.info("[%s] incoming call: transcription TEST (not OpenAI)", call_id)
                try:
                    await self.stt_test.run(call_id, reader, writer)
                finally:
                    await self._hang_up(writer)
                    self._active -= 1
                    log.info("[%s] call ended", call_id)
                return

            self._active += 1
            live = LiveCall(asyncio.current_task())
            self.live[full_id] = live
            if outbound:
                log.info("[%s] outbound call answered (profile %s%s)", call_id, auth.profile,
                         "" if auth.profile == outbound.profile else f", requested {outbound.profile}")
                await self._fire({**self._event(outbound, "answered"), "effective_profile": auth.profile})
            else:
                log.info("[%s] incoming call from %s (%s)", call_id, number or "an unknown number",
                         {"pin": "PIN required", "locked": "query only", "open": "no PIN"}[auth.state])
                if auth.locked and auth.reason:
                    log.info("[%s] query only: %s", call_id, auth.reason)
                await self._fire(self._inbound_event(full_id, "started", number, auth))
            started = time.monotonic()
            session: CallSession | None = None
            fallback: dict[str, Any] | None = None
            # Asterisk drops the call after 2 s without activity, so stream
            # silence right away, while we connect to OpenAI.
            silence = asyncio.create_task(keepalive(writer))
            try:
                if outbound is not None and outbound.engine == "local":
                    silence.cancel()
                    await asyncio.gather(silence, return_exceptions=True)
                    fallback = await self._run_local(call_id, reader, writer, outbound, "requested", auth, live)
                    return
                try:
                    async with connect(
                        self.realtime_url,
                        additional_headers={"Authorization": f"Bearer {self.cfg.openai_api_key}"},
                        open_timeout=10,
                        max_size=None,
                    ) as ws:
                        silence.cancel()
                        await asyncio.gather(silence, return_exceptions=True)
                        session = CallSession(
                            self.cfg, call_id, reader, writer, ws, self.home,
                            outbound=outbound, status=self.status, auth=auth,
                            voice=self.fallback,
                        )
                        live.handler = session
                        await session.run(terminate=False)
                except Exception as err:  # noqa: BLE001
                    log.error("[%s] call failed: %r", call_id, err)
                    self.spawn(self.health.report(False, describe_error(err)))
                else:
                    if session.connected:
                        self.spawn(self.health.report(True))
                    elif session.openai_failed:
                        self.spawn(self.health.report(False, session.openai_error or "no session"))
                silence.cancel()
                await asyncio.gather(silence, return_exceptions=True)
                if self._needs_fallback(session, outbound):
                    fallback = await self._run_fallback(call_id, reader, writer, outbound, auth, live)
            finally:
                silence.cancel()  # e.g. hung up by Home Assistant while connecting
                await self._hang_up(writer)
                self._active -= 1
                self.live.pop(full_id, None)
                log.info("[%s] call ended%s", call_id, " (hung up by Home Assistant)" if live.hung_up else "")
                if outbound:
                    self.fallback.forget(outbound)
                data = self._ended(full_id, outbound, session, time.monotonic() - started, fallback)
                if outbound is None:
                    data.update(self._inbound_event(full_id, "ended", number, auth))
                else:
                    data["effective_profile"] = auth.profile
                if live.hung_up:
                    data["ended_by"] = "hangup"
                await self._fire(data)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            log.warning("Connection from %s closed before the call started", peer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


    async def _on_dial_report(self, writer: Any, status: str, tail: str) -> None:
        """Asterisk reports why an outbound call was not answered."""
        await self._hang_up(writer)
        call = self.registry.take_by_tail(tail)
        if call is None:
            log.debug("Dial status %s for a call that is no longer pending", status)
            return
        result = result_for(status)
        log.info("[%s] outbound call not answered: %s", call.short_id, status)
        self.fallback.forget(call)
        await self._fire({**self._event(call, "ended", result), "dialstatus": status})

    async def _on_voicemail(self, writer: Any, cause: str, tail: str) -> None:
        """Asterisk's AMD() detected an answering machine on an outbound call."""
        call = self.registry.take_by_tail(tail)
        if call is None:
            log.debug("Answering machine for a call that is no longer pending")
            await self._hang_up(writer)
            return
        log.info("[%s] answered by a machine (%s)", call.short_id, cause)
        left = False
        if call.voicemail_message:
            if self.fallback.enabled:
                try:
                    await self._play(writer, await self.fallback.audio(call.voicemail_message))
                    left = True
                except Exception as err:  # noqa: BLE001
                    log.warning("[%s] could not leave the message: %r", call.short_id, err)
            else:
                log.warning("[%s] voicemail_message needs fallback_tts: no message left", call.short_id)
        await self._hang_up(writer)
        self.fallback.forget(call)
        await self._fire({**self._event(call, "ended", "voicemail"), "detected_by": "amd",
                          "amdstatus": "MACHINE", "amdcause": cause, "voicemail_message_left": left})

    async def _play(self, writer: Any, pcm: bytes, frame_seconds: float = FRAME_MS / 1000) -> None:
        loop = asyncio.get_running_loop()
        tick = loop.time()
        for i in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[i:i + FRAME_BYTES]
            writer.write(pack_frame(KIND_AUDIO, frame + bytes(FRAME_BYTES - len(frame))))
            await writer.drain()
            tick += frame_seconds
            await asyncio.sleep(max(0.0, tick - loop.time()))
        await asyncio.sleep(0.5)

    def _inbound_event(self, full_id: str, stage: str, number: str, auth: CallAuth) -> dict[str, Any]:
        return {
            "call_id": full_id,
            "direction": "inbound",
            "stage": stage,
            "profile": PROFILE_INBOUND,
            "number": number,
            "person": auth.person,
            "access": {"pin": "pin", "locked": "query_only", "open": "open"}[auth.state],
        }

    def _needs_fallback(self, session: CallSession | None, outbound: OutboundCall | None) -> bool:
        if not self.fallback.enabled:
            return False
        if session is None:
            return True  # OpenAI could not even be reached
        if session.end_reason in ("asterisk", "pacer"):
            return False  # the phone side is gone
        if not session.connected:
            return True  # OpenAI never confirmed the session (or failed while starting)
        if not session.openai_failed:
            return False  # ended normally (goodbye, time limit)
        if outbound is not None:
            # Mid-call drop: repeat the notice unless it was already confirmed.
            return not session.confirmed
        return True  # incoming call: say that the assistant is not available

    async def _run_fallback(
        self, call_id: str, reader: asyncio.StreamReader, writer: Any, outbound: OutboundCall | None,
        auth: CallAuth | None = None, live: LiveCall | None = None,
    ) -> dict[str, Any]:
        if self.cfg.local_conversation:
            return await self._run_local(call_id, reader, writer, outbound, "OpenAI unavailable", auth, live)
        log.info("[%s] OpenAI unavailable: using the fallback (%s)", call_id, self.fallback.engine)
        try:
            if outbound is not None:
                confirmed = await self.fallback.outbound(reader, writer, outbound)
                log.info("[%s] fallback notice %s", call_id, "confirmed" if confirmed else "not confirmed")
                return {"confirmed": confirmed}
            await self.fallback.inbound(reader, writer)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] fallback failed: %r", call_id, err)
        return {"confirmed": False}

    async def _run_local(
        self, call_id: str, reader: asyncio.StreamReader, writer: Any,
        outbound: OutboundCall | None, reason: str,
        auth: CallAuth | None = None, live: LiveCall | None = None,
    ) -> dict[str, Any]:
        log.info("[%s] local conversation (%s; %s + %s)",
                 call_id, reason, self.cfg.local_stt, self.fallback.engine)
        local = LocalConversation(
            self.cfg, call_id, reader, writer, self.home, self.fallback,
            self.cfg.local_stt, self.status, outbound, auth=auth,
        )
        if live is not None:
            live.handler = local
        try:
            await local.run()
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] local conversation failed: %r", call_id, err)
        return {"confirmed": local.confirmed, "confirmed_by": local.confirmed_by,
                "pin_person": local.pin_person, "local": True,
                "resolved": local.resolved, "silence": local.silence_ended}

    # ------------------------------------------------------------- PIN mode
    async def pin_status(self) -> None:
        """Publish the PIN mode (never the PINs) and keep it published."""
        book = self.cfg.pin_book
        reported: tuple[str, ...] = ()
        while True:
            try:
                states = await self.home.get("states")
                persons = [s for s in states if str(s.get("entity_id", "")).startswith("person.")]
                book.learn_people(persons)
                self.people = [s["entity_id"] for s in persons]
            except Exception as err:  # noqa: BLE001
                log.debug("Could not read the persons: %r", err)
            if book.unknown != reported:
                reported = book.unknown
                for person in book.unknown:
                    log.warning("PIN mode %s: %s is not a person in Home Assistant (check the "
                                "spelling; people_without_pin in %s lists the valid ids)",
                                book.mode, person, PIN_ENTITY_ID)
                if book.unknown:
                    log.warning("PIN mode %s is NOT complete: Management calls run as query only "
                                "and incoming calls can only ask, until it is fixed.", book.mode)
            state, attributes = book.status(self.people)
            try:
                await self.home.set_state(PIN_ENTITY_ID, state, attributes)
            except Exception as err:  # noqa: BLE001
                log.debug("Could not publish the PIN mode: %r", err)
            await asyncio.sleep(PIN_STATUS_MINUTES * 60)


    @staticmethod
    async def _hang_up(writer: Any) -> None:
        try:
            writer.write(pack_frame(KIND_HANGUP))
            await writer.drain()
        except Exception:  # noqa: BLE001 - the connection may already be closed
            pass

    def _ended(
        self,
        full_id: str,
        outbound: OutboundCall | None,
        session: CallSession | None,
        duration: float,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if fallback is not None and outbound is not None:
            result = "confirmed" if fallback.get("confirmed") else "answered"
        elif session is None or not session.connected:
            result = "failed"  # the assistant never got to speak (e.g. OpenAI down)
        elif session.confirmed:
            result = "confirmed"
        else:
            result = "answered"
        source = fallback if fallback is not None else {
            "resolved": getattr(session, "resolved", False),
            "silence": getattr(session, "silence_ended", False),
        }
        resolved = bool(source.get("resolved"))
        voicemail = fallback is None and bool(getattr(session, "voicemail", False))
        if voicemail and outbound is not None and result in ("answered", "confirmed"):
            result = "voicemail"
        if resolved and outbound is not None and result in ("answered", "confirmed"):
            result = "resolved"
        if outbound:
            data = self._event(outbound, "ended", result)
        else:
            data = {"call_id": full_id, "direction": "inbound", "stage": "ended",
                    "profile": PROFILE_INBOUND, "result": result}
        if fallback is not None and fallback.get("confirmed"):
            data["confirmed_by"] = fallback.get("confirmed_by") or "dtmf"
        elif session and session.confirmed_by:
            data["confirmed_by"] = session.confirmed_by
        if resolved:
            data["resolved"] = True
        if source.get("silence"):
            data["ended_by"] = "silence"
        if result == "voicemail":
            data["detected_by"] = "assistant"
            data["voicemail_message_left"] = bool(getattr(session, "voicemail_message_left", False))
        data["duration"] = round(duration)
        data["pin_verified"] = bool(session and session.pin_person is not None)
        if session and session.pin_person:
            data["pin_person"] = session.pin_person
        if fallback is not None:
            requested_local = outbound is not None and outbound.engine == "local"
            if not requested_local:  # engine local is not a fallback
                data["fallback"] = "local" if fallback.get("local") else True
            if fallback.get("local") and outbound is None:
                data["result"] = "answered"  # the caller talked to the local assistant
            if fallback.get("pin_person") is not None:
                data["pin_verified"] = True
                if fallback["pin_person"]:
                    data["pin_person"] = fallback["pin_person"]
        return data


def report_pin_mode(cfg: Config, path: str = PIN_MODE_FILE) -> None:
    """Log the PIN mode at start-up and warn about a change of mode."""
    book = cfg.pin_book
    previous = ""
    try:
        with open(path, encoding="utf-8") as handle:
            previous = handle.read().strip()
    except OSError:
        pass
    if previous and previous != book.mode:
        log.warning("PIN mode changed from %s to %s", previous, book.mode)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(book.mode)
    except OSError as err:
        log.debug("Could not save the PIN mode: %r", err)
    if book.mode == MODE_OFF:
        log.warning(
            "PIN DISABLED: any call that reaches the bridge can control the home. "
            "Your Asterisk dialplan is the only barrier."
        )
        return
    for note in book.ignored:
        log.info("PIN mode %s: %s", book.mode, note)
    for issue in book.issues:
        log.warning("PIN mode %s: %s", book.mode, issue)
    if not book.complete:
        log.warning(
            "PIN mode %s is NOT complete: Management calls run as query only and "
            "incoming calls can only ask, until it is fixed in the configuration.",
            book.mode,
        )
    elif book.entries:
        log.info("Personal PINs for: %s", ", ".join(
            e.person + (f" ({len(e.numbers)} number(s))" if e.numbers else "") for e in book.entries))


async def _stdin_reader() -> asyncio.StreamReader | None:
    """The add-on's stdin, where Home Assistant sends commands (hassio.addon_stdin)."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (OSError, ValueError) as err:
        log.debug("stdin is not available for commands: %r", err)
        return None
    return reader


async def serve(cfg: Config) -> None:
    async with aiohttp.ClientSession() as http:
        bridge = Bridge(cfg, http)
        server = await asyncio.start_server(bridge.handle, cfg.bind_address, cfg.port)
        report_pin_mode(cfg)
        if cfg.bind_address not in ("127.0.0.1", "::1", "localhost"):
            log.warning(
                "AudioSocket is listening on %s. It has no authentication: restrict "
                "access to the Asterisk host with a firewall.",
                cfg.bind_address,
            )
        if cfg.model in DEPRECATED_MODELS:
            log.warning("Model %s is deprecated and retires on 2027-01-20.", cfg.model)
        log.info(
            "Asterisk Realtime Bridge %s listening on %s:%d (model %s, PIN mode %s)",
            __version__,
            cfg.bind_address,
            cfg.port,
            cfg.model,
            cfg.pin_book.mode,
        )
        bridge.spawn(bridge.pin_status())
        if bridge.fallback.enabled:
            bridge.spawn(bridge.fallback.prepare_fixed())
        else:
            log.info("No fallback_tts: if OpenAI is unavailable, calls cannot be answered")
        bridge.spawn(bridge.health.run())
        commands = None
        stdin = await _stdin_reader()
        if stdin is not None:
            commands = asyncio.create_task(read_commands(stdin, bridge.on_command))
            log.info("Outbound calls: listening for commands from Home Assistant "
                     "(read-only scope without PIN: %s%s)", bridge.status.scope,
                     f"; excluding {cfg.query_exclude}" if cfg.query_exclude else "")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        async with server:
            await stop.wait()
        log.info("Stopping...")
        if commands:
            commands.cancel()
            await asyncio.gather(commands, return_exceptions=True)
        await bridge.close()


def main() -> None:
    try:
        cfg = load()
    except ConfigError as err:
        print(f"Configuration ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
