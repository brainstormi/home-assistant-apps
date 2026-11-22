import array
import asyncio
import io
import json
import os
import sys
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.fallback as fb  # noqa: E402
from app.audiosocket import FRAME_BYTES, KIND_AUDIO, KIND_DTMF, KIND_HANGUP, pack_frame  # noqa: E402
from app.fallback import Fallback, phrases_for, to_slin8k  # noqa: E402
from app.health import OpenAIHealth, describe_error, realtime_probe  # noqa: E402
from app.outbound import OutboundCall  # noqa: E402

from tests.test_session import FakeWriter  # noqa: E402

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"


def make_wav(rate=8000, channels=1, seconds=0.1, value=1000):
    samples = array.array("h", [value] * int(rate * seconds) * channels)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


class FakeTTS:
    def __init__(self):
        self.calls = []

    async def tts_audio(self, engine_id, message, language="", voice=""):
        self.calls.append((engine_id, message, language, voice))
        return make_wav(seconds=0.06)  # 3 frames


class ConversionTests(unittest.TestCase):
    def test_8k_mono_passes_through(self):
        pcm = to_slin8k(make_wav())
        self.assertEqual(len(pcm), 800 * 2)
        self.assertEqual(array.array("h", pcm[:4])[0], 1000)

    def test_resample_and_mix(self):
        pcm = to_slin8k(make_wav(rate=16000, channels=2, seconds=0.1, value=2000))
        self.assertEqual(len(pcm), 800 * 2, "16 kHz stereo -> 8 kHz mono")
        self.assertEqual(array.array("h", pcm[:2])[0], 2000)


class PhraseTests(unittest.TestCase):
    def test_spanish_phrases_and_keys(self):
        fallback = Fallback(FakeTTS(), "tts.piper", "es")
        texts = fallback.texts_for(OutboundCall(CALL_ID, "Ha saltado la alarma.", "QueryOnly", confirm_key="#"))
        self.assertEqual(texts["notice"], "Aviso de casa. Ha saltado la alarma.")
        self.assertIn("la tecla almohadilla", texts["press"])
        self.assertIn("el 1", fallback.texts_for(OutboundCall(CALL_ID, "x", "QueryOnly"))["press"])
        self.assertEqual(phrases_for("de")["confirmed"], phrases_for("en")["confirmed"])


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original = (fb.KEY_WAIT_S,)
        fb.KEY_WAIT_S = 0.05

    async def asyncTearDown(self):
        (fb.KEY_WAIT_S,) = self.original

    async def test_audio_is_cached_and_prepared_before_the_call(self):
        tts = FakeTTS()
        fallback = Fallback(tts, "tts.piper", "es", voice="es_ES-davefx-medium")
        call = OutboundCall(CALL_ID, "Ha saltado la alarma.", "QueryOnly")
        await fallback.prepare_call(call)
        self.assertEqual(len(tts.calls), 2)
        self.assertEqual(tts.calls[0][0], "tts.piper")
        self.assertEqual(tts.calls[0][2:], ("es", "es_ES-davefx-medium"))
        await fallback.prepare_call(call)
        self.assertEqual(len(tts.calls), 2, "cached")
        fallback.forget(call)
        await fallback.prepare_call(call)
        self.assertEqual(len(tts.calls), 4)

    async def play(self, call, feed=None):
        fallback = Fallback(FakeTTS(), "tts.piper", "es")
        reader, writer = asyncio.StreamReader(), FakeWriter()
        task = asyncio.create_task(fallback.outbound(reader, writer, call, frame_seconds=0.001))
        if feed:
            await asyncio.sleep(0.01)
            reader.feed_data(feed)
        return await asyncio.wait_for(task, 5), writer

    async def test_key_confirms_the_notice(self):
        call = OutboundCall(CALL_ID, "Alarma.", "QueryOnly", confirm="voice")
        confirmed, writer = await self.play(call, pack_frame(KIND_DTMF, b"1"))
        self.assertTrue(confirmed, "the key works in fallback whatever the confirm mode")
        self.assertTrue(any(c[0] == KIND_AUDIO for c in writer.chunks))

    async def test_no_key_is_not_confirmed(self):
        confirmed, writer = await self.play(OutboundCall(CALL_ID, "Alarma.", "QueryOnly"))
        self.assertFalse(confirmed)
        speech = [c for c in writer.chunks if c[0] == KIND_AUDIO and any(c[3:])]
        self.assertGreaterEqual(len(speech), 2 * 3 + 2 * 3, "notice and key prompt, twice")

    async def test_other_keys_and_hang_up(self):
        confirmed, _ = await self.play(
            OutboundCall(CALL_ID, "Alarma.", "QueryOnly"),
            pack_frame(KIND_DTMF, b"5") + pack_frame(KIND_HANGUP),
        )
        self.assertFalse(confirmed)

    async def test_inbound_message(self):
        fallback = Fallback(FakeTTS(), "tts.piper", "es")
        writer = FakeWriter()
        await asyncio.wait_for(fallback.inbound(asyncio.StreamReader(), writer, frame_seconds=0.001), 5)
        self.assertTrue(any(c[0] == KIND_AUDIO and any(c[3:]) for c in writer.chunks))

    async def test_tts_errors_do_not_raise(self):
        class Broken:
            async def tts_audio(self, *args, **kwargs):
                raise ConnectionError("down")

        fallback = Fallback(Broken(), "tts.piper", "es")
        call = OutboundCall(CALL_ID, "Alarma.", "QueryOnly")
        await fallback.prepare_call(call)
        self.assertFalse(await fallback.outbound(asyncio.StreamReader(), FakeWriter(), call, frame_seconds=0.001))
        self.assertFalse(Fallback(FakeTTS(), "", "es").enabled)


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self.body = status, body


