"""Local speech-to-text through Home Assistant (e.g. Whisper), and a test mode.

Pieces for a future local conversation when OpenAI is unavailable:

* ``Segmenter``: a simple energy-based voice activity detector that cuts the
  caller's audio into utterances.
* ``upsample``: 8 kHz telephone audio -> 16 kHz, the rate Whisper expects.
* ``SttTest``: a diagnostic call mode, armed from Home Assistant through the
  add-on's stdin (``{"action": "stt_test"}``). The next incoming call does not
  go to OpenAI: each utterance is transcribed and read back with the fallback
  voice, and the timings are logged. It never acts on the home.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import sys
import time
import wave
from collections import deque
from typing import Any, Protocol

from .audiosocket import FRAME_BYTES, FRAME_MS, KIND_AUDIO, KIND_ERROR, KIND_HANGUP, pack_frame, read_frame

log = logging.getLogger(__name__)

TEST_ARM_S = 300  # the test applies to the next call within this time
TEST_MAX_S = 180


def rms(frame: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) - len(frame) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def upsample(pcm8k: bytes) -> bytes:
    """8 kHz -> 16 kHz by linear interpolation (16-bit mono)."""
    src = array.array("h")
    src.frombytes(pcm8k[: len(pcm8k) - len(pcm8k) % 2])
    if sys.byteorder == "big":
        src.byteswap()
    out = array.array("h", bytes(len(src) * 4))
    for i, value in enumerate(src):
        nxt = src[i + 1] if i + 1 < len(src) else value
        out[2 * i] = value
        out[2 * i + 1] = (value + nxt) // 2
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class Segmenter:
    """Cuts 20 ms frames into utterances using their energy.

    The threshold adapts to the line's background noise.
    """

    def __init__(
        self,
        min_threshold: float = 400.0,
        start_frames: int = 3,
        end_silence_ms: int = 700,
        min_speech_ms: int = 300,
        max_speech_ms: int = 15000,
        preroll_frames: int = 10,
    ) -> None:
        self.min_threshold = min_threshold
        self.start_frames = start_frames
        self.end_frames = end_silence_ms // FRAME_MS
        self.min_frames = min_speech_ms // FRAME_MS
        self.max_frames = max_speech_ms // FRAME_MS
        self.noise = min_threshold / 3
        self._preroll: deque[bytes] = deque(maxlen=preroll_frames)
        self._voiced = 0
        self._silent = 0
        self._speech: list[bytes] | None = None

    @property
    def threshold(self) -> float:
        return max(self.min_threshold, self.noise * 3)

    @property
    def speaking(self) -> bool:
        return self._speech is not None

    def feed(self, frame: bytes) -> bytes | None:
        """Returns a finished utterance (PCM) or None."""
        level = rms(frame)
        loud = level > self.threshold
        if self._speech is None:
            if not loud:
                self.noise = 0.95 * self.noise + 0.05 * level
            self._preroll.append(frame)
            self._voiced = self._voiced + 1 if loud else 0
            if self._voiced >= self.start_frames:
                self._speech = list(self._preroll)
                self._silent = 0
            return None
        self._speech.append(frame)
        self._silent = 0 if loud else self._silent + 1
        if self._silent >= self.end_frames or len(self._speech) >= self.max_frames:
            speech, self._speech = self._speech, None
            self._voiced = 0
            self._preroll.clear()
            voiced = len(speech) - self._silent
            if voiced >= self.min_frames:
                return b"".join(speech)
        return None


class _HA(Protocol):
    async def stt(self, engine_id: str, wav16k: bytes, language: str) -> str: ...


class _Voice(Protocol):
    async def audio(self, text: str) -> bytes: ...


class SttTest:
    """Diagnostic call: transcribe each utterance and say what was understood."""

    def __init__(self, ha: _HA, engine: str, language: str, voice: _Voice | None) -> None:
        self.ha = ha
        self.engine = engine
        self.language = language
        self.voice = voice
        self._armed_until = 0.0
        self.results: list[dict[str, Any]] = []

    def arm(self) -> None:
        self._armed_until = time.monotonic() + TEST_ARM_S
        log.info("STT test armed: the next incoming call (within %d s) is a transcription test "
                 "with %s", TEST_ARM_S, self.engine)

    def take(self) -> bool:
        if time.monotonic() < self._armed_until:
            self._armed_until = 0.0
            return True
        return False

    async def run(self, call_id: str, reader: asyncio.StreamReader, writer: Any) -> None:
        out: deque[bytes] = deque()
        utterances: asyncio.Queue[bytes] = asyncio.Queue()
        gone = asyncio.Event()

        async def say(text: str) -> None:
            if self.voice is None:
                return
            try:
                pcm = await self.voice.audio(text)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] stt test: no voice: %r", call_id, err)
                return
            for i in range(0, len(pcm), FRAME_BYTES):
                frame = pcm[i:i + FRAME_BYTES]
                out.append(frame + bytes(FRAME_BYTES - len(frame)))

        async def player() -> None:
            loop = asyncio.get_running_loop()
            tick = loop.time()
            while not gone.is_set():
                frame = out.popleft() if out else bytes(FRAME_BYTES)
                writer.write(pack_frame(KIND_AUDIO, frame))
                await writer.drain()
                tick += FRAME_MS / 1000
                delay = tick - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -0.1:
                    tick = loop.time()

        async def listener() -> None:
            seg = Segmenter()
            while True:
                try:
                    kind, payload = await read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                if kind in (KIND_HANGUP, KIND_ERROR):
                    break
                if kind == KIND_AUDIO and not out:  # ignore our own voice playing
                    speech = seg.feed(payload)
                    if speech:
                        utterances.put_nowait(speech)
            gone.set()

        async def transcriber() -> None:
            await say("Modo de prueba de transcripción. Diga una frase y espere la respuesta.")
            n = 0
            while not gone.is_set():
                get = asyncio.create_task(utterances.get())
                stop = asyncio.create_task(gone.wait())
                done, _ = await asyncio.wait({get, stop}, return_when=asyncio.FIRST_COMPLETED)
                stop.cancel()
                if get not in done:
                    get.cancel()
                    break
                speech = get.result()
                n += 1
                seconds = len(speech) / 2 / 8000
                started = time.monotonic()
                try:
                    text = await self.ha.stt(self.engine, to_wav(upsample(speech), 16000), self.language)
                except Exception as err:  # noqa: BLE001
                    log.warning("[%s] stt test #%d failed: %s", call_id, n, err)
                    await say("No he podido transcribirlo.")
                    continue
                took = time.monotonic() - started
                self.results.append({"audio_s": round(seconds, 2), "stt_s": round(took, 2), "text": text})
                log.info("[%s] stt test #%d: %.1f s of audio -> %.2f s: %r", call_id, n, seconds, took, text)
                await say(f"He entendido: {text}" if text else "No he entendido nada.")

        tasks = [asyncio.create_task(t()) for t in (player, listener, transcriber)]
        try:
            await asyncio.wait_for(gone.wait(), TEST_MAX_S)
        except asyncio.TimeoutError:
            log.info("[%s] stt test: time limit", call_id)
        finally:
            gone.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.results:
                avg = sum(r["stt_s"] for r in self.results) / len(self.results)
                log.info("[%s] stt test summary: %d utterance(s), average transcription %.2f s",
                         call_id, len(self.results), avg)
