"""No phone line: bursts, Telegram, resuming calls, taking charge by Telegram."""

import asyncio
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import ACK_BUTTON, Engine, IncidentLog  # noqa: E402
from app.notify import Notifier, take_charge_reply  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402
from tests.test_acc_engine import TRIGGER, FakePort  # noqa: E402
from tests.test_acc_notify import FakeTelegram  # noqa: E402

DOWN = {"resultado": "failed", "detalle": {"dialstatus": "CHANUNAVAIL"}}


class NoLineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.json")
        self.store.save_settings({"call_delay": 0, "rounds": 1, "round_pause_min": 0, "external_notify_delay": 0})
        pins = PinStatus("individual", ["person.luis"])
        self.luis = self.store.save_contact({"kind": "person", "person": "person.luis", "name": "Luis", "phone": "600000001",
                                             "notify_service": "mobile_app_l", "can_manage": True, "attempts": 3,
                                             "retry_wait": 10}, pins)
        self.marta = self.store.save_contact({"kind": "person", "person": "person.marta", "name": "Marta", "phone": "600000002",
                                              "notify_service": "mobile_app_m"}, pins)
        self.pablo = self.store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003",
                                              "telegram_chat_id": "222333444"}, pins)
        self.log = IncidentLog(Path(self.tmp.name) / "i.json")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def setup(self, answers=None, hint=False):
        self.port = FakePort(answers or {})
        engine = Engine(self.store, self.port, self.log, time_scale=0)
        engine.line_down_hint = lambda: hint
        self.tg_alerts = []

        async def tg_alert(inc, contact):
            self.tg_alerts.append(contact["name"])

        engine.telegram_alert = tg_alert
        return engine

    async def alarm(self, engine):
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        await engine.current.task
        return engine.current

    def bursts(self, service):
        return [n for n in self.port.notifications if n[0] == service and n[2] != "clear_notification"
                and re.search(r"_b\d+$", str(n[3].get("tag", "")))]

    async def test_registration_down_alerts_without_calling(self):
        engine = self.setup(hint=True)
        inc = await self.alarm(engine)
        self.assertEqual(self.port.calls, [])
        self.assertEqual(inc.status, "trunk_down")
        # Luis (management, 3 attempts): 1+10, 2+20, 3+30
        sent = self.bursts("mobile_app_l")
        self.assertEqual(len(sent), 11 + 22 + 33)
        critical = [n for n in sent if n[3]["push"].get("interruption-level") == "critical"]
        self.assertEqual(len(critical), 1 + 2 + 3)
        self.assertEqual(len({n[3]["tag"] for n in sent}), len(sent))  # each its own tag
        self.assertEqual(sent[0][3]["actions"][0]["title"], ACK_BUTTON)
        self.assertIn("dejaremos de llamar y avisar al resto", sent[0][2])
        clears = [n for n in self.port.notifications if n[0] == "mobile_app_l" and n[2] == "clear_notification"]
        self.assertEqual(len(clears), len(sent))  # all removed again
        # Marta (no management, no Telegram) is skipped; Pablo gets Telegram
        self.assertEqual(self.bursts("mobile_app_m"), [])
        self.assertEqual(self.tg_alerts, ["Pablo"])
        kinds = [e["kind"] for e in inc.events]
        self.assertIn("skip", kinds)
        self.assertEqual(kinds.count("trunk_down"), 1)

    async def test_call_failures_retry_then_alert(self):
        engine = self.setup({"600000001": [dict(DOWN) for _ in range(3)], "600000002": [dict(DOWN)], "600000003": [dict(DOWN)]})
        inc = await self.alarm(engine)
        self.assertEqual([c["numero"] for c in self.port.calls], ["600000001"] * 3 + ["600000002", "600000003"])
        self.assertEqual(sum(e["kind"] == "retry" for e in inc.events), 2)
        self.assertTrue(self.bursts("mobile_app_l"))
        self.assertEqual(inc.status, "trunk_down")

    async def test_one_off_failure_is_not_a_line_down(self):
        engine = self.setup({"600000001": [dict(DOWN), {"resultado": "confirmed"}]})
        inc = await self.alarm(engine)
        self.assertEqual(inc.acknowledged_by, "Luis")
        self.assertFalse(inc.line_down)
        self.assertEqual(self.bursts("mobile_app_l"), [])

    async def test_take_charge_during_a_burst_stops_everything(self):
        engine = self.setup(hint=True)
        engine.time_scale = 0.001
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        inc = engine.current
        for _ in range(200):
            await asyncio.sleep(0.001)
            if len(self.bursts("mobile_app_l")) >= 3:
                break
        result = await engine.take_charge(inc.id, self.marta["id"], "la app")
        await inc.task
        self.assertEqual(result["result"], "ok")
        self.assertEqual(inc.acknowledged_by, "Marta")
        self.assertLess(len(self.bursts("mobile_app_l")), 11)
        self.assertEqual(self.tg_alerts, [])
        self.assertIn("Marta ha aceptado gestionar la alarma", [e["text"] for e in inc.events])
        again = await engine.take_charge(inc.id, self.luis["id"], "Telegram")
        self.assertEqual((again["result"], again["by"]), ("already", "Marta"))

    async def test_calls_resume_when_the_line_returns(self):
        engine = self.setup(hint=True)
        inc = await self.alarm(engine)
        self.assertEqual(inc.phase(), "open")
        engine.line_down_hint = lambda: False
        self.port.answers["600000001"] = [{"resultado": "confirmed"}]
        await engine.on_line_recovered()
        await inc.task
        self.assertEqual(inc.acknowledged_by, "Luis")
        self.assertIn("La línea vuelve a funcionar: se retoman las llamadas", [e["text"] for e in inc.events])
        self.assertEqual(self.port.calls[0]["numero"], "600000001")

    async def test_no_resume_when_someone_took_charge_or_disarmed(self):
        engine = self.setup(hint=True)
        inc = await self.alarm(engine)
        await engine.take_charge(inc.id, self.luis["id"], "la app")
        await engine.on_line_recovered()
        self.assertTrue(inc.task.done())
        self.assertEqual(self.port.calls, [])

    async def test_take_charge_after_the_chain_ended(self):
        engine = self.setup(hint=True)
        inc = await self.alarm(engine)
        self.assertEqual(inc.status, "trunk_down")
        result = await engine.take_charge(inc.id, self.pablo["id"], "Telegram")
        self.assertEqual((result["result"], inc.status, inc.acknowledged_by), ("ok", "acknowledged", "Pablo"))
        await engine.on_alarm_state("disarmed")
        late = await engine.take_charge(inc.id, self.marta["id"], "la app")
        self.assertEqual(late["result"], "already")

    async def test_drill_can_simulate_the_line_down(self):
        engine = self.setup()
        await engine.start_drill([self.luis["id"]], "", line_down=True)
        inc = engine.current
        await inc.task
        self.assertEqual(self.port.calls, [])
        self.assertTrue(self.bursts("mobile_app_l")[0][1].startswith("SIMULACRO"))
        self.assertIn("simulando línea caída", inc.events[0]["detail"])
        await engine.on_line_recovered()  # a simulated line never "returns"
        self.assertTrue(inc.task.done())


class TelegramTakeChargeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.json")
        self.store.save_settings({"call_delay": 0, "rounds": 1, "external_notify_delay": 0})
        self.pablo = self.store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003",
                                              "telegram_chat_id": "222333444"}, PinStatus("none", []))
        self.port = FakePort()
        self.port.gate = asyncio.Event()
        self.engine = Engine(self.store, self.port, IncidentLog(Path(self.tmp.name) / "i.json"), time_scale=0)
        self.tg = FakeTelegram()
        self.notifier = Notifier(self.store, None, time_scale=0, telegram=self.tg, mailer=lambda *a: asyncio.sleep(0))
        self.notifier.engine = self.engine
        self.engine.listeners.append(self.notifier.on_change)

    async def asyncTearDown(self):
        self.port.gate.set()
        self.tmp.cleanup()

    async def start(self):
        await self.engine.on_alarm_state("armed_away")
        await self.engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(self.port.in_call.wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)
            await self.notifier.drain()
        return self.engine.current

    async def test_button_and_text_take_charge(self):
        inc = await self.start()
        self.assertEqual(self.tg.buttons[0], ("222333444", inc.id))  # the live message has the button
        self.assertIn("dejaremos de llamar", self.tg.sent[0][1])
        await self.notifier.on_update(self.tg, {"update_id": 1, "callback_query": {
            "id": "cb1", "data": f"ACC_ACK:{inc.id}", "from": {"first_name": "Pablo"},
            "message": {"chat": {"id": 222333444, "first_name": "Pablo", "type": "private"}}}})
        self.assertEqual(inc.acknowledged_by, "Pablo")
        self.assertEqual(inc.ack_via, "Telegram")
        self.assertTrue(self.tg.sent[-1][1].startswith("✅ Gracias, Pablo: has aceptado gestionar la alarma"))
        await self.notifier.on_update(self.tg, {"update_id": 2, "message": {
            "chat": {"id": 222333444, "first_name": "Pablo", "type": "private"}, "text": "Yo me encargo"}})
        self.assertEqual(self.tg.sent[-1][1], "Ya habías aceptado gestionar la alarma. Sigue activa hasta que se desarme.")
        self.assertIn("222333444", self.notifier.seen_chats)
        for _ in range(5):
            await asyncio.sleep(0)
            await self.notifier.drain()
        self.assertEqual(self.tg.edit_buttons[-1], "")  # button removed once someone took charge

    async def test_strangers_and_no_alarm(self):
        await self.notifier.on_update(self.tg, {"update_id": 1, "message": {"chat": {"id": 999888777, "type": "private"},
                                                                             "text": "lo atiendo"}})
        self.assertEqual(self.tg.sent[-1][1], "No hay ninguna alarma abierta ahora mismo.")
        inc = await self.start()
        await self.notifier.on_update(self.tg, {"update_id": 2, "callback_query": {
            "id": "cb", "data": f"ACC_ACK:{inc.id}", "message": {"chat": {"id": 999888777, "type": "private"}}}})
        self.assertEqual(self.tg.sent[-1][1], "Este chat no está entre los contactos de la alarma.")
        self.assertEqual(inc.acknowledged_by, "")

    def test_replies(self):
        self.assertIn("Ya se encarga Luis desde las 21:30", take_charge_reply({"result": "already", "by": "Luis", "at": "21:30"}, "Pablo"))
        self.assertIn("ya está desarmada", take_charge_reply({"result": "disarmed"}, "Pablo"))


if __name__ == "__main__":
    unittest.main()
