import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import ACK_PREFIX, Engine, IncidentLog, render_message  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402


class FakePort:
    """Scripted Home Assistant. ``answers`` maps a phone number to a list of results."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.notes = []
        self.resolves = []
        self.hangups = []
        self.notifications = []
        self.stopped_queue = 0
        self.gate = None  # asyncio.Event: when set, calls wait for it (to act mid-call)
        self.in_call = asyncio.Event()

    async def call(self, params):
        self.calls.append(params)
        self.in_call.set()
        if self.gate is not None:
            await self.gate.wait()
        self.in_call.clear()
        queue = self.answers.get(params["numero"], [])
        result = queue.pop(0) if queue else {"resultado": "no_answer"}
        return result

    async def notify(self, service, title, message, data):
        self.notifications.append((service, title, message, data))

    async def bridge_note(self, call_id, text, resolve=False):
        self.notes.append((call_id, text))
        self.resolves.append(resolve)

    async def bridge_hangup(self, call_id=""):
        self.hangups.append(call_id)

    async def stop_queued_calls(self):
        self.stopped_queue += 1

    async def friendly_name(self, entity_id):
        return {"binary_sensor.garaje": "Puerta del garaje"}.get(entity_id, entity_id)


TRIGGER = {"open_sensors": {"binary_sensor.garaje": "on"}}


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.json")
        self.store.save_settings({"call_delay": 30, "rounds": 2, "round_pause_min": 0})
        pins = PinStatus("individual", ["person.luis"])
        self.luis = self.store.save_contact({"kind": "person", "person": "person.luis", "name": "Luis", "phone": "600000001",
                                             "notify_service": "mobile_app_r", "can_manage": True, "attempts": 2}, pins)
        self.marta = self.store.save_contact({"kind": "person", "person": "person.marta", "name": "Marta", "phone": "600000002",
                                              "notify_service": "mobile_app_s"}, pins)
        self.pablo = self.store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003"}, pins)
        self.log = IncidentLog(Path(self.tmp.name) / "i.json")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def engine(self, port):
        return Engine(self.store, port, self.log, time_scale=0)

    async def run_alarm(self, engine):
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        await engine.current.task
        return engine.current

    async def test_confirmed_stops_chain(self):
        port = FakePort({"600000002": [{"resultado": "confirmed", "detalle": {"confirmed_by": "dtmf"}}]})
        inc = await self.run_alarm(self.engine(port))
        self.assertEqual(inc.status, "acknowledged")
        self.assertEqual(inc.acknowledged_by, "Marta")
        self.assertEqual([c["numero"] for c in port.calls], ["600000001", "600000001", "600000002"])
        self.assertEqual(port.stopped_queue, 1)
        self.assertIn("", port.hangups)  # priority: hang up whatever was on the line

    async def test_profiles(self):
        port = FakePort({"600000003": [{"resultado": "confirmed"}]})
        await self.run_alarm(self.engine(port))
        by_number = {c["numero"]: c for c in port.calls}
        self.assertEqual(by_number["600000001"]["perfil"], "Management")
        self.assertEqual(by_number["600000001"]["persona"], "person.luis")
        self.assertEqual(by_number["600000002"]["perfil"], "QueryOnly")
        self.assertEqual(by_number["600000003"]["perfil"], "QueryOnly")
        self.assertTrue(all(c["intentos"] == 1 for c in port.calls))

    async def test_circular_rounds(self):
        port = FakePort()
        inc = await self.run_alarm(self.engine(port))
        # 2 rounds x (Luis 2 attempts + Marta + Pablo)
        self.assertEqual(len(port.calls), 8)
        self.assertEqual(port.calls[4]["numero"], "600000001")
        self.assertEqual(inc.status, "no_answer")

    async def test_unlimited_rounds_capped(self):
        self.store.save_settings({"rounds": 0, "max_chain_minutes": 5})
        port = FakePort()
        engine = self.engine(port)
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.sleep(0.05)
        self.assertGreater(len(port.calls), 8)  # keeps going round
        engine.current.started -= 6 * 60  # pretend 6 minutes went by
        await asyncio.wait_for(engine.current.task, 2)
        self.assertEqual(engine.current.status, "no_answer")

    async def test_message_mentions_previous(self):
        port = FakePort({"600000003": [{"resultado": "confirmed"}]})
        await self.run_alarm(self.engine(port))
        last = port.calls[-1]["mensaje"]
        self.assertIn("Pablo", last)
        self.assertIn("Puerta del garaje", last)
        self.assertIn("Luis y Marta no han contestado", last)

    async def test_disarmed_during_delay_calls_nobody(self):
        self.store.save_settings({"call_delay": 30})
        port = FakePort()
        engine = Engine(self.store, port, self.log, time_scale=1)  # real waiting for the delay
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.sleep(0.05)
        await engine.on_alarm_state("disarmed")
        await asyncio.wait_for(engine.current.task, 2)
        self.assertEqual(port.calls, [])
        self.assertEqual(engine.current.status, "disarmed")
        titles = [n[1] for n in port.notifications]
        self.assertIn("¡Alarma disparada!", titles)
        self.assertIn("Alarma desarmada", titles)

    async def test_app_button_acknowledges_and_informs_call(self):
        port = FakePort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(port.in_call.wait(), 2)
        inc = engine.current
        await engine.on_bridge_event({"stage": "started", "direction": "outbound", "call_id": "abc", "number": "+34600000001"})
        await engine.on_bridge_event({"stage": "answered", "direction": "outbound", "call_id": "abc"})
        await engine.on_notification_action(f"{ACK_PREFIX}:{inc.id}:{self.marta['id']}")
        port.gate.set()
        await asyncio.wait_for(inc.task, 2)
        self.assertEqual(inc.status, "acknowledged")
        self.assertEqual(inc.acknowledged_by, "Marta")
        self.assertEqual(port.notes, [("abc", "Te informo de que Marta ha aceptado gestionar la alarma.")])
        self.assertEqual(port.resolves, [True])  # the notice is closed, no more confirmation
        self.assertEqual(len(port.calls), 1)  # no more calls after the button
        # The button never disarms anything: the engine has no way to do it.

    async def test_ack_while_ringing_withdraws_call(self):
        port = FakePort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(port.in_call.wait(), 2)
        await engine.on_bridge_event({"stage": "started", "direction": "outbound", "call_id": "xyz", "number": "600000001"})
        await engine.on_notification_action(f"{ACK_PREFIX}:{engine.current.id}:{self.marta['id']}")
        port.gate.set()
        await asyncio.wait_for(engine.current.task, 2)
        self.assertIn("xyz", port.hangups)
        self.assertEqual(port.notes, [])

    async def test_trunk_down_alerts_by_notifications(self):
        down = {"resultado": "failed", "detalle": {"dialstatus": "CHANUNAVAIL"}}
        port = FakePort({"600000001": [dict(down) for _ in range(9)], "600000002": [dict(down) for _ in range(9)],
                         "600000003": [dict(down) for _ in range(9)]})
        inc = await self.run_alarm(self.engine(port))
        self.assertEqual(inc.status, "trunk_down")
        # 3 tries for the first call (2 retries), then a single try per contact
        self.assertEqual([c["numero"] for c in port.calls][:3], ["600000001"] * 3)
        self.assertTrue(any("no funcionan" in n[1] for n in port.notifications))
        self.assertTrue(any(e["kind"] == "burst" for e in inc.events))

    async def test_busy_is_not_trunk_down(self):
        port = FakePort({"600000001": [{"resultado": "failed", "detalle": {"dialstatus": "BUSY"}}],
                         "600000002": [{"resultado": "confirmed"}]})
        inc = await self.run_alarm(self.engine(port))
        self.assertEqual(inc.acknowledged_by, "Marta")

    async def test_test_call_only_one_contact_no_notifications(self):
        port = FakePort({"600000003": [{"resultado": "confirmed"}]})
        engine = self.engine(port)
        await engine.start_test(self.pablo["id"])
        await engine.current.task
        self.assertEqual(len(port.calls), 1)
        self.assertTrue(port.calls[0]["mensaje"].startswith("Esto es una prueba"))
        self.assertEqual(port.notifications, [])
        self.assertEqual(engine.current.status, "acknowledged")

    async def test_alarm_preempts_test(self):
        port = FakePort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.start_test(self.pablo["id"])
        await asyncio.wait_for(port.in_call.wait(), 2)
        test = engine.current
        await engine.on_bridge_event({"stage": "started", "direction": "outbound", "call_id": "t1", "number": "600000003"})
        port.gate.set()
        await engine.on_alarm_state("triggered", TRIGGER)
        self.assertEqual(test.status, "cancelled")
        self.assertEqual(engine.current.kind, "alarm")
        await asyncio.wait_for(engine.current.task, 2)

    async def test_no_test_during_alarm(self):
        port = FakePort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.on_alarm_state("triggered", TRIGGER)
        with self.assertRaises(RuntimeError):
            await engine.start_test(self.pablo["id"])
        port.gate.set()
        await engine.cancel()
        await asyncio.wait_for(engine.current.task, 2)

    async def test_incident_is_logged(self):
        port = FakePort({"600000001": [{"resultado": "confirmed"}]})
        await self.run_alarm(self.engine(port))
        items = IncidentLog(Path(self.tmp.name) / "i.json").items()
        self.assertEqual(items[0]["status"], "acknowledged")
        self.assertTrue(any(e["kind"] == "confirmed" for e in items[0]["events"]))
        self.assertFalse(items[0]["active"])

    async def test_drill_only_participants_no_priority(self):
        port = FakePort({"600000003": [{"resultado": "confirmed"}]})
        engine = self.engine(port)
        await engine.start_drill([self.marta["id"], self.pablo["id"]], "binary_sensor.garaje")
        inc = engine.current
        await inc.task
        self.assertEqual(inc.kind, "drill")
        self.assertEqual(inc.sensor, "Puerta del garaje")
        self.assertEqual([c["numero"] for c in port.calls], ["600000002", "600000003"])
        self.assertTrue(all(c["mensaje"].startswith("Esto es un simulacro") for c in port.calls))
        self.assertEqual(port.stopped_queue, 0)  # a drill never takes the line from others
        self.assertNotIn("", port.hangups)
        services = {n[0] for n in port.notifications}
        self.assertEqual(services, {"mobile_app_s"})  # Luis is not taking part
        self.assertTrue(all(n[1].startswith("SIMULACRO") for n in port.notifications))
        self.assertEqual(inc.acknowledged_by, "Pablo")

    async def test_drill_simulated_disarm(self):
        self.store.save_settings({"call_delay": 30})
        port = FakePort()
        engine = Engine(self.store, port, self.log, time_scale=1)
        await engine.start_drill([self.luis["id"]], "")
        await asyncio.sleep(0.05)
        await engine.simulate_disarm()
        await asyncio.wait_for(engine.current.task, 2)
        self.assertEqual(engine.current.status, "disarmed")
        self.assertEqual(port.calls, [])

    async def test_real_alarm_disarm_does_not_touch_drill_and_alarm_preempts_it(self):
        port = FakePort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.start_drill([self.luis["id"]], "")
        drill = engine.current
        await asyncio.wait_for(port.in_call.wait(), 2)
        await engine.on_alarm_state("disarmed")  # Alarmo disarmed: unrelated to the drill
        self.assertTrue(engine._active(drill))
        port.gate.set()
        await engine.on_alarm_state("triggered", TRIGGER)
        self.assertEqual(drill.status, "cancelled")
        self.assertEqual(engine.current.kind, "alarm")
        await engine.cancel()
        await asyncio.wait_for(engine.current.task, 2)

    async def test_disarm_during_call_is_final(self):
        # Disarmed (twice) while a call is going on; the person then confirms.
        port = FakePort({"600000001": [{"resultado": "confirmed"}]})
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.start_drill([self.luis["id"]], "")
        await asyncio.wait_for(port.in_call.wait(), 2)
        inc = engine.current
        await engine.on_bridge_event({"stage": "started", "direction": "outbound", "call_id": "d1", "number": "600000001"})
        await engine.on_bridge_event({"stage": "answered", "direction": "outbound", "call_id": "d1"})
        await engine.simulate_disarm()
        with self.assertRaises(RuntimeError):
            await engine.simulate_disarm()  # still running, but already settled
        port.gate.set()
        await asyncio.wait_for(inc.task, 2)
        self.assertEqual(inc.status, "disarmed")
        self.assertEqual(inc.acknowledged_by, "")
        self.assertEqual(len(port.notes), 1)
        titles = [n[1] for n in port.notifications]
        self.assertEqual(titles.count("SIMULACRO · Alarma desarmada"), 1)
        self.assertNotIn("SIMULACRO · Alarma atendida", titles)
        self.assertEqual(inc.events[-1]["kind"], "after")

    async def test_drill_needs_contacts(self):
        with self.assertRaises(RuntimeError):
            await self.engine(FakePort()).start_drill(["nope"], "")

    async def test_busy_wording_and_effective_profile(self):
        port = FakePort({"600000001": [{"resultado": "failed", "detalle": {"dialstatus": "BUSY"}}]})
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.start_test(self.luis["id"])
        await asyncio.wait_for(port.in_call.wait(), 2)
        await engine.on_bridge_event({"stage": "started", "direction": "outbound", "call_id": "c1",
                                      "number": "600000001", "effective_profile": "QueryOnly"})
        port.gate.set()
        await engine.current.task
        texts = [e["text"] for e in engine.current.events]
        self.assertIn("Luis rechaza la llamada o comunica", texts)
        self.assertTrue(any("solo consulta" in t for t in texts))

    async def test_own_ack_wording(self):
        # The person on the phone presses the app button themselves.
        port = FakePort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(port.in_call.wait(), 2)
        await engine.on_bridge_event({"stage": "started", "direction": "outbound", "call_id": "o1", "number": "600000001"})
        await engine.on_bridge_event({"stage": "answered", "direction": "outbound", "call_id": "o1"})
        await engine.on_notification_action(f"{ACK_PREFIX}:{engine.current.id}:{self.luis['id']}")
        port.gate.set()
        await asyncio.wait_for(engine.current.task, 2)
        self.assertEqual(port.notes, [("o1", "Gracias. Has aceptado gestionar la alarma: ya no avisaremos a nadie más. "
                                             "La alarma sigue activa hasta que se desarme.")])

    async def test_voicemail_and_silence(self):
        self.store.save_settings({"rounds": 1})
        port = FakePort({
            "600000001": [{"resultado": "voicemail", "detalle": {"detected_by": "assistant", "voicemail_message_left": True}},
                          {"resultado": "answered", "detalle": {"ended_by": "silence"}}],
            "600000002": [{"resultado": "confirmed"}],
        })
        inc = await self.run_alarm(self.engine(port))
        self.assertEqual(inc.acknowledged_by, "Marta")  # voicemail and silence never confirm
        texts = [(e["text"], e["detail"]) for e in inc.events]
        self.assertIn(("Salta el buzón de voz de Luis", "Mensaje dejado en el buzón"), texts)
        self.assertTrue(any(t == "Luis contesta pero no responde" for t, _ in texts))
        first = port.calls[0]
        self.assertTrue(first["detectar_buzon"])
        self.assertIn("Luis", first["mensaje_buzon"])
        self.assertIn("Puerta del garaje", first["mensaje_buzon"])

    async def test_voicemail_detection_off(self):
        self.store.save_settings({"detect_voicemail": False})
        port = FakePort({"600000001": [{"resultado": "confirmed"}]})
        await self.run_alarm(self.engine(port))
        self.assertNotIn("detectar_buzon", port.calls[0])

    async def test_drill_voicemail_message_marked(self):
        port = FakePort({"600000003": [{"resultado": "confirmed"}]})
        engine = self.engine(port)
        await engine.start_drill([self.pablo["id"]], "")
        await engine.current.task
        self.assertTrue(port.calls[0]["mensaje_buzon"].startswith("Esto es un simulacro"))

    def test_render_message(self):
        text = render_message("Hola {nombre}. {sensor} a las {hora}. {atendido}", nombre="Ana", sensor="Garaje", hora="23:41", atendido="")
        self.assertEqual(text, "Hola Ana. Garaje a las 23:41.")


if __name__ == "__main__":
    unittest.main()