class FakeError(Exception):
    def __init__(self, status, body):
        super().__init__("rejected")
        self.response = FakeResponse(status, body)


class FakeConnection:
    def __init__(self, events=None, error=None):
        self.events, self.error = events or [], error

    def __call__(self, url, **kwargs):
        self.kwargs = kwargs
        return self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for event in self.events:
            yield json.dumps(event)


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_results(self):
        ok = await realtime_probe(FakeConnection([{"type": "session.created"}]), "wss://x", "k")
        self.assertEqual(ok, (True, ""))
        quota = await realtime_probe(
            FakeConnection([{"type": "error", "error": {"code": "insufficient_quota"}}]), "wss://x", "k")
        self.assertEqual(quota, (False, "insufficient_quota"))
        body = json.dumps({"error": {"code": "invalid_api_key"}}).encode()
        rejected = await realtime_probe(FakeConnection(error=FakeError(401, body)), "wss://x", "k")
        self.assertEqual(rejected, (False, "HTTP 401 invalid_api_key"))
        self.assertEqual(describe_error(ConnectionError("no route")), "ConnectionError: no route")

    async def test_states_are_published(self):
        published = []

        class States:
            async def set_state(self, entity_id, state, attributes):
                published.append((entity_id, state, attributes))

        async def probe():
            return False, "insufficient_quota"

        health = OpenAIHealth(States(), probe, 15)
        self.assertFalse(await health.check())
        entity, state, attrs = published[-1]
        self.assertEqual((entity, state), ("binary_sensor.asterisk_realtime_bridge_openai", "off"))
        self.assertEqual(attrs["reason"], "insufficient_quota")
        self.assertEqual(attrs["device_class"], "connectivity")
        await health.report(True)
        self.assertEqual(published[-1][1], "on")
        self.assertIsNone(published[-1][2]["reason"])

    async def test_check_timeout(self):
        import app.health as health_module

        async def slow():
            await asyncio.sleep(1)
            return True, ""

        class States:
            async def set_state(self, *args):
                pass

        original = health_module.CHECK_TIMEOUT_S
        health_module.CHECK_TIMEOUT_S = 0.01
        try:
            health = OpenAIHealth(States(), slow, 0)
            self.assertFalse(await health.check())
            self.assertEqual(health.reason, "timeout")
        finally:
            health_module.CHECK_TIMEOUT_S = original


if __name__ == "__main__":
    unittest.main()
