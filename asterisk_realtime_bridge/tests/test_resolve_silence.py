"""0.9.0: notes that resolve the notice, silence, ringing calls and answering machines."""

import asyncio
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.audiosocket import KIND_AUDIO, KIND_DTMF, KIND_UUID, pack_frame  # noqa: E402
from app.config import Config  # noqa: E402
from app.dialplan import voicemail_report  # noqa: E402
from app.fallback import Fallback  # noqa: E402
from app.local import LocalConversation  # noqa: E402
from app.outbound import OutboundCall, parse_command  # noqa: E402
from app.session import CallSession  # noqa: E402

from tests.test_fallback import FakeTTS  # noqa: E402
from tests.test_local import FakeHome as LocalHome, FakeScope, UTTERANCE  # noqa: E402
from tests import test_main  # noqa: E402  (also stubs the network libraries)
from tests.test_outbound import FakeStatus  # noqa: E402
from tests.test_session import FakeHome, FakeWriter, FakeWS, call  # noqa: E402

EventWriter = test_main.EventWriter

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, cfg=None, voice=None, **options):
        self.cfg = cfg or Config(openai_api_key="k", pin="1111", max_call_seconds=20, silence_seconds=0)
        self.ws, self.writer = FakeWS(), FakeWriter()
        self.reader = asyncio.StreamReader()
        options.setdefault("confirm", "voice")
        self.session = CallSession(self.cfg, "out", self.reader, self.writer, self.ws, FakeHome(),
                                   outbound=OutboundCall(CALL_ID, "Alarm.", "QueryOnly", **options),
                                   status=FakeStatus(), voice=voice)
        self.task = asyncio.create_task(self.session.run())
        await asyncio.sleep(0.01)
        self.ws.push({"type": "session.updated"})
        await asyncio.sleep(0.02)

    async def asyncTearDown(self):
        if getattr(self, "task", None):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def notes(self):
        return [e["item"]["content"][0]["text"] for e in self.ws.of_type("conversation.item.create")
                if e["item"]["type"] == "message"]

    async def test_resolving_note_interrupts_and_drops_the_cancelled_audio(self):
        await self.start()
        self.ws.push({"type": "response.created", "response": {"id": "r1"}})
        self.ws.push({"type": "response.output_audio.delta", "response_id": "r1", "item_id": "i1",
                      "delta": "AAAA" * 200})
        await asyncio.sleep(0.03)
        self.assertTrue(await self.session.note("Alarm disarmed by Marta.", resolve=True))
        self.assertEqual(len(self.ws.of_type("response.cancel")), 1)
        self.assertEqual(len(self.session._out), 0, "what was being said is cut")
        self.assertIn("Alarm disarmed by Marta.", self.notes()[-1])
        self.assertIn("do not ask for one again", self.notes()[-1])
        # Late audio of the cancelled response is not played.
        self.ws.push({"type": "response.output_audio.delta", "response_id": "r1", "item_id": "i1",
                      "delta": "AAAA" * 200})
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.session._out), 0)
        # When the cancelled response ends, the note is said.
        creates = len(self.ws.of_type("response.create"))
        self.ws.push({"type": "response.done", "response": {"status": "cancelled", "output": []}})
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.ws.of_type("response.create")), creates + 1)
        # No more confirmation: neither by voice nor by key.
        self.ws.push(call("confirm_notice", {}, "c1"))
        await asyncio.sleep(0.05)
        self.assertIn("No confirmation is needed", self.ws.tool_outputs()[-1]["message"])
        self.assertFalse(self.session.confirmed)
        self.assertTrue(self.session.resolved)

    async def test_plain_note_does_not_resolve(self):
        await self.start()
        await self.session.note("Info.")
        self.assertFalse(self.session.resolved)
        self.assertEqual(self.ws.of_type("response.cancel"), [])

    async def test_silence_asks_again_then_hangs_up(self):
        cfg = Config(openai_api_key="k", pin="1111", max_call_seconds=20, silence_seconds=1, silence_reprompts=1)
        await self.start(cfg)
        self.ws.push({"type": "response.done", "response": {"output": []}})  # greeting said
        await asyncio.sleep(1.4)
        self.assertIn("has not said anything", self.notes()[-1])
        self.ws.push({"type": "response.done", "response": {"output": []}})
        await asyncio.sleep(1.4)
        self.assertIn("Nobody is answering", self.notes()[-1])
        self.assertTrue(self.session.silence_ended)
        self.ws.push({"type": "response.done", "response": {"output": []}})  # the goodbye
        await asyncio.wait_for(self.task, 3)

    async def test_a_key_counts_as_an_answer(self):
        cfg = Config(openai_api_key="k", pin="1111", max_call_seconds=20, silence_seconds=1, silence_reprompts=1)
        await self.start(cfg)
        self.ws.push({"type": "response.done", "response": {"output": []}})
        for _ in range(4):
            await asyncio.sleep(0.4)
            self.reader.feed_data(pack_frame(KIND_DTMF, b"5"))
        self.assertFalse(any("has not said anything" in n for n in self.notes()))


class LocalTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, heard, cfg):
        self.home = LocalHome(heard)
        self.reader = asyncio.StreamReader()
        self.local = LocalConversation(
            cfg, "loc", self.reader, FakeWriter(), self.home, Fallback(FakeTTS(), "tts.piper", "es"),
            "stt.faster_whisper", FakeScope(), OutboundCall(CALL_ID, "Alarma.", "QueryOnly", confirm="voice"),
            frame_seconds=0.001,
        )
        self.task = asyncio.create_task(self.local.run())
        await asyncio.sleep(0.05)

    async def test_silence_then_goodbye(self):
        await self.start([], Config(openai_api_key="k", language="es", max_call_seconds=10,
                                    silence_seconds=1, silence_reprompts=1))
        await asyncio.wait_for(self.task, 5)
        self.assertTrue(self.local.silence_ended)

    async def test_resolving_note_stops_confirmation(self):
        await self.start(["recibido"], Config(openai_api_key="k", language="es", max_call_seconds=10,
                                              silence_seconds=1, silence_reprompts=3))
        self.assertTrue(await self.local.note("La alarma se ha desarmado.", resolve=True))
        self.reader.feed_data(UTTERANCE)
        await asyncio.wait_for(self.task, 5)
        self.assertTrue(self.local.resolved)
        self.assertFalse(self.local.confirmed, "a yes after the resolve is not a confirmation")
        self.assertTrue(self.local.silence_ended, "after a resolve, silence ends the call at once")


class BridgeV09Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        test_main.main.connect = test_main._no_openai
        self.bridge = test_main.main.Bridge(Config(openai_api_key="k", pin="1234"), http=None)
        self.events = []

        async def fire_event(event_type, data):
            self.events.append((event_type, data))

        self.bridge.home.fire_event = fire_event

    async def asyncTearDown(self):
        await self.bridge.close()

    async def test_withdrawn_ringing_call_is_hung_up_in_asterisk(self):
        services = []

        async def call_service(domain, service, data):
            services.append((domain, service, data))

        self.bridge.home.call_service = call_service
        self.bridge.registry.add(OutboundCall(CALL_ID, "Alarm", "QueryOnly", number="600111222"))
        self.bridge.registry.add(OutboundCall(str(uuid.uuid4()), "Alarm", "QueryOnly", number="60;rm"))
        await self.bridge.on_command({"action": "hangup"})
        self.assertEqual(len(services), 1, "a number that is not a phone number is never sent")
        domain, service, data = services[0]
        self.assertEqual((domain, service, data["addon"]), ("hassio", "addon_stdin", "3e533915_asterisk"))
        self.assertEqual(data["input"],
                         f"channel originate Local/600111222_{CALL_ID}@bridge-kill application Wait 1")
        self.assertEqual({d["result"] for _, d in self.events}, {"cancelled"})

    async def test_stop_ringing_failure_keeps_the_call_cancelled(self):
        async def call_service(domain, service, data):
            raise ConnectionError("down")

        self.bridge.home.call_service = call_service
        self.bridge.registry.add(OutboundCall(CALL_ID, "Alarm", "QueryOnly", number="600111222"))
        await self.bridge.on_command({"action": "hangup", "call_id": CALL_ID})
        self.assertEqual(self.events[-1][1]["result"], "cancelled")

    async def test_answering_machine(self):
        played = []

        async def audio(text):
            played.append(text)
            return bytes(640)

        self.bridge.fallback.engine = "tts.piper"
        self.bridge.fallback.audio = audio
        cmd = parse_command({"action": "outbound", "call_id": CALL_ID, "message": "Alarm",
                             "profile": "QueryOnly", "voicemail_message": "Call home, please."})
        self.bridge.registry.add(cmd)
        reader = asyncio.StreamReader()
        reader.feed_data(pack_frame(KIND_UUID, uuid.UUID("a3d00002" + CALL_ID[8:]).bytes))
        writer = EventWriter()
        await self.bridge.handle(reader, writer)
        self.assertEqual(played, ["Call home, please."])
        self.assertTrue(any(c[0] == KIND_AUDIO for c in writer.chunks))
        data = self.events[-1][1]
        self.assertEqual((data["result"], data["amdstatus"], data["amdcause"], data["voicemail_message_left"]),
                         ("voicemail", "MACHINE", "LONGGREETING", True))
        self.assertEqual(len(self.bridge.registry), 0)

    async def test_resolved_result(self):
        class S:
            connected, confirmed, confirmed_by, pin_person = True, False, None, None
            resolved, silence_ended = True, False

        data = self.bridge._ended(CALL_ID, OutboundCall(CALL_ID, "x", "QueryOnly"), S(), 10)
        self.assertEqual((data["result"], data["resolved"]), ("resolved", True))
        S.resolved, S.silence_ended = False, True
        data = self.bridge._ended(CALL_ID, OutboundCall(CALL_ID, "x", "QueryOnly"), S(), 10)
        self.assertEqual((data["result"], data["ended_by"]), ("answered", "silence"))
        data = self.bridge._ended(CALL_ID, OutboundCall(CALL_ID, "x", "QueryOnly"), None, 0,
                                  {"confirmed": False, "local": True, "resolved": True})
        self.assertEqual(data["result"], "resolved")


