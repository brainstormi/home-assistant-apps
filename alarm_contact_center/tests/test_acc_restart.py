"""An incident that was open when the App stopped carries on after the restart."""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine, IncidentLog  # noqa: E402
from app.notify import Notifier, _State  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402
from tests.test_acc_engine import TRIGGER, FakePort  # noqa: E402


class BlockPort(FakePort):
    """Calls to one number hang until released (the App "dies" during that call)."""

    def __init__(self, number, answers=None):
        super().__init__(answers)
        self.number = number
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, params):
        if params["numero"] == self.number:
            self.calls.append(params)
            self.blocked.set()
            await self.release.wait()
            return {"resultado": "no_answer"}
        return await super().call(params)


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "c.json")
        self.store.save_settings({"call_delay": 0, "rounds": 1, "round_pause_min": 0})
        pins = PinStatus("none", [])
        self.a = self.store.save_contact({"kind": "person", "person": "person.luis", "name": "Luis", "phone": "600000001",
                                          "notify_service": "mobile_app_l"}, pins)
        self.b = self.store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000002"}, pins)
        self.c = self.store.save_contact({"kind": "external", "name": "Ana", "phone": "600000003"}, pins)
        self.log = IncidentLog(self.dir / "i.json")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def engine(self, port):
        e = Engine(self.store, port, self.log, time_scale=0)
        e.state_path = self.dir / "open.json"
        return e

    async def die_during(self, engine, inc):
        """The App stops: nothing of its own shutdown runs, only the saved file stays."""
        engine.persist(inc)
        saved = (self.dir / "open.json").read_text()
        inc.task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await inc.task
        (self.dir / "open.json").write_text(saved)

    async def start_and_block_on_second_call(self):
        port = BlockPort("600000002")
        engine = self.engine(port)
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(port.blocked.wait(), 2)  # Luis did not answer; now calling Pablo
        return engine, engine.current

    async def test_chain_resumes_where_it_stopped(self):
        engine, inc = await self.start_and_block_on_second_call()
        await self.die_during(engine, inc)

        port2 = FakePort({"600000003": [{"resultado": "confirmed"}]})
        engine2 = self.engine(port2)
        restored = engine2.restore()
        self.assertEqual(restored.id, inc.id)
        await engine2.on_alarm_state("triggered", TRIGGER, restarted=True)  # the catch-up after connecting
        await engine2.current.task
        cur = engine2.current
        self.assertEqual(cur.id, inc.id)  # the same incident, not a new one
        self.assertEqual([c["numero"] for c in port2.calls], ["600000003"])  # Ana was next
        self.assertEqual(cur.acknowledged_by, "Ana")
        texts = [e["text"] for e in cur.events]
        self.assertTrue(any(t.startswith("La App se ha reiniciado") for t in texts))
        self.assertIn("La llamada a Pablo se interrumpió con el reinicio", texts)
        self.assertNotIn("¡Alarma disparada!", [n[1] for n in port2.notifications])  # not notified again
        self.assertEqual(len([i for i in self.log.items() if i["kind"] == "alarm"]), 1)

    async def test_taken_charge_stays_open_without_calls(self):
        engine, inc = await self.start_and_block_on_second_call()
        await engine.take_charge(inc.id, self.a["id"], "la app")
        engine.persist(inc)
        saved = (self.dir / "open.json").read_text()
        engine.port.release.set()
        await inc.task
        (self.dir / "open.json").write_text(saved)

        port2 = FakePort()
        engine2 = self.engine(port2)
        engine2.restore()
        await engine2.on_alarm_state("armed_away", {}, restarted=True)
        cur = engine2.current
        self.assertEqual((cur.phase(), cur.acknowledged_by), ("open", "Luis"))
        self.assertEqual(port2.calls, [])
        await engine2.on_alarm_state("disarmed")
        self.assertEqual(cur.phase(), "closing")

    async def test_disarmed_while_stopped(self):
        engine, inc = await self.start_and_block_on_second_call()
        await self.die_during(engine, inc)
        port2 = FakePort()
        engine2 = self.engine(port2)
        engine2.restore()
        await engine2.on_alarm_state("disarmed", {}, restarted=True)
        cur = engine2.current
        self.assertEqual(cur.phase(), "closing")
        self.assertIn("Alarma desarmada mientras la App se reiniciaba", [e["text"] for e in cur.events])
        self.assertEqual(port2.calls, [])
        cur.record_until = 0
        await engine2._closer(cur)
        self.assertFalse((self.dir / "open.json").exists())  # closed: nothing left to restore

    async def test_old_or_closed_snapshots_are_ignored(self):
        engine, inc = await self.start_and_block_on_second_call()
        await self.die_during(engine, inc)
        data = json.loads((self.dir / "open.json").read_text())
        data["started"] = time.time() - 24 * 3600
        (self.dir / "open.json").write_text(json.dumps(data))
        engine2 = self.engine(FakePort())
        self.assertIsNone(engine2.restore())
        self.assertFalse((self.dir / "open.json").exists())

    async def test_drill_resumes_too(self):
        port = BlockPort("600000001")
        engine = self.engine(port)
        await engine.start_drill([self.a["id"], self.b["id"]], "")
        inc = engine.current
        await asyncio.wait_for(port.blocked.wait(), 2)
        await self.die_during(engine, inc)
        port2 = FakePort({"600000002": [{"resultado": "confirmed"}]})
        engine2 = self.engine(port2)
        engine2.restore()
        await engine2.on_alarm_state("disarmed", {}, restarted=True)  # Alarmo is irrelevant for drills
        await engine2.current.task
        self.assertEqual(engine2.current.acknowledged_by, "Pablo")
        self.assertEqual(engine2.current.only_contacts, {self.a["id"], self.b["id"]})


