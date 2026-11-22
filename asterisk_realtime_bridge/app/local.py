"""Local conversation when OpenAI is unavailable (``local_conversation``).

Whisper (Home Assistant STT) -> Home Assistant's own Assist -> Piper (Home
Assistant TTS). Slower and more literal than OpenAI, but it keeps the same
rules:

* The PIN is typed on the keypad only (spoken numbers are not reliable).
* Incoming calls: nothing until the PIN is verified.
* Outbound calls: the notice is read; confirmation by key, and by voice when
  the call allows it. Questions without a PIN are answered only if Home
  Assistant's built-in agent recognises them as a read-only intent (checked
  BEFORE executing, with its debug API) about entities in the read-only scope.
  Changes need the PIN (Management) and are impossible in QueryOnly.
* While the assistant speaks it does not listen (no barge-in).
"""

from __future__ import annotations

import array
import asyncio
import hmac
import logging
import random
import sys
import time
import unicodedata
from collections import deque
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
from .config import PROFILE_MANAGE, Config
from .fallback import Fallback
from .outbound import OutboundCall
from .pins import CallAuth
from .session import call_auth
from .stt import Segmenter, to_wav, upsample

log = logging.getLogger(__name__)

CONFIRM_KEY_WAIT_S = 2.0

# Intents of Home Assistant's built-in agent that only read.
READ_ONLY_INTENTS = {
    "HassGetState",
    "HassClimateGetTemperature",
    "HassGetWeather",
    "HassGetCurrentTime",
    "HassGetCurrentDate",
    "HassGetTemperature",
}

# Spoken phrases. "pin" is lower case on purpose: TTS engines such as Piper
# spell an upper-case "PIN" letter by letter.
TEXT = {
    "es": {
        "intro_pin": "El asistente funciona en modo reducido. Teclee su pin.",
        "intro": "El asistente funciona en modo reducido. ¿Qué necesita?",
        "pin_ok": "pin correcto. ¿Qué necesita?",
        "pin_bad": "pin incorrecto.",
        "pin_first": "Primero teclee su pin.",
        "pin_for_changes": "Para hacer cambios, teclee su pin.",
        "no_changes": "Desde esta llamada no se puede cambiar nada.",
        "not_understood": "No lo he entendido. Dígalo de otra forma.",
        "no_answer": "No he podido hacerlo.",
        "questions": "También puede hacerme preguntas.",
        "confirmed": "Aviso confirmado. Gracias.",
        "still_there": "¿Sigue ahí?",
        "resolved_more": "Si tiene alguna pregunta, dígamela; si no, me despido.",
        "bye": "Hasta luego.",
    },
    "en": {
        "intro_pin": "The assistant is in reduced mode. Type your pin.",
        "intro": "The assistant is in reduced mode. How can I help?",
        "pin_ok": "pin correct. How can I help?",
        "pin_bad": "Wrong pin.",
        "pin_first": "Type your pin first.",
        "pin_for_changes": "To change anything, type your pin.",
        "no_changes": "Nothing can be changed from this call.",
        "not_understood": "I did not understand. Please say it another way.",
        "no_answer": "I could not do it.",
        "questions": "You can also ask me questions.",
        "confirmed": "Notice confirmed. Thank you.",
        "still_there": "Are you still there?",
        "resolved_more": "If you have any question, tell me; otherwise, goodbye.",
        "bye": "Goodbye.",
    },
}

YES = {"es": ("si", "sí", "recibido", "confirmo", "confirmado", "vale", "de acuerdo", "entendido", "ok"),
       "en": ("yes", "received", "confirm", "confirmed", "ok", "okay", "understood", "got it")}
BYE = {"es": ("adios", "adiós", "hasta luego", "nada mas", "nada más", "eso es todo", "cuelga"),
       "en": ("bye", "goodbye", "nothing else", "that's all", "hang up")}


