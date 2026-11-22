"""Fallback when OpenAI is unavailable: Home Assistant's TTS (for example Piper).

* Outbound calls: the notice is read twice and the person confirms with the
  key (speech cannot be understood without OpenAI). The notice audio is
  generated as soon as Home Assistant registers the call, while the phone rings.
* Incoming calls: a short "not available" message, then the call ends.

The audio comes from Home Assistant's TTS API, already converted to 8 kHz mono
16-bit WAV, so it is played as it is.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import sys
import wave
from typing import Any, Protocol

from .audiosocket import FRAME_BYTES, FRAME_MS, KIND_AUDIO, KIND_DTMF, KIND_ERROR, KIND_HANGUP, pack_frame, read_frame
from .outbound import OutboundCall

log = logging.getLogger(__name__)

SAMPLE_RATE = 8000
KEY_WAIT_S = 12.0
PREPARE_WAIT_S = 6.0
SILENCE = bytes(FRAME_BYTES)

PHRASES = {
    "es": {
        "notice": "Aviso de casa. {message}",
        "press": "Pulse {key} para confirmar que ha recibido el aviso.",
        "confirmed": "Aviso confirmado. Gracias.",
        "bye": "El asistente no está disponible para preguntas en este momento. Hasta luego.",
        "inbound": "Lo siento, el asistente no está disponible en este momento. Inténtelo más tarde.",
        "keys": {"#": "la tecla almohadilla", "*": "la tecla asterisco"},
        "digit": "el {key}",
    },
    "en": {
        "notice": "Home notice. {message}",
        "press": "Press {key} to confirm that you have received this notice.",
        "confirmed": "Notice confirmed. Thank you.",
        "bye": "The assistant cannot answer questions right now. Goodbye.",
        "inbound": "Sorry, the assistant is not available right now. Please try again later.",
        "keys": {"#": "the hash key", "*": "the star key"},
        "digit": "{key}",
    },
}


def phrases_for(language: str) -> dict[str, Any]:
    base = language.strip().lower().replace("_", "-").split("-")[0]
    return PHRASES.get(base, PHRASES["en"])


def to_slin8k(wav_bytes: bytes) -> bytes:
    """WAV -> 16-bit signed linear, 8 kHz, mono (converting if Home Assistant did not)."""
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width == 2:
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
    elif width == 1:  # unsigned 8-bit
        samples = array.array("h", ((b - 128) << 8 for b in raw))
    elif width == 4:
        wide = array.array("i")
        wide.frombytes(raw)
        if sys.byteorder == "big":
            wide.byteswap()
        samples = array.array("h", (v >> 16 for v in wide))
    else:
        raise ValueError(f"unsupported sample width: {width}")
    if channels > 1:
        samples = array.array(
            "h", (sum(samples[i:i + channels]) // channels for i in range(0, len(samples), channels))
        )
    if rate != SAMPLE_RATE and samples:
        step = rate / SAMPLE_RATE
        count = int(len(samples) / step)
        samples = array.array("h", (samples[min(int(i * step), len(samples) - 1)] for i in range(count)))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


class _TTS(Protocol):
    async def tts_audio(self, engine_id: str, message: str, language: str = "", voice: str = "") -> bytes: ...


class Fallback:
    """Generates and plays the fallback audio."""

    def __init__(self, tts: _TTS, engine: str, language: str, voice: str = "") -> None:
        self._tts = tts
        self.engine = engine
        self._language = language
        self._voice = voice
        self._phrases = phrases_for(language)
        self._cache: dict[str, bytes] = {}
        self._pending: dict[str, asyncio.Task] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.engine)

    def key_phrase(self, key: str) -> str:
        if key in self._phrases["keys"]:
            return self._phrases["keys"][key]
        return self._phrases["digit"].format(key=key)

    def texts_for(self, call: OutboundCall) -> dict[str, str]:
        return {
            "notice": self._phrases["notice"].format(message=call.message),
            "press": self._phrases["press"].format(key=self.key_phrase(call.confirm_key)),
        }

    async def audio(self, text: str) -> bytes:
        """Speech for ``text`` (cached; concurrent requests share one synthesis)."""
        if text in self._cache:
            return self._cache[text]
        task = self._pending.get(text)
        if task is None:
            task = asyncio.create_task(self._synthesize(text))
            self._pending[text] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._pending.pop(text, None)

    async def _synthesize(self, text: str) -> bytes:
        pcm = to_slin8k(await self._tts.tts_audio(self.engine, text, self._language, self._voice))
        self._cache[text] = pcm
        return pcm

    def forget(self, call: OutboundCall) -> None:
        """Free the audio of a finished call (fixed phrases stay cached)."""
        for text in self.texts_for(call).values():
            self._cache.pop(text, None)

    async def prepare_fixed(self) -> None:
        for key in ("confirmed", "bye", "inbound"):
            try:
                await self.audio(self._phrases[key])
            except Exception as err:  # noqa: BLE001
                log.warning("Fallback TTS (%s) is not working: %r", self.engine, err)
                return
        log.info("Fallback TTS ready (%s)", self.engine)

    async def prepare_call(self, call: OutboundCall) -> None:
        """Generate a call's audio while the phone rings."""
        for text in self.texts_for(call).values():
            try:
                await self.audio(text)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] could not prepare the fallback audio: %r", call.short_id, err)
                return
        log.debug("[%s] fallback audio ready", call.short_id)

    async def _get(self, text: str) -> bytes:
        try:
            return await asyncio.wait_for(self.audio(text), PREPARE_WAIT_S)
        except Exception as err:  # noqa: BLE001
            log.warning("Fallback TTS failed: %r", err)
            return b""

    # ----------------------------------------------------------------- calls
    async def outbound(
        self, reader: asyncio.StreamReader, writer: Any, call: OutboundCall,
        frame_seconds: float = FRAME_MS / 1000,
    ) -> bool:
        """Read the notice and wait for the key. Returns True if confirmed."""
        texts = self.texts_for(call)
        notice, press = await self._get(texts["notice"]), await self._get(texts["press"])
        if not notice:
            return False
        player = _Player(reader, writer, call.confirm_key, frame_seconds)
        return await player.notice(
            notice, press,
            await self._get(self._phrases["confirmed"]),
            await self._get(self._phrases["bye"]),
        )

    async def inbound(
        self, reader: asyncio.StreamReader, writer: Any, frame_seconds: float = FRAME_MS / 1000
    ) -> None:
        audio = await self._get(self._phrases["inbound"])
        if audio:
            await _Player(reader, writer, "", frame_seconds).say(audio)


