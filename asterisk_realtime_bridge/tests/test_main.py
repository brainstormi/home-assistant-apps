"""Bridge wiring: commands from Home Assistant, call lookup and result events."""

import asyncio
import os
import sys
import types
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stand-ins for the network libraries (not installed in the test environment).
if "aiohttp" not in sys.modules:
    fake_aiohttp = types.ModuleType("aiohttp")
    fake_aiohttp.ClientConnectorError = type("ClientConnectorError", (Exception,), {})
    fake_aiohttp.ClientResponseError = type("ClientResponseError", (Exception,), {})
    fake_aiohttp.ClientTimeout = lambda total=None: ("timeout", total)
    fake_aiohttp.ClientSession = object
    sys.modules["aiohttp"] = fake_aiohttp
for name in ("websockets", "websockets.asyncio", "websockets.asyncio.client"):
    sys.modules.setdefault(name, types.ModuleType(name))


def _no_openai(*args, **kwargs):
    raise ConnectionError("OpenAI unreachable (test)")


sys.modules["websockets.asyncio.client"].connect = _no_openai

from app import main  # noqa: E402
from app.audiosocket import KIND_UUID, pack_frame  # noqa: E402
from app.config import Config  # noqa: E402
from app.outbound import OutboundCall  # noqa: E402

from tests.test_session import FakeWriter  # noqa: E402

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"


