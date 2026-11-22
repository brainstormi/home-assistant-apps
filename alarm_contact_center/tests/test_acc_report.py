import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine, IncidentLog  # noqa: E402
from app.report import ATTR_TOPIC, DISCOVERY_TOPIC, NO_INCIDENT, STATE_TOPIC, Reporter, summary  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402
from tests.test_acc_engine import TRIGGER, FakePort  # noqa: E402

NOW = dt.datetime(2026, 11, 22, 15, 0).astimezone()


class SummaryTests(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(summary(None), NO_INCIDENT)
        inc = {"started": NOW.replace(hour=14, minute=33).isoformat(), "sensor": "Puerta del garaje",
               "status": "acknowledged", "acknowledged_by": "Luis",
               "last_person": {"camera": "Portal", "start": NOW.replace(hour=14, minute=31).timestamp()}}
        self.assertEqual(summary(inc, NOW), "Alarma hoy a las 14:33, por Puerta del garaje, gestionada por Luis, "
                                            "última persona vista en Portal a las 14:31.")
        inc = {"started": (NOW - dt.timedelta(days=1)).isoformat(), "sensor": "", "status": "no_answer"}
        self.assertEqual(summary(inc, NOW), "Alarma ayer a las 15:00, nadie aceptó gestionarla.")
        self.assertLessEqual(len(summary({**inc, "sensor": "x" * 400}, NOW)), 255)


class ReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_real_alarms_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "c.json")
            store.save_settings({"call_delay": 0, "rounds": 1})
            store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003"}, PinStatus("none", []))
            log = IncidentLog(Path(tmp) / "i.json")
            sent = []

            async def publish(topic, payload, retain):
                sent.append((topic, payload, retain))

            reporter = Reporter(publish, log, delay=0)
            reporter.load()
            await reporter.send(force=True)
            self.assertEqual(dict((t, p) for t, p, _ in sent)[STATE_TOPIC], NO_INCIDENT)
            self.assertEqual(json.loads(sent[0][1])["default_entity_id"], "sensor.alarm_contact_center_incident")
            self.assertEqual(sent[0][0], DISCOVERY_TOPIC)
            sent.clear()

            engine = Engine(store, FakePort(), log, time_scale=0)
            engine.listeners.append(reporter.on_change)
            await engine.start_drill([store.contacts()[0]["id"]], "")
            await engine.current.task
            await reporter.send()
            self.assertEqual(sent, [])  # drills are not published

            await engine.on_alarm_state("armed_away")
            await engine.on_alarm_state("triggered", TRIGGER)
            await engine.current.task
            await reporter.send()
            by_topic = {t: p for t, p, _ in sent}
            self.assertIn("nadie aceptó gestionarla", by_topic[STATE_TOPIC])
            attrs = json.loads(by_topic[ATTR_TOPIC])
            self.assertFalse(attrs["active"])
            self.assertTrue(all(r for _, _, r in sent))
            sent.clear()
            await reporter.send()
            self.assertEqual(sent, [])  # unchanged: nothing re-sent

            reporter2 = Reporter(publish, log)
            reporter2.load()
            self.assertEqual(reporter2.latest["kind"], "alarm")


if __name__ == "__main__":
    unittest.main()


class AssistantContextTests(unittest.IsolatedAsyncioTestCase):
    def test_text(self):
        from app.report import CONTEXT_MAX, assistant_context

        inc = {"kind": "alarm", "sensor": "Puerta del garaje", "started": NOW.replace(hour=14, minute=33).isoformat(),
               "status": "acknowledged", "acknowledged_by": "Luis", "ack_via": "Telegram",
               "acknowledged_at": NOW.replace(hour=14, minute=35).isoformat(), "phase": "open", "persons": [{"camera": "Portal"}],
               "events": [{"t": NOW.replace(hour=14, minute=32).isoformat(), "kind": "person", "text": "Persona en Portal",
                           "detail": "Alerta de Frigate · antes del disparo"},
                          {"t": NOW.replace(hour=14, minute=33).isoformat(), "kind": "wait", "text": "Esperando 30 s", "detail": ""}]}
        text = assistant_context(inc)
        self.assertIn("gestionada por Luis (Telegram) desde las 14:35. Sigue activa hasta que se desarme.", text)
        self.assertIn("Personas vistas por las cámaras: 1 (Portal).", text)
        self.assertIn("14:32:00 Persona en Portal (Vista por la cámara · antes del disparo)", text)
        self.assertNotIn("Esperando", text)
        self.assertNotIn("Frigate", text)
        many = dict(inc, events=[{"t": NOW.isoformat(), "kind": "sensor", "text": f"Ventana {i}: se abre", "detail": ""} for i in range(500)])
        text = assistant_context(many)
        self.assertLessEqual(len(text), CONTEXT_MAX)
        self.assertIn("Ventana 499: se abre", text)  # the most recent are kept
        self.assertIn("entradas anteriores omitidas", text)
        self.assertEqual(assistant_context(None), "")

    async def test_calls_pass_the_context_entity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "c.json")
            store.save_settings({"call_delay": 0, "rounds": 1})
            c = store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003"}, PinStatus("none", []))
            port = FakePort({"600000003": [{"resultado": "confirmed"}, {"resultado": "confirmed"}, {"resultado": "confirmed"}]})
            engine = Engine(store, port, IncidentLog(Path(tmp) / "i.json"), time_scale=0)
            await engine.on_alarm_state("armed_away")
            await engine.on_alarm_state("triggered", TRIGGER)
            await engine.current.task
            await engine.on_alarm_state("disarmed")
            engine.current.record_until = 0
            await engine._closer(engine.current)
            await engine.start_drill([c["id"]], "")
            await engine.current.task
            self.assertEqual(port.calls[0]["contexto_entidad"], "sensor.alarm_contact_center_incident")
            self.assertEqual(port.calls[1]["contexto_entidad"], "sensor.alarm_contact_center_drill")
            await engine.on_alarm_state("disarmed")
            engine.current.record_until = 0
            await engine._closer(engine.current)
            await engine.start_test(c["id"])
            await engine.current.task
            self.assertNotIn("contexto_entidad", port.calls[2])  # test calls have no incident to tell
