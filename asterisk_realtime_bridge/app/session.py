"""One phone call: Asterisk audio <-> OpenAI Realtime API, with Home Assistant tools.

Design
------
* Uplink: 20 ms slin frames from Asterisk -> µ-law -> ``input_audio_buffer.append``.
* Downlink: ``response.output_audio.delta`` (µ-law, usually delivered faster
  than real time) -> slin -> local queue -> a "pacer" sends it to Asterisk at
  20 ms per frame. Because the queue lives on our side, interrupting is
  instantaneous: we just empty the queue.
* Jitter buffer: playback of a response starts once ``PREBUFFER_MS`` of audio
  are queued (or the response's audio is complete), and restarts the same way
  if the queue runs dry mid-response, so a late delta does not cut the voice.
* Barge-in: on ``input_audio_buffer.speech_started`` we empty the queue and send
  ``conversation.item.truncate`` with the milliseconds the caller actually
  heard, so the model knows what was really said.
* Security: until the PIN is verified, the ``home`` tool is rejected in code
  (it does not depend on the model following its instructions).
* Outbound calls (placed for Home Assistant to deliver a notice) start by
  reading the notice. Questions are answered with ``home_status``, a read-only
  list built by the bridge (``status.py``); changes need a PIN in the
  ``Management`` profile (the global PIN or the personal PIN of the person
  called) and are impossible in ``QueryOnly``.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hmac
import re
import json
import logging
from typing import Any, Protocol

from .audiosocket import (
    FRAME_BYTES,
    FRAME_MS,
    KIND_AUDIO,
    KIND_DTMF,
    KIND_ERROR,
    KIND_HANGUP,
    pack_frame,
    read_frame,
)
from .config import PROFILE_MANAGE, PROFILE_QUERY, Config, key_name
from .pins import CallAuth
from .g711 import slin_to_ulaw, ulaw_to_slin
from .outbound import OutboundCall
from .stt import rms

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 10.0
FLUSH_GRACE_S = 0.6
# A confirmation key that is also a digit, in a call where a PIN can be typed,
# counts only if no other key follows within this time.
CONFIRM_KEY_WAIT_S = 2.0
# Audio queued before playback starts (and restarts after running dry).
PREBUFFER_MS = 200
# Voicemail: the message is left once the phone has been quiet this long (the
# greeting and its beep are over), waiting at most VOICEMAIL_MAX_WAIT_S.
FAR_VOICE_RMS = 400
VOICEMAIL_QUIET_S = 1.5
VOICEMAIL_MAX_WAIT_S = 20.0

# 20 ms of digital silence. Asterisk's AudioSocket application hangs up if it
# sees no activity (from the channel or from the server) for 2000 ms, and some
# carriers only start sending media once they receive some. So we must send
# audio continuously, from the very first moment, even when the assistant is
# not talking.
SILENCE_FRAME = bytes(FRAME_BYTES)


async def keepalive(writer: Any, frame_seconds: float = FRAME_MS / 1000) -> None:
    """Send silence to Asterisk until cancelled (used while connecting to OpenAI)."""
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    while True:
        writer.write(pack_frame(KIND_AUDIO, SILENCE_FRAME))
        await writer.drain()
        next_tick += frame_seconds
        delay = next_tick - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            next_tick = loop.time()


class _WebSocket(Protocol):
    async def send(self, message: str) -> None: ...
    def __aiter__(self): ...


class _Home(Protocol):
    async def ask(self, text: str) -> dict[str, Any]: ...


class _Status(Protocol):
    async def snapshot(self, search: str = "") -> dict[str, Any]: ...


_HOME_INBOUND = (
    "Control or query the home through Home Assistant. Pass ONE "
    "request in natural language, for example 'turn on the living "
    "room light' or 'is the garage door open?'. Returns Home "
    "Assistant's response."
)
_HOME_MANAGE = (
    "Change something in the home through Home Assistant (only after the PIN "
    "is verified). Pass ONE request in natural language, for example 'close "
    "the garage door'. For questions use home_status instead."
)
_STATUS_TOOL = {
    "type": "function",
    "name": "home_status",
    "description": (
        "Read-only: the current state of the home entities you may talk about, "
        "with the current time. Optional search: one word to narrow the list "
        "(for example 'garaje'). Call it before saying how anything is now."
    ),
    "parameters": {
        "type": "object",
        "properties": {"search": {"type": "string"}},
    },
}


def call_auth(cfg: Config, call: OutboundCall | None = None, number: str = "") -> CallAuth:
    """What this call may do (see ``pins.py``): outbound by profile and person,
    incoming by the caller's number."""
    book = cfg.pin_book
    return book.for_outbound(call) if call is not None else book.for_inbound(number)


