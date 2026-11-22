import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.card import URL_PATH, CardInstaller  # noqa: E402
from app.engine import Engine, IncidentLog  # noqa: E402
from app.report import Reporter, Reporters, topics  # noqa: E402
from app.store import PinStatus, Store, ValidationError  # noqa: E402
from tests.test_acc_engine import TRIGGER, FakePort  # noqa: E402


class FakeConn:
    def __init__(self, resources=None, fail=False):
        self.resources = resources if resources is not None else []
        self.fail = fail
        self.commands = []

    async def command(self, payload, timeout=30):
        self.commands.append(payload)
        if self.fail:
            raise RuntimeError("not supported in YAML mode")
        if payload["type"] == "lovelace/resources":
            return self.resources
        return {}


class CardInstallerTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_and_register(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "www").mkdir()
            card = CardInstaller(root, version="1.2.3")
            conn = FakeConn()
            await card.run(conn)
            js = (root / "www" / "alarm-contact-center" / "alarm-contact-center-card.js").read_text()
            self.assertIn('const CARD_VERSION = "1.2.3"', js)
            self.assertEqual(conn.commands[-1], {"type": "lovelace/resources/create", "res_type": "module", "url": f"{URL_PATH}?v=1.2.3"})
            self.assertEqual((card.status["file"], card.status["resource"]), ("ok", "ok"))

            conn = FakeConn([{"id": "a", "type": "module", "url": f"{URL_PATH}?v=1.0.0"},
                             {"id": "b", "type": "module", "url": f"{URL_PATH}?v=0.9"}])
            await card.run(conn)
            self.assertEqual(conn.commands[1]["type"], "lovelace/resources/update")
            self.assertEqual(conn.commands[1]["url"], f"{URL_PATH}?v=1.2.3")
            self.assertEqual(conn.commands[2], {"type": "lovelace/resources/delete", "resource_id": "b"})

            conn = FakeConn([{"id": "a", "type": "module", "url": f"{URL_PATH}?v=1.2.3"}])
            await card.run(conn)
            self.assertEqual(len(conn.commands), 1)  # already up to date

    async def test_yaml_mode_and_new_www(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = CardInstaller(Path(tmp), version="1.0")
            await card.run(FakeConn(fail=True))
            self.assertEqual((card.status["file"], card.status["resource"]), ("restart", "manual"))

    async def test_no_config_access(self):
        card = CardInstaller(Path("/nonexistent-acc"), version="1.0")
        conn = FakeConn()
        await card.run(conn)
        self.assertEqual(card.status["file"], "no_config")
        self.assertEqual(conn.commands, [])


class DrillSensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_drill_sensor_with_events_for_the_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "c.json")
            store.save_settings({"call_delay": 0, "rounds": 1})
            c = store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003"}, PinStatus("none", []))
            log = IncidentLog(Path(tmp) / "i.json")
            sent = []

            async def publish(topic, payload, retain):
                sent.append((topic, payload))

            reporters = Reporters([Reporter(publish, log, delay=0), Reporter(publish, log, delay=0, kind="drill")])
            engine = Engine(store, FakePort({"600000003": [{"resultado": "confirmed"}]}), log, time_scale=0)
            engine.listeners.append(reporters.on_change)
            await engine.start_drill([c["id"]], "")
            await engine.current.task
            await reporters.send()
            by_topic = dict(sent)
            _, drill_state, drill_attrs = topics("drill")
            self.assertTrue(by_topic[drill_state].startswith("Simulacro"))
            attrs = json.loads(by_topic[drill_attrs])
            self.assertEqual(attrs["kind"], "drill")
            self.assertEqual(attrs["events"][0]["kind"], "triggered")
            self.assertEqual(attrs["phase"], "open")  # not "calling": the calls are over
            self.assertEqual(by_topic[topics("alarm")[1]], "Sin alarmas registradas")  # a drill is not an alarm


class NotifyUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_notifications_open_the_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "c.json")
            store.save_settings({"call_delay": 0, "rounds": 1, "notify_url": "/alarma/incidente"})
            store.save_contact({"kind": "person", "person": "person.luis", "name": "Luis", "phone": "600000001",
                                "notify_service": "mobile_app_l"}, PinStatus("none", []))
            port = FakePort({"600000001": [{"resultado": "confirmed"}]})
            engine = Engine(store, port, IncidentLog(Path(tmp) / "i.json"), time_scale=0)
            await engine.on_alarm_state("armed_away")
            await engine.on_alarm_state("triggered", TRIGGER)
            await engine.current.task
            data = port.notifications[0][3]
            self.assertEqual((data["url"], data["clickAction"]), ("/alarma/incidente", "/alarma/incidente"))
            with self.assertRaises(ValidationError):
                store.save_settings({"notify_url": "https://evil.example"})