class DialplanTests(unittest.TestCase):
    def test_voicemail_report(self):
        self.assertEqual(voicemail_report("a3d00001" + CALL_ID[8:]), ("INITIALSILENCE", CALL_ID[8:]))
        self.assertEqual(voicemail_report("a3d00000" + CALL_ID[8:])[0], "UNKNOWN")
        self.assertIsNone(voicemail_report(CALL_ID))

    def test_new_options(self):
        cfg = Config(openai_api_key="k")
        self.assertEqual((cfg.silence_seconds, cfg.silence_reprompts, cfg.asterisk_app),
                         (8, 2, "3e533915_asterisk"))


if __name__ == "__main__":
    unittest.main()


class AssistantVoicemailTests(RealtimeTests):
    def tools(self):
        return [t["name"] for t in self.ws.sent[0]["session"]["tools"]]

    async def test_tool_only_when_asked(self):
        await self.start()
        self.assertNotIn("report_voicemail", self.tools())
        self.assertNotIn("report_voicemail", self.ws.sent[0]["session"]["instructions"])

    async def test_voicemail_message_is_left_by_the_bridge_after_the_greeting(self):
        class Voice:
            enabled = True

            async def audio(self, text):
                self.text = text
                return bytes(3200)  # 200 ms

        voice = Voice()
        await self.start(voice=voice, detect_voicemail=True, voicemail_message="Call home.")
        self.assertIn("report_voicemail", self.tools())
        self.assertIn("Live Voicemail", self.ws.sent[0]["session"]["instructions"])
        loud = (b"\xff\x3f" * 160)  # the greeting is still playing
        self.reader.feed_data(pack_frame(KIND_AUDIO, loud))
        creates = len(self.ws.of_type("response.create"))
        self.ws.push(call("report_voicemail", {}, "v1"))
        await asyncio.sleep(0.5)
        self.assertFalse(self.session.voicemail_message_left, "waits for the greeting to end")
        await asyncio.wait_for(self.task, 5)
        self.assertEqual(voice.text, "Call home.")
        self.assertTrue(self.session.voicemail and self.session.voicemail_message_left)
        self.assertEqual(len(self.ws.of_type("response.create")), creates, "the assistant says nothing more")

    async def test_voicemail_result(self):
        await self.start(detect_voicemail=True)
        self.ws.push(call("report_voicemail", {}, "v1"))
        await asyncio.wait_for(self.task, 5)
        bridge = test_main.main.Bridge(Config(openai_api_key="k", pin="1234"), http=None)
        data = bridge._ended(CALL_ID, self.session.outbound, self.session, 5)
        await bridge.close()
        self.assertEqual((data["result"], data["detected_by"], data["voicemail_message_left"]),
                         ("voicemail", "assistant", False))

    def test_command_flag(self):
        cmd = parse_command({"action": "outbound", "call_id": CALL_ID, "message": "x",
                             "profile": "QueryOnly", "detect_voicemail": True})
        self.assertTrue(cmd.detect_voicemail)
        cmd = parse_command({"action": "outbound", "call_id": CALL_ID, "message": "x",
                             "profile": "QueryOnly", "detect_voicemail": "false"})
        self.assertFalse(cmd.detect_voicemail)