def accepted_pins(cfg: Config, call: OutboundCall | None, number: str = "") -> tuple[tuple[str, str], ...]:
    """PINs that can unlock changes in this call, as (who, digits)."""
    return call_auth(cfg, call, number).pins


def pin_state(cfg: Config, call: OutboundCall | None, number: str = "") -> str:
    """"open" (no PIN needed to act), "pin" (a PIN unlocks) or "locked" (no changes)."""
    return call_auth(cfg, call, number).state


def build_tools(
    cfg: Config, call: OutboundCall | None = None, auth: CallAuth | None = None
) -> list[dict[str, Any]]:
    """Tools for an incoming call (``call`` None) or an outbound call."""
    state = (auth or call_auth(cfg, call)).state
    tools: list[dict[str, Any]] = []
    if call is not None or state == "locked":
        tools.append(_STATUS_TOOL)
    if state != "locked":
        # The tool that can act is not even offered when nothing may change.
        tools.append(
            {
                "type": "function",
                "name": "home",
                "description": _HOME_INBOUND if call is None else _HOME_MANAGE,
                "parameters": {
                    "type": "object",
                    "properties": {"request": {"type": "string"}},
                    "required": ["request"],
                },
            }
        )
    tools.append(
        {
            "type": "function",
            "name": "hang_up",
            "description": (
                "End the call. Use it after saying goodbye, when the caller "
                "needs nothing else."
            ),
            "parameters": {"type": "object", "properties": {}},
        }
    )
    if call is not None and call.detect_voicemail:
        tools.append(
            {
                "type": "function",
                "name": "report_voicemail",
                "description": (
                    "The call was answered by a voicemail or an answering machine, "
                    "not by a person. Call it at once, without saying anything first."
                ),
                "parameters": {"type": "object", "properties": {}},
            }
        )
    if call is not None and call.confirm != "dtmf":
        tools.append(
            {
                "type": "function",
                "name": "confirm_notice",
                "description": (
                    "Record that the person has confirmed they received the "
                    "notice. Call it once, as the call's rules describe."
                ),
                "parameters": {"type": "object", "properties": {}},
            }
        )
    if state == "pin":
        tools.insert(
            0,
            {
                "type": "function",
                "name": "verify_pin",
                "description": (
                    "Check the security PIN spoken by the caller. Pass ONLY the "
                    "digits one by one as a string of numerals (for example "
                    "'123456'); never words, never grouped as a number like "
                    "'one hundred twenty-three thousand'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"pin": {"type": "string"}},
                    "required": ["pin"],
                },
            },
        )
    return tools


def build_session_update(
    cfg: Config, call: OutboundCall | None = None, auth: CallAuth | None = None
) -> dict[str, Any]:
    auth = auth or call_auth(cfg, call)
    if cfg.vad == "semantic_vad":
        turn_detection: dict[str, Any] = {
            "type": "semantic_vad",
            "eagerness": "auto",
            "create_response": True,
            "interrupt_response": True,
        }
    else:
        turn_detection = {
            "type": "server_vad",
            "threshold": cfg.vad_threshold,
            "prefix_padding_ms": 300,
            "silence_duration_ms": cfg.vad_silence_ms,
            "create_response": True,
            "interrupt_response": True,
        }
    audio_in: dict[str, Any] = {
        "format": {"type": "audio/pcmu"},
        "turn_detection": turn_detection,
    }
    if cfg.noise_reduction != "off":
        audio_in["noise_reduction"] = {"type": cfg.noise_reduction}
    if cfg.log_transcripts:
        audio_in["transcription"] = {
            "model": "gpt-4o-mini-transcribe",
            "language": cfg.language,
        }
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": cfg.instructions_for(call, auth.state),
            "audio": {
                "input": audio_in,
                "output": {"format": {"type": "audio/pcmu"}, "voice": cfg.voice},
            },
            "tools": build_tools(cfg, call, auth),
            "tool_choice": "auto",
        },
    }


