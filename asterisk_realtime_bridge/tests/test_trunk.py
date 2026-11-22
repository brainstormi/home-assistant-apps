"""Trunk test: call our own line and see the call come back."""

import asyncio
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import test_main  # noqa: E402  (also stubs the network libraries)

from app import trunk  # noqa: E402
from app.audiosocket import KIND_UUID, pack_frame  # noqa: E402
from app.config import Config  # noqa: E402
from app.trunk import result_for, trunk_report  # noqa: E402


class ParsingTests(unittest.TestCase):
    def test_reports(self):
        tail = str(uuid.uuid4())[8:]
        self.assertEqual(trunk_report("7e570002" + tail), ("dial", "BUSY", tail))
        self.assertEqual(trunk_report("7e57a100" + tail), ("arrived", "", tail))
        self.assertIsNone(trunk_report("fa110002" + tail))
        self.assertIsNone(trunk_report("7e57ffff" + tail))

    def test_results(self):
        self.assertEqual(result_for(True, "BUSY"), "ok")
        self.assertEqual(result_for(False, "BUSY"), "out_only")
        self.assertEqual(result_for(False, "NOANSWER"), "out_only")
        self.assertEqual(result_for(False, "CHANUNAVAIL"), "failed")
        self.assertEqual(result_for(False, ""), "failed")


class TrunkBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = test_main.main.Bridge(Config(openai_api_key="k", pin="1234"), http=None)
        self.events, self.commands = [], []

        async def fire_event(event_type, data):
            self.events.append((event_type, data))

        async def call_service(domain, service, data):
            self.commands.append(data)

        self.bridge.home.fire_event = fire_event
        self.bridge.home.call_service = call_service

    async def asyncTearDown(self):
        await self.bridge.close()

    def trunk_events(self):
        return [d for t, d in self.events if t == "asterisk_realtime_bridge_trunk"]

    async def report(self, prefix):
        test = self.bridge.trunk.current
        reader = asyncio.StreamReader()
        reader.feed_data(pack_frame(KIND_UUID, uuid.UUID(prefix + test.uuid[8:]).bytes))
        await self.bridge.handle(reader, test_main.EventWriter())

    async def start(self, **extra):
        await self.bridge.on_command({"action": "trunk_test", "test_id": "t1", "number": "600111222",
                                      "timeout": 5, **extra})
        await asyncio.sleep(0.02)

    async def test_ok_when_the_call_comes_back(self):
        await self.start()
        test = self.bridge.trunk.current
        self.assertEqual(self.commands[0]["addon"], "3e533915_asterisk")
        self.assertEqual(self.commands[0]["input"],
                         f"channel originate Local/600111222_{test.uuid}@bridge-trunk-test application Wait 1")
        await self.report("7e57a100")
        await self.report("7e570002")  # BUSY: we hung the incoming leg up
        await asyncio.sleep(0.05)
        (data,) = self.trunk_events()
        self.assertEqual((data["test_id"], data["result"], data["dialstatus"], data["arrived"]),
                         ("t1", "ok", "BUSY", True))
        self.assertIsNone(self.bridge.trunk.current)

    async def test_out_only_after_the_grace_period(self):
        self.addCleanup(setattr, trunk, "ARRIVAL_GRACE_S", trunk.ARRIVAL_GRACE_S)
        trunk.ARRIVAL_GRACE_S = 0.1
        await self.start()
        await self.report("7e570002")
        await asyncio.sleep(0.3)
        self.assertEqual(self.trunk_events()[0]["result"], "out_only")

    async def test_failed_without_reports(self):
        await self.bridge.on_command({"action": "trunk_test", "test_id": "t2", "number": "600111222",
                                      "timeout": 5})
        self.bridge.trunk.current.timeout = 0.1
        await asyncio.sleep(0.01)
        await self.report("7e570004")  # CHANUNAVAIL
        await asyncio.sleep(2.3)
        data = self.trunk_events()[0]
        self.assertEqual((data["result"], data["dialstatus"]), ("failed", "CHANUNAVAIL"))

    async def test_one_test_at_a_time_and_validation(self):
        await self.start()
        await self.bridge.on_command({"action": "trunk_test", "test_id": "t9", "number": "600111222"})
        await self.bridge.on_command({"action": "trunk_test", "test_id": "bad id!", "number": "600"})
        await self.bridge.on_command({"action": "trunk_test", "test_id": "t8", "number": "60;rm"})
        errors = [d for d in self.trunk_events() if d.get("error")]
        self.assertEqual(len(errors), 3)
        self.assertIn("another trunk test", errors[0]["error"])
        self.assertEqual(len(self.commands), 1, "nothing else reached Asterisk")

    async def test_reports_for_another_test_are_ignored(self):
        await self.start()
        reader = asyncio.StreamReader()
        reader.feed_data(pack_frame(KIND_UUID, uuid.UUID("7e57a100" + str(uuid.uuid4())[8:]).bytes))
        await self.bridge.handle(reader, test_main.EventWriter())
        self.assertFalse(self.bridge.trunk.current.arrived)


if __name__ == "__main__":
    unittest.main()
