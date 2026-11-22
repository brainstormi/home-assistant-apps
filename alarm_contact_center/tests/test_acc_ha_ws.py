"""Logic of ha_ws.py with a fake connection (aiohttp is stubbed if missing)."""

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # use the real aiohttp when installed (CI), a stub otherwise
    import aiohttp  # noqa: F401
except ImportError:  # pragma: no cover
    stub = types.ModuleType("aiohttp")

    class _WSMsgType:
        TEXT, CLOSED, ERROR = 1, 8, 258

    stub.WSMsgType = _WSMsgType
    stub.ClientError = type("ClientError", (Exception,), {})
    stub.ClientWebSocketResponse = object
    stub.ClientSession = object
    sys.modules["aiohttp"] = stub

import aiohttp  # noqa: E402

from app.engine import Engine, IncidentLog  # noqa: E402
from app.ha_ws import HAConnection, HAPort, wire  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402


class FakeConn:
    def __init__(self, states):
        self.states = states
        self.subs = {}
        self.services = []
        self.connected = asyncio.Event()
        self.connected.set()
        self.response = {}

    async def subscribe(self, payload, handler):
        key = payload.get("event_type") or payload["type"]
        self.subs[key] = (payload, handler)

    async def command(self, payload, timeout=30):
        assert payload["type"] == "get_states"
        return self.states

    async def call_service(self, domain, service, data=None, target=None, return_response=False, timeout=30):
        self.services.append((domain, service, data, target, return_response))
        return {"context": {}, "response": self.response} if return_response else None


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.json")
        self.store.save_settings({"call_delay": 0, "rounds": 1})
        self.store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003"}, PinStatus())

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_catch_up_on_triggered_alarm(self):
        os.environ["HOSTNAME"] = "abcd1234-alarm-contact-center"
        self.addCleanup(os.environ.pop, "HOSTNAME", None)
        conn = FakeConn([{"entity_id": "alarm_control_panel.alarmo", "state": "triggered",
                          "attributes": {"open_sensors": {"binary_sensor.x": "on"}}},
                         {"entity_id": "binary_sensor.x", "state": "on", "attributes": {"friendly_name": "Puerta"}}])
        conn.response = {"resultado": "confirmed"}
        engine = Engine(self.store, HAPort(conn, self.store), IncidentLog(Path(self.tmp.name) / "i.json"), time_scale=0)
        await wire(conn, engine, self.store)
        self.assertIn("subscribe_trigger", conn.subs)
        self.assertEqual(conn.subs["subscribe_trigger"][0]["trigger"]["entity_id"], "alarm_control_panel.alarmo")
        self.assertIn("asterisk_realtime_bridge_call", conn.subs)
        self.assertIn("mobile_app_notification_action", conn.subs)
        await engine.current.task
        self.assertEqual(engine.current.status, "acknowledged")
        self.assertIn("reanudado", engine.current.events[0]["detail"])
        script_calls = [s for s in conn.services if s[0] == "script" and s[1] == "llamada_saliente"]
        self.assertEqual(script_calls[0][2]["numero"], "600000003")
        self.assertTrue(script_calls[0][4])  # return_response
        self.assertIn(("script", "turn_off", None, {"entity_id": "script.llamada_saliente"}, False), conn.services)
        hangup = [s for s in conn.services if s[0] == "hassio"]
        self.assertEqual(hangup[0][2], {"addon": "abcd1234_asterisk_realtime_bridge", "input": {"action": "hangup", "call_id": ""}})

    async def test_trigger_event_shape(self):
        conn = FakeConn([{"entity_id": "alarm_control_panel.alarmo", "state": "armed_away", "attributes": {}}])
        conn.response = {"resultado": "confirmed"}
        engine = Engine(self.store, HAPort(conn, self.store), IncidentLog(Path(self.tmp.name) / "i.json"), time_scale=0)
        await wire(conn, engine, self.store)
        self.assertIsNone(engine.current)
        _, on_alarm = conn.subs["subscribe_trigger"]
        await on_alarm({"variables": {"trigger": {"to_state": {"state": "triggered", "attributes": {}}}}})
        await engine.current.task
        self.assertEqual(engine.current.kind, "alarm")


class FakeWS:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.messages:
            yield types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(m))

    async def send_json(self, data):
        self.sent.append(data)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_and_events_routed(self):
        conn = HAConnection(token="t")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        conn._pending[1] = fut
        got = []

        async def handler(event):
            got.append(event)

        conn._subscriptions[2] = handler
        ws = FakeWS([
            {"id": 1, "type": "result", "success": True, "result": {"ok": 1}},
            {"id": 2, "type": "event", "event": {"data": {"a": 1}}},
        ])
        await conn._read(ws)
        await asyncio.sleep(0)
        self.assertEqual(fut.result(), {"ok": 1})
        self.assertEqual(got, [{"data": {"a": 1}}])

    async def test_error_result(self):
        conn = HAConnection(token="t")
        fut = asyncio.get_running_loop().create_future()
        conn._pending[5] = fut
        await conn._read(FakeWS([{"id": 5, "type": "result", "success": False, "error": {"code": "x", "message": "boom"}}]))
        with self.assertRaises(Exception):
            fut.result()


if __name__ == "__main__":
    unittest.main()