def typing_sound(seed: int | None = None):
    """Endless 20 ms frames of soft keyboard clicks, played while the assistant works.

    Generated here (no audio files): short decaying noise bursts at irregular
    intervals, with an occasional pause, at a low volume.
    """
    rng = random.Random(seed)
    samples_per_frame = FRAME_BYTES // 2
    buffer: list[int] = []
    while True:
        while len(buffer) < samples_per_frame:
            click = 40  # 5 ms at 8 kHz
            amplitude = rng.randint(900, 1800)
            buffer.extend(int(rng.uniform(-1, 1) * amplitude * (1 - i / click)) for i in range(click))
            gap_ms = rng.choice((70, 90, 110, 130, 160, 200)) if rng.random() > 0.12 else rng.randint(300, 550)
            buffer.extend([0] * (gap_ms * 8))
        frame = array.array("h", buffer[:samples_per_frame])
        del buffer[:samples_per_frame]
        if sys.byteorder == "big":
            frame.byteswap()
        yield frame.tobytes()


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c) and (c.isalnum() or c == " ")).strip()


def has_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    words = f" {_norm(text)} "
    return any(f" {_norm(p)} " in words for p in phrases)


class _Home(Protocol):
    async def ask(self, text: str) -> dict[str, Any]: ...
    async def stt(self, engine_id: str, wav16k: bytes, language: str) -> str: ...
    async def ws(self, command: str, **kwargs: Any) -> Any: ...
    async def converse(self, agent_id: str, text: str) -> dict[str, Any]: ...


class _Scope(Protocol):
    async def allows(self, entity_ids: list[str]) -> bool: ...