class EventWriter(FakeWriter):
    def get_extra_info(self, name):
        return ("127.0.0.1", 40000)

    def close(self):
        pass

    async def wait_closed(self):
        pass


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.connect = _no_openai
        self.bridge = main.Bridge(Config(openai_api_key="k", pin="1234"), http=None)
        self.events = []

        async def fire_event(event_type, data):
            self.events.append((event_type, data))

        self.bridge.home.fire_event = fire_event

    async def asyncTearDown(self):
        await self.bridge.close()

    async def connect(self, call_id):
        reader = asyncio.StreamReader()
        reader.feed_data(pack_frame(KIND_UUID, uuid.UUID(call_id).bytes))
        await self.bridge.handle(reader, EventWriter())

    async def test_command_registers_an_outbound_call(self):
        await self.bridge.on_command({"action": "outbound", "call_id": CALL_ID, "message": "Alarm",
                                      "profile": "QueryOnly", "person": "person.sonia",
                                      "number": "600111222"})
        self.assertEqual(len(self.bridge.registry), 1)
        (_, data), = self.events
        self.assertEqual((data["stage"], data["call_id"], data["person"], data["number"]),
                         ("started", CALL_ID, "person.sonia", "600111222"))

    async def test_invalid_command_is_reported_as_failed(self):
        await self.bridge.on_command({"action": "outbound", "call_id": CALL_ID, "message": "",
                                      "profile": "QueryOnly"})
        self.assertEqual(len(self.bridge.registry), 0)
        event_type, data = self.events[0]
        self.assertEqual(event_type, "asterisk_realtime_bridge_call")
        self.assertEqual((data["call_id"], data["stage"], data["result"]), (CALL_ID, "ended", "failed"))

    async def test_answered_outbound_call_reports_answered_then_failed_when_openai_is_down(self):
        self.bridge.registry.add(OutboundCall(CALL_ID, "Alarm", "Management", person="Sonia"))
        await self.connect(CALL_ID)
        stages = [(d["stage"], d.get("result")) for _, d in self.events]
        self.assertEqual(stages, [("answered", None), ("ended", "failed")])
        self.assertEqual(self.events[-1][1]["effective_profile"], "Management")
        ended = self.events[-1][1]
        self.assertEqual(ended["direction"], "outbound")
        self.assertEqual(ended["person"], "Sonia")
        self.assertFalse(ended["pin_verified"])

    async def test_unknown_uuid_is_an_incoming_call(self):
        await self.connect(str(uuid.uuid4()))
        self.assertEqual([d["stage"] for _, d in self.events], ["started", "ended"])
        for _, data in self.events:
            self.assertEqual((data["direction"], data["profile"], data["number"]), ("inbound", "InboundCall", ""))
        self.assertEqual(self.events[0][1]["access"], "pin")

    async def test_incoming_call_with_the_caller_number(self):
        from app.pins import PinEntry
        self.bridge.cfg = Config(openai_api_key="k", pin_mode="individual",
                                 pins=(PinEntry(person="person.sonia", pin="482916", numbers=("600111222",)),))
        await self.connect("ca11e009-1234-0000-0000-000600111222")
        started = self.events[0][1]
        self.assertEqual((started["number"], started["person"], started["access"]),
                         ("600111222", "person.sonia", "pin"))
        self.events.clear()
        await self.connect("ca11e009-4321-0000-0000-000699999999")
        self.assertEqual(self.events[0][1]["access"], "query_only")

    async def test_dial_status_report(self):
        self.bridge.registry.add(OutboundCall(CALL_ID, "Alarm", "QueryOnly", number="600111222"))
        await self.connect("fa110002" + CALL_ID[8:])  # BUSY
        self.assertEqual(len(self.bridge.registry), 0)
        data = self.events[-1][1]
        self.assertEqual((data["call_id"], data["result"], data["dialstatus"], data["number"]),
                         (CALL_ID, "failed", "BUSY", "600111222"))
        self.events.clear()
        other = str(uuid.uuid4())
        self.bridge.registry.add(OutboundCall(other, "Alarm", "QueryOnly"))
        await self.connect("fa110001" + other[8:])  # NOANSWER
        self.assertEqual((self.events[-1][1]["result"], self.events[-1][1]["dialstatus"]), ("no_answer", "NOANSWER"))
        # A late answer of a call that is no longer pending is hung up, never an incoming call.
        self.events.clear()
        await self.connect(other)
        self.assertEqual(self.events, [])

    async def test_hangup_withdraws_a_ringing_call(self):
        self.bridge.registry.add(OutboundCall(CALL_ID, "Alarm", "QueryOnly"))
        await self.bridge.on_command({"action": "hangup"})
        self.assertEqual(len(self.bridge.registry), 0)
        self.assertEqual((self.events[-1][1]["result"], self.events[-1][1]["ended_by"]), ("cancelled", "hangup"))
        self.events.clear()
        await self.connect(CALL_ID)  # answered too late: hung up
        self.assertEqual(self.events, [])

    async def test_hangup_and_note_on_a_call_in_progress(self):
        notes = []
        started = asyncio.Event()

        class Handler:
            async def note(self, text, resolve=False):
                notes.append(text)
                return True

        async def run_local(call_id, reader, writer, outbound, reason, auth=None, live=None):
            live.handler = Handler()
            started.set()
            await asyncio.Event().wait()  # talking until hung up

        self.bridge.fallback.engine = "tts.piper"
        self.bridge._run_local = run_local
        await self.bridge.on_command({"action": "outbound", "call_id": CALL_ID, "message": "x",
                                      "profile": "QueryOnly", "engine": "local"})
        task = asyncio.create_task(self.connect(CALL_ID))
        await asyncio.wait_for(started.wait(), 1)
        await self.bridge.on_command({"action": "note", "call_id": CALL_ID.upper(), "text": "Alarma activada"})
        self.assertEqual(notes, ["Alarma activada"])
        self.assertEqual(self.events[-1][1], {"call_id": CALL_ID, "stage": "note", "delivered": True, "resolve": False})
        await self.bridge.on_command({"action": "hangup", "call_id": CALL_ID})
        await asyncio.gather(task, return_exceptions=True)
        ended = self.events[-1][1]
        self.assertEqual((ended["stage"], ended["ended_by"]), ("ended", "hangup"))
        self.assertEqual(self.bridge.live, {})
        await self.bridge.on_command({"action": "note", "call_id": CALL_ID, "text": "x"})
        self.assertFalse(self.events[-1][1]["delivered"])

    async def test_missed_outbound_call_reports_no_answer(self):
        call = OutboundCall(CALL_ID, "Alarm", "Management")
        call.timeout = 0.05
        self.bridge.registry.add(call)
        await asyncio.sleep(0.12)
        data = self.events[0][1]
        self.assertEqual((data["call_id"], data["result"]), (CALL_ID, "no_answer"))

    async def test_outbound_fallback_when_openai_is_down(self):
        played = []

        async def outbound(reader, writer, call):
            played.append(call.call_id)
            return True

        async def report(ok, reason="", source="call"):
            self.health.append((ok, reason))

        self.health = []
        self.bridge.fallback.engine = "tts.piper"
        self.bridge.fallback.outbound = outbound
        self.bridge.health.report = report
        self.bridge.registry.add(OutboundCall(CALL_ID, "Alarm", "QueryOnly"))
        await self.connect(CALL_ID)
        await asyncio.sleep(0.01)
        ended = self.events[-1][1]
        self.assertEqual(played, [CALL_ID])
        self.assertEqual((ended["result"], ended["confirmed_by"], ended["fallback"]),
                         ("confirmed", "dtmf", True))
        self.assertEqual(self.health[0][0], False)

    async def test_inbound_fallback_message(self):
        played = []

        async def inbound(reader, writer):
            played.append("inbound")

        self.bridge.fallback.engine = "tts.piper"
        self.bridge.fallback.inbound = inbound
        await self.connect(str(uuid.uuid4()))
        self.assertEqual(played, ["inbound"])
        ended = self.events[-1][1]
        self.assertEqual((ended["result"], ended["fallback"]), ("failed", True))

    async def test_no_fallback_configured(self):
        await self.connect(str(uuid.uuid4()))
        self.assertNotIn("fallback", self.events[-1][1])

    async def test_engine_local_skips_openai(self):
        import app.main as main_module
        calls = []

        def must_not_connect(*args, **kwargs):
            calls.append("openai")
            raise AssertionError("OpenAI must not be used")

        async def run_local(call_id, reader, writer, outbound, reason, *args):
            calls.append(reason)
            return {"confirmed": True, "confirmed_by": "voice", "pin_person": None, "local": True}

        main_module.connect = must_not_connect
        self.bridge.fallback.engine = "tts.piper"
        self.bridge._run_local = run_local
        await self.bridge.on_command({"action": "outbound", "call_id": CALL_ID, "message": "Lavadora",
                                      "profile": "QueryOnly", "engine": "local"})
        await self.connect(CALL_ID)
        self.assertEqual(calls, ["requested"])
        ended = self.events[-1][1]
        self.assertEqual((ended["result"], ended["confirmed_by"], ended["engine"]), ("confirmed", "voice", "local"))
        self.assertNotIn("fallback", ended)

    async def test_engine_local_needs_a_voice(self):
        await self.bridge.on_command({"action": "outbound", "call_id": CALL_ID, "message": "x",
                                      "profile": "QueryOnly", "engine": "local"})
        self.assertEqual(len(self.bridge.registry), 0)
        self.assertEqual(self.events[0][1]["result"], "failed")

    def test_result_values(self):
        class S:
            connected, confirmed, confirmed_by, pin_person = True, False, None, None

        session = S()
        self.assertEqual(self.bridge._ended(CALL_ID, None, session, 3.2)["result"], "answered")
        session.confirmed, session.confirmed_by, session.pin_person = True, "dtmf", "Luis"
        data = self.bridge._ended(CALL_ID, None, session, 3.2)
        self.assertEqual((data["result"], data["confirmed_by"], data["pin_person"], data["duration"]),
                         ("confirmed", "dtmf", "Luis", 3))
        self.assertEqual(data["profile"], "InboundCall")
        self.assertEqual(self.bridge._ended(CALL_ID, None, None, 0)["result"], "failed")


if __name__ == "__main__":
    unittest.main()