class OrphanCallTests(unittest.IsolatedAsyncioTestCase):
    """The call in progress when the App restarted goes on in the Bridge."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "c.json")
        self.store.save_settings({"call_delay": 0, "rounds": 1, "round_pause_min": 0})
        self.luis = self.store.save_contact({"kind": "external", "name": "Luis", "phone": "600000001", "attempts": 3,
                                             "retry_wait": 10}, PinStatus("none", []))
        self.log = IncidentLog(self.dir / "i.json")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def restart_during_call(self):
        port = BlockPort("600000001")
        engine = Engine(self.store, port, self.log, time_scale=0)
        engine.state_path = self.dir / "open.json"
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(port.blocked.wait(), 2)
        inc = engine.current
        await engine.on_bridge_event({"stage": "started", "direction": "outbound", "call_id": "c-1", "number": "600000001"})
        engine.persist(inc)
        saved = (self.dir / "open.json").read_text()
        inc.task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await inc.task
        (self.dir / "open.json").write_text(saved)
        port2 = FakePort({"600000001": [{"resultado": "confirmed"}]})
        engine2 = Engine(self.store, port2, self.log, time_scale=0)
        engine2.state_path = self.dir / "open.json"
        engine2.restore()
        await engine2.on_alarm_state("triggered", TRIGGER, restarted=True)
        return engine2, port2

    async def test_acceptance_in_the_orphan_call_counts(self):
        engine, port = await self.restart_during_call()
        inc = engine.current
        self.assertEqual(inc.phase(), "calling")  # waiting for that call
        self.assertEqual(port.calls, [])
        await engine.on_bridge_event({"stage": "ended", "direction": "outbound", "call_id": "c-1", "result": "confirmed"})
        await inc.task
        self.assertEqual(inc.acknowledged_by, "Luis")
        self.assertEqual(port.calls, [])  # not called again
        self.assertIn("En la llamada que siguió durante el reinicio", [e["detail"] for e in inc.events])

    async def test_unanswered_orphan_then_next_attempt(self):
        engine, port = await self.restart_during_call()
        inc = engine.current
        await engine.on_bridge_event({"stage": "ended", "direction": "outbound", "call_id": "c-1", "result": "no_answer"})
        await inc.task
        texts = [e["text"] for e in inc.events]
        self.assertIn("Llamando a Luis (2/3)", texts)  # the interrupted call was attempt 1
        self.assertNotIn("Llamando a Luis (1/3)", texts[texts.index("Se retoman los avisos donde se quedaron"):])
        self.assertEqual(inc.acknowledged_by, "Luis")

    def test_gap_text(self):
        from app.engine import gap_time

        base = time.mktime((2026, 11, 22, 23, 11, 5, 0, 0, -1))
        self.assertEqual(gap_time(base, base + 40), "23:11:05 y las 23:11:45")
        self.assertEqual(gap_time(base, base + 120), "23:11 y las 23:13")


class NotifierStateTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "c.json")
            n = Notifier(store)
            n.state_path = Path(tmp) / "n.json"
            st = _State()
            st.armed, st.first_email = True, True
            st.tg_messages = {"111222333": 55}
            st.photos_sent = {"e1"}
            st.milestones = {"ack"}
            n.states["inc1"] = st
            done = _State()
            done.done = True
            n.states["old"] = done
            n.save_state()
            n2 = Notifier(store)
            n2.state_path = n.state_path
            n2.load_state()
            self.assertEqual(set(n2.states), {"inc1"})
            got = n2.states["inc1"]
            self.assertEqual((got.armed, got.first_email, got.tg_messages, got.photos_sent, got.milestones),
                             (True, True, {"111222333": 55}, {"e1"}, {"ack"}))


if __name__ == "__main__":
    unittest.main()