class LocalConversation:
    def __init__(
        self,
        cfg: Config,
        call_id: str,
        reader: asyncio.StreamReader,
        writer: Any,
        home: _Home,
        voice: Fallback,
        stt_engine: str,
        scope: _Scope | None,
        outbound: OutboundCall | None = None,
        frame_seconds: float = FRAME_MS / 1000,
        auth: CallAuth | None = None,
    ) -> None:
        self.cfg = cfg
        self.call_id = call_id
        self.reader = reader
        self.writer = writer
        self.home = home
        self.voice = voice
        self.stt_engine = stt_engine
        self.scope = scope
        self.outbound = outbound
        self.frame_seconds = frame_seconds
        lang = cfg.language.split("-")[0].lower()
        self.lang = lang if lang in TEXT else "en"
        self.text = TEXT[self.lang]
        self.auth = auth or call_auth(cfg, outbound)
        self._pins = self.auth.pins
        self._pin_state = self.auth.state
        self.authed = self._pin_state == "open"
        self.pin_person: str | None = None
        self.confirmed = False
        self.confirmed_by: str | None = None
        self.resolved = False  # Home Assistant closed the notice with a note (resolve)
        self.silence_ended = False  # hung up because nobody answered
        self._attempts = 0
        self._keys = ""
        self._key_timer: asyncio.Task | None = None
        self._out: deque[bytes] = deque()
        self._thinking = False  # play the typing sound while working
        self._typing = typing_sound()
        self._events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._gone = asyncio.Event()
        self._done = False

    # ---------------------------------------------------------------- audio
    async def say(self, text: str) -> None:
        try:
            pcm = await self.voice.audio(text)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] local: no voice: %r", self.call_id, err)
            return
        for i in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[i:i + FRAME_BYTES]
            self._out.append(frame + bytes(FRAME_BYTES - len(frame)))

    async def _speaking_done(self) -> None:
        while self._out and not self._gone.is_set():
            await asyncio.sleep(self.frame_seconds)

    async def _player(self) -> None:
        loop = asyncio.get_running_loop()
        tick = loop.time()
        while not self._gone.is_set():
            if self._out:
                frame = self._out.popleft()
            elif self._thinking:
                frame = next(self._typing)
            else:
                frame = bytes(FRAME_BYTES)
            self.writer.write(pack_frame(KIND_AUDIO, frame))
            await self.writer.drain()
            tick += self.frame_seconds
            delay = tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.1:
                tick = loop.time()

    async def _listener(self) -> None:
        seg = Segmenter()
        while True:
            try:
                kind, payload = await read_frame(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            if kind in (KIND_HANGUP, KIND_ERROR):
                break
            if kind == KIND_DTMF and payload:
                self._events.put_nowait(("key", payload[:1].decode("ascii", "ignore")))
            elif kind == KIND_AUDIO and not self._out:  # no barge-in: ignore while speaking
                speech = seg.feed(payload)
                if speech:
                    self._events.put_nowait(("speech", speech))
        self._gone.set()
        self._events.put_nowait(("gone", None))

    async def note(self, text: str, resolve: bool = False) -> bool:
        """A message from Home Assistant for the person on the phone (no hang-up).

        With ``resolve`` the notice is closed: what is being said is cut, the
        note is said at once and no confirmation is asked for any more.
        """
        if self._gone.is_set():
            return False
        if resolve:
            log.info("[%s] local: note from Home Assistant: the notice is resolved", self.call_id)
            self.resolved = True
            if self._key_timer:
                self._key_timer.cancel()
            self._out.clear()
            await self.say(text)
            await self.say(self.text["resolved_more"])
            self._events.put_nowait(("resolved", None))  # restart the silence count
            return True
        log.info("[%s] local: note from Home Assistant", self.call_id)
        await self.say(text)
        return True

    # ---------------------------------------------------------------- flow
    async def run(self) -> None:
        tasks = [asyncio.create_task(self._player()), asyncio.create_task(self._listener())]
        try:
            await asyncio.wait_for(self._converse(), self.cfg.max_call_seconds)
        except asyncio.TimeoutError:
            log.info("[%s] local: maximum call duration reached", self.call_id)
        finally:
            if self._key_timer:
                self._key_timer.cancel()
            self._gone.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _converse(self) -> None:
        call = self.outbound
        if call is not None:
            texts = self.voice.texts_for(call)
            await self.say(texts["notice"])
            await self.say(texts["press"])  # the key always works in local mode
            await self.say(self.text["questions"])
        else:
            await self.say(self.text["intro_pin"] if self._pin_state == "pin" else self.text["intro"])
        limit = self.cfg.silence_seconds or None  # None: wait for ever
        asked = 0
        while not self._done:
            await self._speaking_done()
            try:
                kind, value = await asyncio.wait_for(self._events.get(), limit)
            except asyncio.TimeoutError:
                if self.resolved or asked >= self.cfg.silence_reprompts:
                    log.info("[%s] local: no answer for %ss: hanging up", self.call_id, limit)
                    self.silence_ended = True
                    await self.say(self.text["bye"])
                    await self._speaking_done()
                    return
                asked += 1
                log.info("[%s] local: silence (%ss): asking again (%d/%d)",
                         self.call_id, limit, asked, self.cfg.silence_reprompts)
                await self.say(self.text["still_there"])
                continue
            if kind == "resolved":
                continue
            asked = 0
            if kind == "gone":
                return
            if kind == "key":
                await self._on_key(value)
            elif kind == "speech":
                await self._on_speech(value)
        await self._speaking_done()

    def _confirm(self, how: str) -> None:
        if self.outbound is not None and not self.confirmed and not self.resolved:
            self.confirmed, self.confirmed_by = True, how
            log.info("[%s] local: notice confirmed (%s)", self.call_id, how)

    # ----------------------------------------------------------------- keys
    async def _on_key(self, key: str) -> None:
        call = self.outbound
        pin_entry = self._pin_state == "pin" and not self.authed
        if call is not None and not self.confirmed and not self.resolved and key == call.confirm_key:
            if not pin_entry or (not key.isdigit() and not self._keys):
                self._confirm("dtmf")
                await self.say(self.text["confirmed"])
                return
            if key.isdigit() and not self._keys:
                self._key_timer = asyncio.create_task(self._confirm_after_wait(key))
        if not pin_entry:
            return
        if self._key_timer and key != (call.confirm_key if call else None):
            self._key_timer.cancel()
        if key.isdigit():
            self._keys = (self._keys + key)[-16:]
            for name, pin in self._pins:
                if hmac.compare_digest(self._keys[-len(pin):], pin):
                    if self._key_timer:
                        self._key_timer.cancel()
                    self._keys = ""
                    self.authed, self.pin_person = True, name
                    log.info("[%s] local: PIN verified by keypad%s", self.call_id, f" ({name})" if name else "")
                    await self.say(self.text["pin_ok"])
                    return
        elif key == "#" and self._keys:
            self._keys = ""
            self._attempts += 1
            log.warning("[%s] local: wrong PIN (%d/%d)", self.call_id, self._attempts, self.cfg.max_pin_attempts)
            await self.say(self.text["pin_bad"])
            if self._attempts >= self.cfg.max_pin_attempts:
                await self.say(self.text["bye"])
                self._done = True
        elif key == "*":
            self._keys = ""

    async def _confirm_after_wait(self, key: str) -> None:
        await asyncio.sleep(CONFIRM_KEY_WAIT_S)
        if self._keys == key and not self.resolved:
            self._keys = ""
            self._confirm("dtmf")
            await self.say(self.text["confirmed"])

    # --------------------------------------------------------------- speech
    async def _on_speech(self, pcm: bytes) -> None:
        self._thinking = True
        try:
            await self._handle_speech(pcm)
        finally:
            self._thinking = False

    async def _handle_speech(self, pcm: bytes) -> None:
        started = time.monotonic()
        try:
            text = await self.home.stt(self.stt_engine, to_wav(upsample(pcm), 16000), self.lang)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] local: transcription failed: %s", self.call_id, err)
            await self.say(self.text["not_understood"])
            return
        log.info("[%s] local heard (%.1fs): %r", self.call_id, time.monotonic() - started, text)
        if not text:
            return
        call = self.outbound
        if call is not None and not self.confirmed and call.confirm == "implicit":
            self._confirm("implicit")  # any reaction to the notice, a goodbye included
        if has_phrase(text, BYE[self.lang]):
            await self.say(self.text["bye"])
            self._done = True
            return
        if call is not None and not self.confirmed and not self.resolved:
            if call.confirm in ("voice", "voice_or_dtmf") and has_phrase(text, YES[self.lang]):
                self._confirm("voice")
                await self.say(self.text["confirmed"])
                return
        if self.authed:
            await self._act(text)
        elif call is None and self._pin_state == "pin":
            await self.say(self.text["pin_first"])
        else:
            await self._read_only(text)

    async def _act(self, text: str) -> None:
        log.info("[%s] local home: %s", self.call_id, text)
        try:
            result = await self.home.ask(text)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] local: Home Assistant failed: %s", self.call_id, err)
            await self.say(self.text["no_answer"])
            return
        await self.say(result.get("response") or self.text["no_answer"])

    async def _read_only(self, text: str) -> None:
        """Answer only if the built-in agent sees a read-only intent within scope."""
        try:
            debug = await self.home.ws(
                "conversation/agent/homeassistant/debug", sentences=[text], language=self.lang
            )
            match = ((debug or {}).get("results") or [{}])[0] or {}
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] local: intent check failed: %s", self.call_id, err)
            await self.say(self.text["no_answer"])
            return
        intent = ((match.get("intent") or {}).get("name")) or ""
        if not match.get("match"):
            await self.say(self.text["not_understood"])
            return
        if intent not in READ_ONLY_INTENTS:
            log.info("[%s] local: %s refused without PIN (%s)", self.call_id, intent, text)
            await self.say(
                self.text["pin_for_changes"]
                if self._pin_state == "pin" and self.outbound is not None
                and self.outbound.profile == PROFILE_MANAGE
                else self.text["no_changes"]
            )
            return
        targets = list((match.get("targets") or {}).keys())
        if self.scope is not None and not await self.scope.allows(targets):
            log.info("[%s] local: question outside the read-only scope: %s", self.call_id, text)
            await self.say(self.text["no_answer"])
            return
        log.info("[%s] local query (%s): %s", self.call_id, intent, text)
        try:
            result = await self.home.converse("conversation.home_assistant", text)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] local: Home Assistant failed: %s", self.call_id, err)
            await self.say(self.text["no_answer"])
            return
        await self.say(result.get("response") or self.text["no_answer"])
