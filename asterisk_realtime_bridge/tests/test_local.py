import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.audiosocket import KIND_AUDIO, KIND_DTMF, KIND_HANGUP, pack_frame  # noqa: E402
from app.config import Config  # noqa: E402
from app.fallback import Fallback  # noqa: E402
from app.local import LocalConversation, has_phrase, typing_sound  # noqa: E402
from app.audiosocket import FRAME_BYTES  # noqa: E402
from app.stt import rms  # noqa: E402
from app.outbound import OutboundCall  # noqa: E402
from app.pins import PinEntry  # noqa: E402
from app.session import call_auth  # noqa: E402

from tests.test_fallback import FakeTTS  # noqa: E402
from tests.test_session import FakeWriter  # noqa: E402
from tests.test_stt import LOUD, QUIET  # noqa: E402

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"
UTTERANCE = b"".join(pack_frame(KIND_AUDIO, f) for f in [QUIET] * 5 + [LOUD] * 20 + [QUIET] * 40)

INTENTS = {
    "esta abierta la puerta del garaje": ("HassGetState", ["binary_sensor.garaje"]),
    "enciende la luz del sofa": ("HassTurnOn", ["light.sofa"]),
    "que temperatura hace en la piscina": ("HassClimateGetTemperature", ["sensor.piscina"]),
}


class FakeHome:
    def __init__(self, heard):
        self.heard = list(heard)
        self.asked, self.conversed = [], []

    async def stt(self, engine_id, wav16k, language):
        return self.heard.pop(0) if self.heard else ""

    async def ask(self, text):
        self.asked.append(text)
        return {"type": "action_done", "response": "Hecho."}

    async def converse(self, agent_id, text):
        self.conversed.append((agent_id, text))
        return {"type": "query_answer", "response": "Está cerrada."}

    async def ws(self, command, **kwargs):
        assert command == "conversation/agent/homeassistant/debug"
        key = kwargs["sentences"][0].lower().replace("¿", "").replace("?", "").replace("á", "a")
        intent, targets = INTENTS.get(key, (None, []))
        return {"results": [{"match": intent is not None, "intent": {"name": intent},
                             "targets": {t: {"matched": True} for t in targets}}]}


class FakeScope:
    async def allows(self, entity_ids):
        return "sensor.piscina" not in entity_ids


class LocalTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, heard, outbound=None, cfg=None, number="600111222"):
        self.cfg = cfg or Config(openai_api_key="k", language="es", pin_mode="individual", max_call_seconds=10,
                                 pins=(PinEntry(person="person.sonia", pin="482916", numbers=("600111222",)),))
        self.home = FakeHome(heard)
        self.reader = asyncio.StreamReader()
        self.local = LocalConversation(
            self.cfg, "loc", self.reader, FakeWriter(), self.home, Fallback(FakeTTS(), "tts.piper", "es"),
            "stt.faster_whisper", FakeScope(), outbound, frame_seconds=0.001,
            auth=call_auth(self.cfg, outbound, number),
        )
        self.task = asyncio.create_task(self.local.run())
        await asyncio.sleep(0.05)

    async def feed(self, data, wait=0.15):
        self.reader.feed_data(data)
        await asyncio.sleep(wait)

    async def keys(self, digits):
        await self.feed(b"".join(pack_frame(KIND_DTMF, d.encode()) for d in digits))

    async def finish(self):
        await self.feed(pack_frame(KIND_HANGUP), 0.05)
        await asyncio.wait_for(self.task, 5)

    async def test_inbound_needs_the_keypad_pin(self):
        await self.start(["enciende la luz del sofá", "enciende la luz del sofá"])
        await self.feed(UTTERANCE)
        self.assertEqual(self.home.asked, [], "nothing before the PIN")
        await self.keys("482916")
        self.assertEqual(self.local.pin_person, "person.sonia")
        await self.feed(UTTERANCE)
        self.assertEqual(self.home.asked, ["enciende la luz del sofá"])
        await self.finish()

    async def test_inbound_from_an_unlinked_number_only_reads(self):
        await self.start(["¿está abierta la puerta del garaje?", "enciende la luz del sofá"], number="699999999")
        await self.feed(UTTERANCE)
        await self.feed(UTTERANCE)
        self.assertEqual(self.home.conversed, [("conversation.home_assistant", "¿está abierta la puerta del garaje?")])
        self.assertEqual(self.home.asked, [])
        await self.keys("482916")
        self.assertFalse(self.local.authed, "no PIN for an unlinked number")
        await self.finish()

    async def test_note_is_spoken(self):
        await self.start([])
        self.assertTrue(await self.local.note("La alarma se ha activado."))
        await self.finish()

    async def test_wrong_pins_end_the_call(self):
        await self.start([])
        for _ in range(3):
            await self.keys("999999#")
        await asyncio.wait_for(self.task, 5)
        self.assertFalse(self.local.authed)

    async def test_query_only_reads_but_never_acts(self):
        call = OutboundCall(CALL_ID, "Alarma.", "QueryOnly", confirm="voice")
        await self.start(["¿está abierta la puerta del garaje?", "enciende la luz del sofá",
                          "¿qué temperatura hace en la piscina?", "recibido"], call)
        for _ in range(4):
            await self.feed(UTTERANCE)
        self.assertEqual(self.home.conversed, [("conversation.home_assistant", "¿está abierta la puerta del garaje?")])
        self.assertEqual(self.home.asked, [], "the acting agent is never used")
        self.assertEqual((self.local.confirmed, self.local.confirmed_by), (True, "voice"))
        await self.keys("482916")
        self.assertFalse(self.local.authed, "no PIN in QueryOnly")
        await self.finish()

    async def test_management_acts_after_the_pin_and_key_confirms(self):
        call = OutboundCall(CALL_ID, "Alarma.", "Management", person="Sonia", confirm="dtmf", confirm_key="#")
        await self.start(["enciende la luz del sofá", "recibido", "enciende la luz del sofá"], call)
        await self.feed(UTTERANCE)
        self.assertEqual(self.home.asked, [])
        await self.feed(UTTERANCE)
        self.assertFalse(self.local.confirmed, "voice does not count in dtmf mode")
        await self.keys("#")
        self.assertEqual(self.local.confirmed_by, "dtmf")
        await self.keys("482916")
        await self.feed(UTTERANCE)
        self.assertEqual(self.home.asked, ["enciende la luz del sofá"])
        await self.finish()

    async def test_goodbye_ends_the_call(self):
        await self.start(["vale, adiós"], OutboundCall(CALL_ID, "Aviso.", "QueryOnly"))
        await self.feed(UTTERANCE, 0.3)
        await asyncio.wait_for(self.task, 5)
        self.assertEqual(self.local.confirmed_by, "implicit")

    def test_typing_sound_is_soft_and_irregular(self):
        frames = [f for f, _ in zip(typing_sound(seed=1), range(150))]  # 3 s
        self.assertTrue(all(len(f) == FRAME_BYTES for f in frames))
        levels = [rms(f) for f in frames]
        self.assertGreater(sum(1 for v in levels if v > 0), 10, "there are clicks")
        self.assertGreater(sum(1 for v in levels if v == 0), 10, "and gaps between them")
        self.assertLess(max(levels), 1000, "at a low volume")

    async def test_typing_sound_plays_while_working(self):
        await self.start(["enciende la luz del sofá"])
        await self.keys("482916")
        slow = asyncio.Event()
        original = self.home.ask

        async def slow_ask(text):
            await slow.wait()
            return await original(text)

        self.home.ask = slow_ask
        await self.feed(UTTERANCE, 0.1)
        self.assertTrue(self.local._thinking)
        writer = self.local.writer
        before = len(writer.chunks)
        await asyncio.sleep(0.1)
        noisy = [c for c in writer.chunks[before:] if c[0] == KIND_AUDIO and any(c[3:])]
        self.assertTrue(noisy, "clicks are sent while Home Assistant works")
        slow.set()
        await asyncio.sleep(0.1)
        self.assertFalse(self.local._thinking)
        await self.finish()

    def test_pin_is_not_spelled(self):
        from app.local import TEXT
        for lang in TEXT.values():
            self.assertFalse(any("PIN" in text for text in lang.values()))

    def test_phrases(self):
        self.assertTrue(has_phrase("Sí, recibido", ("recibido",)))
        self.assertTrue(has_phrase("vale adiós", ("adiós",)))
        self.assertFalse(has_phrase("¿si qué?", ("recibido",)))


if __name__ == "__main__":
    unittest.main()