_DIGIT_WORDS = {
    "cero": "0", "uno": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4",
    "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _extract_digits(text: str) -> str:
    """Digits in ``text``; spelled-out digit words ("ocho", "one") count too."""
    out = []
    for token in re.findall(r"[^\W_]+", text.lower()):
        if token.isdigit():
            out.append(token)
        elif token in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[token])
    return "".join(out)


class CallSession:
    def __init__(
        self,
        cfg: Config,
        call_id: str,
        reader: asyncio.StreamReader,
        writer: Any,
        ws: _WebSocket,
        home: _Home,
        frame_seconds: float = FRAME_MS / 1000,
        outbound: OutboundCall | None = None,
        status: _Status | None = None,
        auth: CallAuth | None = None,
        voice: Any = None,
    ) -> None:
        self.cfg = cfg
        self.voice = voice  # fallback TTS (Fallback): leaves the voicemail message
        self.call_id = call_id
        self.reader = reader
        self.writer = writer
        self.ws = ws
        self.home = home
        self.status = status
        self.frame_seconds = frame_seconds
        self.outbound = outbound
        self.profile = outbound.profile if outbound else None
        self.auth = auth or call_auth(cfg, outbound)
        self._pins = self.auth.pins
        self._pin_state = self.auth.state

        # Audio queue towards Asterisk: (item_id, 20 ms frame).
        self._out: collections.deque[tuple[str, bytes]] = collections.deque()
        self._carry = bytearray()
        self._carry_item: str | None = None
        self._played_ms: dict[str, int] = collections.defaultdict(int)
        self._audio_done: set[str] = set()  # items whose audio is complete
        self._buffering = True
        self.underruns = 0  # times the audio ran dry mid-response

        self._ready = asyncio.Event()
        self._finished = asyncio.Event()
        self._response_active = False
        self._hangup_pending = False
        self._note_pending = False
        self._response_id = ""
        self._dropped: set[str] = set()  # responses cancelled by a resolving note
        self._last_activity = 0.0  # loop time of the last sound or key, either side
        self._reprompts = 0
        self._seen_calls: set[str] = set()
        self._tool_tasks: set[asyncio.Task] = set()

        # "Authed" means allowed to CHANGE things: never when "locked".
        self._authed = self._pin_state == "open"
        self._pin_attempts = 0
        self._dtmf = ""
        self._key_timer: asyncio.Task | None = None

        # Outcome, reported to Home Assistant when the call ends.
        self.connected = False  # the OpenAI session was ready (audio flowed)
        self.confirmed = False  # the person confirmed the notice
        self.confirmed_by: str | None = None  # "implicit", "voice" or "dtmf"
        self.pin_person: str | None = None  # who verified ("" = global PIN)
        # Why the call ended: "asterisk", "openai", "ready" (OpenAI did not
        # confirm the session), "finished" (hang_up), "pacer" or "timeout".
        self.end_reason: str | None = None
        self.resolved = False  # Home Assistant closed the notice with a note (resolve)
        self.silence_ended = False  # hung up because nobody answered
        self.voicemail = False  # the assistant recognised a voicemail
        self._far_voice = 0.0  # loop time of the last loud audio from the phone
        self.voicemail_message_left = False
        self.openai_error = ""  # last error code sent by OpenAI

    # ------------------------------------------------------------------ run
    @property
    def openai_failed(self) -> bool:
        """OpenAI dropped the call while the phone was still connected."""
        return self.end_reason in ("openai", "ready")

    async def run(self, terminate: bool = True) -> None:
        """Run the call. With ``terminate=False`` the caller hangs up (after a fallback)."""
        if self._pin_state == "locked" and self.auth.reason:
            log.warning("[%s] query only: %s", self.call_id, self.auth.reason)
        await self._send(build_session_update(self.cfg, self.outbound, self.auth))
        workers = {
            "asterisk": asyncio.create_task(self._from_asterisk()),
            "openai": asyncio.create_task(self._from_openai()),
            "pacer": asyncio.create_task(self._pacer()),
            "ready": asyncio.create_task(self._watch_ready()),
            "finished": asyncio.create_task(self._finished.wait()),
            "silence": asyncio.create_task(self._silence_watch()),
        }
        try:
            done, _ = await asyncio.wait(
                workers.values(),
                timeout=self.cfg.max_call_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                self.end_reason = "timeout"
                log.info(
                    "[%s] maximum call duration reached (%ss)",
                    self.call_id,
                    self.cfg.max_call_seconds,
                )
            for name, task in workers.items():
                if task in done and self.end_reason is None:
                    self.end_reason = name
                if task in done and not task.cancelled() and task.exception():
                    log.warning("[%s] %s worker failed: %r", self.call_id, name, task.exception())
        finally:
            for task in (*workers.values(), *self._tool_tasks):
                task.cancel()
            await asyncio.gather(*workers.values(), *self._tool_tasks, return_exceptions=True)
            if self.underruns:
                log.info(
                    "[%s] the assistant's audio ran dry %d time(s) mid-response",
                    self.call_id, self.underruns,
                )
            if terminate:
                await self._terminate()

    async def _terminate(self) -> None:
        try:
            self.writer.write(pack_frame(KIND_HANGUP))
            await self.writer.drain()
        except Exception:  # noqa: BLE001 - the connection may already be closed
            pass

    async def _watch_ready(self) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RuntimeError("OpenAI did not confirm the session in time") from None
        await asyncio.Event().wait()  # ready: sleep until cancelled

    async def _send(self, event: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(event))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    # ------------------------------------------------- Asterisk -> OpenAI
    async def _from_asterisk(self) -> None:
        while True:
            try:
                kind, payload = await read_frame(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError):
                log.info("[%s] Asterisk closed the connection", self.call_id)
                return
            if kind == KIND_AUDIO:
                if rms(payload) > FAR_VOICE_RMS:
                    self._far_voice = asyncio.get_running_loop().time()
                if self._ready.is_set() and not self.voicemail:
                    audio = base64.b64encode(slin_to_ulaw(payload)).decode("ascii")
                    await self._send({"type": "input_audio_buffer.append", "audio": audio})
            elif kind == KIND_DTMF:
                self._caller_active()
                await self._on_dtmf(payload.decode("ascii", errors="ignore")[:1])
            elif kind == KIND_HANGUP:
                log.info("[%s] caller hung up", self.call_id)
                return
            elif kind == KIND_ERROR:
                log.warning("[%s] AudioSocket error: %r", self.call_id, payload)
                return

    # ------------------------------------------------- OpenAI -> Asterisk
    async def _from_openai(self) -> None:
        async for raw in self.ws:
            try:
                event = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            await self._on_event(event)
        log.info("[%s] OpenAI closed the connection", self.call_id)

    async def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "session.updated":
            if not self._ready.is_set():
                self._ready.set()
                self.connected = True
                await self._greet()
        elif kind == "input_audio_buffer.speech_started":
            self._caller_active()
            await self._interrupt()
        elif kind == "response.created":
            self._response_active = True
            self._response_id = str((event.get("response") or {}).get("id") or "")
            self._touch()
        elif kind == "response.output_audio.delta":
            if event.get("response_id") and event["response_id"] in self._dropped:
                return  # audio of a response cut short by a resolving note
            if self.voicemail:
                return  # the bridge is leaving the voicemail message
            self._touch()
            self._deliver_audio(event.get("item_id", ""), base64.b64decode(event.get("delta", "")))
        elif kind == "response.output_audio.done":
            self._flush_carry()
            if event.get("item_id"):
                self._audio_done.add(event["item_id"])
        elif kind == "response.done":
            await self._on_response_done(event.get("response") or {})
        elif kind == "error":
            err = event.get("error") or {}
            self.openai_error = str(err.get("code") or err.get("type") or "error")
            log.warning("[%s] OpenAI error: %s %s", self.call_id, err.get("code"), err.get("message"))
        elif self.cfg.log_transcripts:
            if kind == "conversation.item.input_audio_transcription.completed":
                log.info("[%s] caller: %s", self.call_id, event.get("transcript", "").strip())
            elif kind == "response.output_audio_transcript.done":
                log.info("[%s] assistant: %s", self.call_id, event.get("transcript", "").strip())

    async def _greet(self) -> None:
        # State the language explicitly: the model tends to answer in the
        # language of the instruction it is given, which would be English.
        lang = self.cfg.language_name
        call = self.outbound
        if call is not None:
            name = call.assistant_name or self.cfg.assistant_name
            key = key_name(call.confirm_key)
            extra = f' Also tell them: "{call.confirm_text}"' if call.confirm_text else ""
            ask = {
                "implicit": "Then ask whether they need anything else.",
                "voice": "Then ask them explicitly to confirm that they have received it." + extra,
                "dtmf": f"Then ask them to press the {key} key to confirm that they have received it." + extra,
                "voice_or_dtmf": (
                    "Then ask them to confirm that they have received it, by saying so "
                    f"or by pressing the {key} key." + extra
                ),
            }[call.confirm]
            hint = (
                f"You placed this call to deliver a notice. Speak {lang}. Introduce "
                f"yourself in a few words as {name}, then read this notice clearly: "
                f"\"{call.message}\". {ask}"
            )
        elif self._pin_state == "pin":
            hint = (
                f"Greet the caller in {lang}, in one short sentence, introducing yourself "
                f"as {self.cfg.assistant_name}, and ask for the security PIN."
            )
        else:
            hint = (
                f"Greet the caller in {lang}, in one short sentence, introducing yourself "
                f"as {self.cfg.assistant_name}, and ask how you can help."
            )
        await self._send({"type": "response.create", "response": {"instructions": hint}})

    async def _on_response_done(self, response: dict[str, Any]) -> None:
        self._response_active = False
        calls = [
            item
            for item in response.get("output", [])
            if item.get("type") == "function_call" and item.get("call_id") not in self._seen_calls
        ]
        if calls:
            self._seen_calls.update(c["call_id"] for c in calls)
            self._spawn(self._run_tools(calls))
        elif self._note_pending and not self._hangup_pending:
            # A note arrived while the assistant was speaking: say it now.
            self._note_pending = False
            await self._send({"type": "response.create"})
        elif self._hangup_pending:
            self._spawn(self._finish_after_drain())

    # ------------------------------------------------------------- audio
    def _deliver_audio(self, item_id: str, ulaw: bytes) -> None:
        if item_id != self._carry_item:
            self._flush_carry()
            self._carry_item = item_id
        self._carry += ulaw_to_slin(ulaw)
        while len(self._carry) >= FRAME_BYTES:
            self._out.append((item_id, bytes(self._carry[:FRAME_BYTES])))
            del self._carry[:FRAME_BYTES]

    def _flush_carry(self) -> None:
        if self._carry and self._carry_item is not None:
            padded = bytes(self._carry) + bytes(FRAME_BYTES - len(self._carry))
            self._out.append((self._carry_item, padded))
        self._carry.clear()

    def _can_play(self) -> bool:
        if not self._out:
            return False
        if not self._buffering:
            return True
        frames_needed = max(1, PREBUFFER_MS // FRAME_MS)
        if len(self._out) >= frames_needed or self._out[-1][0] in self._audio_done:
            self._buffering = False
            return True
        return False

    async def _pacer(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._can_play():
                item_id, frame = self._out.popleft()
                self.writer.write(pack_frame(KIND_AUDIO, frame))
                await self.writer.drain()
                self._played_ms[item_id] += FRAME_MS
                self._last_activity = loop.time()
                if not self._out:
                    # Ran dry: buffer again before the next audio plays.
                    self._buffering = True
                    if item_id not in self._audio_done:
                        self.underruns += 1
                        log.debug("[%s] audio underrun (%d)", self.call_id, self.underruns)
                next_tick += self.frame_seconds
                if next_tick < loop.time() - 0.1:  # fell behind: do not burst
                    next_tick = loop.time()
            else:
                # Nothing to say (or buffering): keep the media flowing with silence.
                self.writer.write(pack_frame(KIND_AUDIO, SILENCE_FRAME))
                await self.writer.drain()
                next_tick += self.frame_seconds
                if next_tick < loop.time() - 0.1:
                    next_tick = loop.time()

    async def _interrupt(self) -> None:
        """The caller starts talking: go silent now and tell the model what was heard."""
        pending: list[str] = []
        for item_id, _ in self._out:
            if item_id not in pending:
                pending.append(item_id)
        if self._carry_item and self._carry and self._carry_item not in pending:
            pending.append(self._carry_item)
        self._out.clear()
        self._carry.clear()
        self._buffering = True
        for item_id in pending:
            await self._send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": item_id,
                    "content_index": 0,
                    "audio_end_ms": self._played_ms.get(item_id, 0),
                }
            )
        if pending:
            log.debug("[%s] barge-in: truncated %d item(s)", self.call_id, len(pending))

    async def _finish_after_drain(self) -> None:
        while self._out or self._carry:
            await asyncio.sleep(0.05)
        await asyncio.sleep(FLUSH_GRACE_S)  # let the last audio reach the phone
        self._finished.set()

    # ------------------------------------------------------------- tools
    async def _run_tools(self, calls: list[dict[str, Any]]) -> None:
        only_hangup = all(c.get("name") == "hang_up" for c in calls)
        for call in calls:
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await self._dispatch(call.get("name", ""), args)
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(result, ensure_ascii=False),
                    },
                }
            )
        if self.voicemail:
            return  # the bridge leaves the message and hangs up: no more replies
        if self._hangup_pending and only_hangup:
            await self._finish_after_drain()
        else:
            await self._send({"type": "response.create"})

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "verify_pin":
            return self._check_pin(str(args.get("pin", "")))
        if name == "hang_up":
            self._hangup_pending = True
            return {"ok": True}
        if name == "report_voicemail" and self.outbound is not None and self.outbound.detect_voicemail:
            return self._on_voicemail()
        if name == "confirm_notice" and self.resolved:
            return {"ok": True, "message": "No confirmation is needed: the notice is resolved."}
        if name == "confirm_notice" and self.outbound is not None and self.outbound.confirm != "dtmf":
            self._confirm("implicit" if self.outbound.confirm == "implicit" else "voice")
            return {"ok": True, "message": "Confirmation recorded."}
        if name == "home_status" and (self.outbound is not None or self._pin_state == "locked"):
            return await self._home_status(str(args.get("search") or "").strip())
        if name == "home":
            request = str(args.get("request", "")).strip()
            if not request:
                return {"ok": False, "error": "The request is missing."}
            return await self._home_request(request)
        return {"ok": False, "error": f"Unknown tool: {name}"}

    async def _home_status(self, search: str) -> dict[str, Any]:
        if self.status is None:
            return {"ok": False, "error": "The state of the home is not available."}
        log.info("[%s] status%s", self.call_id, f": {search}" if search else "")
        try:
            extra = (self.outbound.context_entity,) if self.outbound and self.outbound.context_entity else ()
            return {"ok": True, **await self.status.snapshot(search, extra=extra)}
        except Exception as err:  # noqa: BLE001 - report it to the model
            log.warning("[%s] failed to read the home state: %r", self.call_id, err)
            return {"ok": False, "error": "Could not read the state of the home."}

    async def _home_request(self, request: str) -> dict[str, Any]:
        """The tool that can ACT: only with a verified PIN (or no PIN at all)."""
        if not self._authed:
            if self._pin_state == "locked":
                return {"ok": False, "error": "Nothing can be changed in this call."}
            if self.profile is None:
                return {"ok": False, "error": "PIN not verified. Ask for the PIN before continuing."}
            return {
                "ok": False,
                "error": "The PIN must be verified before changing anything. "
                "For questions use home_status.",
            }
        log.info("[%s] home: %s", self.call_id, request)
        try:
            return {"ok": True, **await self.home.ask(request)}
        except Exception as err:  # noqa: BLE001 - report it to the model
            log.warning("[%s] failed to talk to Home Assistant: %r", self.call_id, err)
            return {"ok": False, "error": "Could not reach Home Assistant."}

    # --------------------------------------------------------------- PIN
    def _match_pin(self, digits: str, suffix: bool = False) -> str | None:
        """Name of the PIN that matches ("" for the global one), or None."""
        found = None
        for name, pin in self._pins:
            candidate = digits[-len(pin):] if suffix else digits
            # Compare against every PIN (no early exit) to keep timing uniform.
            if candidate and hmac.compare_digest(candidate, pin) and found is None:
                found = name
        return found

    def _pin_verified(self, name: str, how: str) -> None:
        self._authed = True
        self.pin_person = name
        log.info("[%s] PIN verified%s%s", self.call_id, how, f" ({name})" if name else "")

    def _check_pin(self, candidate: str) -> dict[str, Any]:
        if self._pin_state == "locked":
            return {"ok": False, "error": "There is no PIN in this call: nothing can be changed."}
        digits = _extract_digits(candidate)
        if log.isEnabledFor(logging.DEBUG):
            for _, pin in self._pins:
                log.debug(
                    "[%s] PIN candidate: %d digits (expected %d), %d matching positions",
                    self.call_id, len(digits), len(pin),
                    sum(a == b for a, b in zip(digits, pin)),
                )
        if self._authed:
            return {"ok": True, "message": "The PIN was already verified."}
        name = self._match_pin(digits)
        if name is not None:
            self._pin_verified(name, "")
            return {"ok": True, "message": "PIN correct."}
        return self._pin_failed()

    def _pin_failed(self) -> dict[str, Any]:
        self._pin_attempts += 1
        log.warning(
            "[%s] wrong PIN (%d/%d)", self.call_id, self._pin_attempts, self.cfg.max_pin_attempts
        )
        if self._pin_attempts >= self.cfg.max_pin_attempts:
            self._hangup_pending = True
            return {
                "ok": False,
                "error": "Wrong PIN. No attempts left: say a brief goodbye without giving any hints.",
            }
        return {"ok": False, "error": "Wrong PIN. Ask for it again."}

    # ------------------------------------------------------- confirmation
    def _confirm(self, how: str) -> bool:
        """Record the confirmation of the notice; False if it was already confirmed."""
        if self.confirmed or self.resolved:
            return False
        self.confirmed = True
        self.confirmed_by = how
        log.info("[%s] notice confirmed (%s)", self.call_id, how)
        return True

    async def _confirm_by_key(self) -> None:
        if self._confirm("dtmf"):
            key = key_name(self.outbound.confirm_key) if self.outbound else ""
            await self._system_note(
                f"The person pressed the {key} key: the notice is confirmed. "
                "Acknowledge it briefly and ask whether they need anything else."
            )

    async def _confirm_key_after_wait(self, key: str) -> None:
        await asyncio.sleep(CONFIRM_KEY_WAIT_S)
        self._key_timer = None
        if self._dtmf == key:  # nothing else was typed: it was the confirmation
            self._dtmf = ""
            await self._confirm_by_key()

    def _cancel_key_timer(self) -> None:
        if self._key_timer is not None:
            self._key_timer.cancel()
            self._key_timer = None

    # --------------------------------------------------------------- keypad
    async def _on_dtmf(self, digit: str) -> None:
        """Keys: the confirmation key (outbound calls) and the PIN (more reliable than speaking it)."""
        if not digit:
            return
        self._cancel_key_timer()
        call = self.outbound
        pin_entry = self._pin_state == "pin" and not self._authed
        if (
            call is not None
            and call.confirm in ("dtmf", "voice_or_dtmf")
            and not self.confirmed
            and not self.resolved
            and digit == call.confirm_key
        ):
            if not pin_entry or (not digit.isdigit() and not self._dtmf):
                await self._confirm_by_key()
                return
            if digit.isdigit() and not self._dtmf:
                # It may be the confirmation or the first digit of a PIN.
                self._key_timer = asyncio.create_task(self._confirm_key_after_wait(digit))
                self._tool_tasks.add(self._key_timer)
                self._key_timer.add_done_callback(self._tool_tasks.discard)
        if not pin_entry:
            return
        if digit.isdigit():
            self._dtmf = (self._dtmf + digit)[-16:]
            name = self._match_pin(self._dtmf, suffix=True)
            if name is not None:
                self._cancel_key_timer()
                self._dtmf = ""
                self._pin_verified(name, " by keypad")
                await self._system_note(
                    "The caller typed the correct PIN on the keypad. Confirm it and ask how you can help."
                )
            return
        if digit == "#" and self._dtmf:
            self._dtmf = ""
            result = self._pin_failed()
            await self._system_note(result["error"])
        elif digit == "*":
            self._dtmf = ""

    async def note(self, text: str, resolve: bool = False) -> bool:
        """A message from Home Assistant for the person on the phone (no hang-up).

        With ``resolve`` the notice is closed: the assistant stops talking at
        once, says the note, asks for no confirmation and offers to answer a
        question before saying goodbye.
        """
        if not self.connected:
            return False
        if resolve:
            return await self._resolve(text)
        log.info("[%s] note from Home Assistant", self.call_id)
        await self._system_note(
            "Home Assistant has just sent a message for the person on the phone. "
            "Tell them now, briefly and in your own words, then carry on with the "
            f"call. The message: {json.dumps(text, ensure_ascii=False)}"
        )
        if self._response_active:
            self._note_pending = True
        return True

    async def _resolve(self, text: str) -> bool:
        log.info("[%s] note from Home Assistant: the notice is resolved", self.call_id)
        self.resolved = True
        self._reprompts = 0
        self._cancel_key_timer()
        if self._response_active:
            if self._response_id:
                self._dropped.add(self._response_id)
            await self._send({"type": "response.cancel"})
        await self._interrupt()
        self._touch()
        await self._system_note(
            "Stop what you were saying. Home Assistant has resolved the notice: it no "
            "longer needs any confirmation, so do not ask for one again. Tell the person "
            "now, briefly and in your own words: "
            f"{json.dumps(text, ensure_ascii=False)}. Then offer, in one short sentence, "
            "to answer any question; if they have none, say goodbye and call hang_up."
        )
        if self._response_active:
            self._note_pending = True  # said as soon as the cancelled response ends
        return True

    def _on_voicemail(self) -> dict[str, Any]:
        """The assistant heard a voicemail: the bridge takes over and hangs up.

        The message is left by the bridge itself (exact text, fallback voice),
        after the greeting and its beep: once the phone has been quiet for
        ``VOICEMAIL_QUIET_S``.
        """
        log.info("[%s] the assistant recognised a voicemail", self.call_id)
        self.voicemail = True
        self._hangup_pending = True
        self._spawn(self._leave_voicemail())
        return {"ok": True, "instruction": "Say nothing more. The call is being closed."}

    async def _leave_voicemail(self) -> None:
        if self._response_active:
            if self._response_id:
                self._dropped.add(self._response_id)
            await self._send({"type": "response.cancel"})
        await self._interrupt()
        message = self.outbound.voicemail_message if self.outbound else ""
        if message and self.voice is not None and getattr(self.voice, "enabled", False):
            try:
                pcm = await self.voice.audio(message)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] could not prepare the voicemail message: %r", self.call_id, err)
                pcm = b""
            if pcm:
                await self._wait_for_quiet()
                for i in range(0, len(pcm), FRAME_BYTES):
                    frame = pcm[i:i + FRAME_BYTES]
                    self._out.append(("voicemail", frame + bytes(FRAME_BYTES - len(frame))))
                self._audio_done.add("voicemail")
                while self._out:
                    await asyncio.sleep(0.05)
                self.voicemail_message_left = True
                log.info("[%s] voicemail message left", self.call_id)
        elif message:
            log.warning("[%s] voicemail_message needs fallback_tts: no message left", self.call_id)
        await asyncio.sleep(FLUSH_GRACE_S)
        self._finished.set()

    async def _wait_for_quiet(self) -> None:
        """Wait until the greeting (and its beep) is over."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + VOICEMAIL_MAX_WAIT_S
        while loop.time() < deadline:
            if loop.time() - self._far_voice >= VOICEMAIL_QUIET_S:
                return
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------ silence
    def _touch(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()

    def _caller_active(self) -> None:
        """The caller spoke or pressed a key: they are there."""
        self._touch()
        self._reprompts = 0

    def _busy(self) -> bool:
        return bool(
            self._response_active or self._out or self._carry
            or any(not t.done() for t in self._tool_tasks)
        )

    async def _silence_watch(self) -> None:
        """Nobody answers (silence or an answering machine): ask again, then hang up."""
        limit = self.cfg.silence_seconds
        if limit <= 0:
            await asyncio.Event().wait()
        await self._ready.wait()
        self._touch()
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(min(0.5, limit / 4))
            if self._busy() or self._hangup_pending:
                self._touch()
                continue
            if loop.time() - self._last_activity < limit:
                continue
            self._touch()
            if self.resolved or self._reprompts >= self.cfg.silence_reprompts:
                log.info("[%s] no answer for %ss: hanging up", self.call_id, limit)
                self.silence_ended = True
                self._hangup_pending = True
                await self._system_note("Nobody is answering. Say a very brief goodbye now.")
                await asyncio.sleep(15)  # in case the goodbye never comes
                self._finished.set()
                await asyncio.Event().wait()
            self._reprompts += 1
            log.info("[%s] silence (%ss): asking again (%d/%d)",
                     self.call_id, limit, self._reprompts, self.cfg.silence_reprompts)
            pending = self.outbound is not None and not self.confirmed and self.outbound.confirm != "implicit"
            await self._system_note(
                f"The person has not said anything for {limit} seconds. Ask again, in one "
                "short sentence, whether they can hear you"
                + (" and whether they received the notice." if pending else ".")
            )

    async def _system_note(self, text: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if not self._response_active:
            await self._send({"type": "response.create"})