class _Player:
    """Plays audio to Asterisk at real time while watching the keypad and hang-ups."""

    def __init__(self, reader: asyncio.StreamReader, writer: Any, key: str, frame_seconds: float) -> None:
        self.reader = reader
        self.writer = writer
        self.key = key
        self.frame_seconds = frame_seconds
        self.confirmed = asyncio.Event()
        self.gone = asyncio.Event()

    async def _listen(self) -> None:
        while True:
            try:
                kind, payload = await read_frame(self.reader)
            except (asyncio.IncompleteReadError, ConnectionError):
                self.gone.set()
                return
            if kind in (KIND_HANGUP, KIND_ERROR):
                self.gone.set()
                return
            if kind == KIND_DTMF and self.key and payload[:1].decode("ascii", "ignore") == self.key:
                self.confirmed.set()

    async def _play(self, pcm: bytes, stop_on_key: bool = False) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        for start in range(0, len(pcm), FRAME_BYTES):
            if self.gone.is_set() or (stop_on_key and self.confirmed.is_set()):
                return
            frame = pcm[start:start + FRAME_BYTES]
            self.writer.write(pack_frame(KIND_AUDIO, frame + bytes(FRAME_BYTES - len(frame))))
            await self.writer.drain()
            next_tick += self.frame_seconds
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    async def _silence(self, seconds: float, stop_on_key: bool = True) -> None:
        frames = int(seconds / self.frame_seconds)
        await self._play(SILENCE * frames, stop_on_key=stop_on_key)

    async def say(self, pcm: bytes) -> None:
        listener = asyncio.create_task(self._listen())
        try:
            await self._play(pcm)
            await self._silence(0.5, stop_on_key=False)
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    async def notice(self, notice: bytes, press: bytes, confirmed: bytes, bye: bytes) -> bool:
        listener = asyncio.create_task(self._listen())
        try:
            for _ in range(2):
                await self._play(notice, stop_on_key=True)
                await self._silence(0.4)
                if press:
                    await self._play(press, stop_on_key=True)
                await self._silence(1.0)
                if self.confirmed.is_set() or self.gone.is_set():
                    break
            if not self.confirmed.is_set() and not self.gone.is_set():
                await self._silence(KEY_WAIT_S)
            if self.confirmed.is_set() and confirmed:
                await self._play(confirmed)
                await self._silence(0.3, stop_on_key=False)
            if bye:
                await self._play(bye)
            await self._silence(0.5, stop_on_key=False)
            return self.confirmed.is_set()
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
