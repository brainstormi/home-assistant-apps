import asyncio
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import IncidentLog  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402
from app.trunk import TrunkMonitor, next_run  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=1))


class FakePort:
    def __init__(self, answer=None):
        self.commands = []
        self.notifications = []
        self.answer = answer  # dict sent back as the Bridge event, or None (no answer)
        self.monitor = None

    async def bridge_command(self, command):
        self.commands.append(command)
        if self.answer is not None:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.monitor.on_bridge_event({"test_id": command["test_id"], **self.answer})))

    async def notify(self, service, title, message, data):
        self.notifications.append((service, title, message))


class FakeEngine:
    current = None


class NextRunTests(unittest.TestCase):
    def test_daily(self):
        now = dt.datetime(2026, 11, 22, 3, 0, tzinfo=TZ)
        self.assertEqual(next_run(now, "daily", 4), dt.datetime(2026, 11, 22, 4, 7, tzinfo=TZ))
        now = dt.datetime(2026, 11, 22, 5, 0, tzinfo=TZ)
        self.assertEqual(next_run(now, "daily", 4), dt.datetime(2026, 11, 23, 4, 7, tzinfo=TZ))

    def test_every_6h(self):
        now = dt.datetime(2026, 11, 22, 11, 0, tzinfo=TZ)
        self.assertEqual(next_run(now, "every_6h", 4), dt.datetime(2026, 11, 22, 16, 7, tzinfo=TZ))
        now = dt.datetime(2026, 11, 22, 23, 0, tzinfo=TZ)
        self.assertEqual(next_run(now, "every_6h", 4), dt.datetime(2026, 11, 23, 4, 7, tzinfo=TZ))

    def test_weekly_on_monday(self):
        now = dt.datetime(2026, 11, 22, 11, 0, tzinfo=TZ)  # a Sunday
        self.assertEqual(next_run(now, "weekly", 4), dt.datetime(2026, 11, 23, 4, 7, tzinfo=TZ))

    def test_off(self):
        self.assertIsNone(next_run(dt.datetime(2026, 11, 22, tzinfo=TZ), "off", 4))


class TrunkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.json")
        self.store.save_settings({"trunk_test_number": "+34 600 000 009", "trunk_alert_after_min": 2})
        self.store.save_contact({"kind": "person", "person": "person.luis", "name": "Luis", "phone": "600000001",
                                 "notify_service": "mobile_app_l"}, PinStatus())
        self.log = IncidentLog(Path(self.tmp.name) / "i.json")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def monitor(self, port, engine=None):
        m = TrunkMonitor(self.store, port, self.log, engine or FakeEngine(), Path(self.tmp.name) / "t.json", time_scale=0)
        port.monitor = m
        return m

    async def test_ok_test(self):
        port = FakePort({"result": "ok"})
        m = self.monitor(port)
        test = await m.run_test(manual=True)
        self.assertEqual(test["result"], "ok")
        self.assertEqual(port.commands[0]["action"], "trunk_test")
        self.assertEqual(port.commands[0]["number"], "600000009")
        self.assertEqual(port.notifications, [])
        self.assertTrue(m.status()["bridge_answers"])

    async def test_failure_alerts_once_then_recovers(self):
        port = FakePort({"result": "failed", "dialstatus": "CHANUNAVAIL"})
        m = self.monitor(port)
        await m.run_test()
        await m.run_test()
        self.assertEqual([n[1] for n in port.notifications], ["Falla la prueba de la línea"])
        port.answer = {"result": "ok"}
        await m.run_test()
        self.assertEqual(port.notifications[-1][1], "Línea telefónica recuperada")
        kinds = [i["status"] for i in self.log.items()]
        self.assertEqual(kinds, ["ok", "failed"])

    async def test_old_bridge_without_answer_does_not_alert(self):
        port = FakePort(None)
        m = self.monitor(port)
        test = await m.run_test()
        self.assertEqual(test["result"], "timeout")
        self.assertEqual(port.notifications, [])
        self.assertFalse(m.status()["bridge_answers"])

    async def test_timeout_alerts_once_bridge_known(self):
        port = FakePort({"result": "ok"})
        m = self.monitor(port)
        await m.run_test()
        port.answer = None
        await m.run_test()
        self.assertEqual([n[1] for n in port.notifications], ["Falla la prueba de la línea"])

    async def test_needs_number_and_free_line(self):
        self.store.save_settings({"trunk_test_number": ""})
        with self.assertRaises(RuntimeError):
            await self.monitor(FakePort({"result": "ok"})).run_test()
        self.store.save_settings({"trunk_test_number": "600000009"})

        class Busy:
            class current:  # noqa: N801
                task = asyncio.get_running_loop().create_future()

        with self.assertRaises(RuntimeError):
            await self.monitor(FakePort({"result": "ok"}), Busy()).run_test()

    async def test_registration_down_alerts_after_delay_and_recovers(self):
        port = FakePort()
        m = self.monitor(port)
        await m.on_entity_state("binary_sensor.siptrunk_registered", "off")
        await asyncio.sleep(0.01)
        self.assertEqual([n[1] for n in port.notifications], ["Línea telefónica caída"])
        self.assertIn("binary_sensor.siptrunk_registered", m.status()["down_since"])
        await m.on_entity_state("binary_sensor.siptrunk_registered", "on")
        self.assertEqual(port.notifications[-1][1], "Línea telefónica recuperada")
        self.assertEqual(m.status()["down_since"], {})

    async def test_short_blip_no_alert(self):
        port = FakePort()
        m = TrunkMonitor(self.store, port, self.log, FakeEngine(), Path(self.tmp.name) / "t.json", time_scale=1)
        await m.on_entity_state("binary_sensor.ami_connected", "unavailable")
        await m.on_entity_state("binary_sensor.ami_connected", "on")
        await asyncio.sleep(0.01)
        self.assertEqual(port.notifications, [])

    async def test_flapping_alerts_once_per_window(self):
        port = FakePort()
        m = TrunkMonitor(self.store, port, self.log, FakeEngine(), Path(self.tmp.name) / "t.json", time_scale=1)
        eid = "binary_sensor.siptrunk_registered"
        for _ in range(5):  # five short drops, none long enough for the "down" alert
            await m.on_entity_state(eid, "unavailable")
            await m.on_entity_state(eid, "on")
        await asyncio.sleep(0.01)
        self.assertEqual([n[1] for n in port.notifications], ["Línea telefónica inestable"])
        self.assertIn("3 veces", port.notifications[0][2])
        st = m.status()["stability"]["trunk"]
        self.assertEqual(st["drops_24h"], 5)
        self.assertIsNotNone(st["last_drop"])
        self.assertEqual(m.status()["stability"]["ami"]["drops_24h"], 0)

    async def test_drops_survive_restart_and_expire(self):
        port = FakePort()
        m = TrunkMonitor(self.store, port, self.log, FakeEngine(), Path(self.tmp.name) / "t.json", time_scale=1)
        await m.on_entity_state("binary_sensor.ami_connected", "off")
        await m.on_entity_state("binary_sensor.ami_connected", "on")
        again = TrunkMonitor(self.store, port, self.log, FakeEngine(), Path(self.tmp.name) / "t.json", time_scale=1)
        self.assertEqual(again.status()["stability"]["ami"]["drops_24h"], 1)
        import time as _t
        later = again.stability(now=_t.time() + 2 * 86400)
        self.assertEqual(later["ami"]["drops_24h"], 0)
        self.assertEqual(later["ami"]["drops_7d"], 1)

    async def test_flap_settings_validated(self):
        from app.store import ValidationError, clean_settings
        with self.assertRaises(ValidationError):
            clean_settings({"flap_alert_count": 1})
        with self.assertRaises(ValidationError):
            clean_settings({"flap_window_min": 5})

    async def test_other_entities_ignored(self):
        port = FakePort()
        m = self.monitor(port)
        await m.on_entity_state("binary_sensor.other", "off")
        self.assertEqual(m.status()["down_since"], {})


if __name__ == "__main__":
    unittest.main()
