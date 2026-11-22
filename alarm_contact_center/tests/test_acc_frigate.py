import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine, IncidentLog  # noqa: E402
from app.frigate import FrigateAPI, FrigateWatcher, camera_names, parse_value, pick_helpers, url_from_title  # noqa: E402
from app.media import MediaStore  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402
from tests.test_acc_engine import TRIGGER, FakePort  # noqa: E402


def rid(t, suffix="abcdef"):
    return f"{t:.6f}-{suffix}"


class HelperFormatTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_value("1795354401.831865-vqtljq,1795354411.489668-aradwo"),
                         ("1795354401.831865-vqtljq", ["1795354411.489668-aradwo"]))
        self.assertIsNone(parse_value(""))
        self.assertIsNone(parse_value("hola"))
        self.assertIsNone(parse_value("1795354401.8-vqtljq,otra cosa"))

    def test_camera_names_drop_shared_words(self):
        names = camera_names({"input_text.a": "Alerta persona portal", "input_text.b": "Alerta persona huerto"})
        self.assertEqual(names, {"input_text.a": "Portal", "input_text.b": "Huerto"})
        self.assertEqual(camera_names({"input_text.a": "Puerta principal"}), {"input_text.a": "Puerta principal"})
        self.assertEqual(camera_names({"input_text.garden_person": ""}), {"input_text.garden_person": "Garden person"})

    def test_pick_helpers_detects_by_value(self):
        states = [
            {"entity_id": "input_text.cam_a", "state": rid(1795354401.8), "attributes": {"friendly_name": "Persona A"}},
            {"entity_id": "input_text.nota", "state": "hola", "attributes": {}},
            {"entity_id": "sensor.x", "state": rid(1795354401.8), "attributes": {}},
        ]
        self.assertEqual(pick_helpers([], states), {"input_text.cam_a": "Persona A"})
        self.assertEqual(pick_helpers(["input_text.nota"], states), {"input_text.nota": ""})

    def test_watcher_merges_updates_and_window(self):
        now = 1795354500.0
        fw = FrigateWatcher()
        fw.set_helpers({"input_text.a": "Alerta persona portal", "input_text.b": "Alerta persona patio"})
        alert, new = fw.on_value("input_text.a", rid(now - 100) + "," + rid(now - 99, "det001"), now=now)
        self.assertTrue(new)
        self.assertEqual((alert.camera, alert.event_id), ("Portal", f"{now - 99:.6f}-det001"))
        again, new = fw.on_value("input_text.a", rid(now - 100) + "," + rid(now - 99, "det001") + "," + rid(now - 90, "det002"), now=now)
        self.assertFalse(new)
        self.assertIs(again, alert)
        self.assertEqual(len(alert.detections), 2)
        self.assertEqual(alert.event_id, f"{now - 99:.6f}-det001")  # the first one stays
        fw.on_value("input_text.b", rid(now - 50, "zzzzzz"), now=now)
        self.assertEqual([a.camera for a in fw.window(now - 200, now)], ["Portal", "Patio"])
        self.assertEqual(fw.window(now - 60, now)[0].camera, "Patio")
        self.assertEqual(fw.latest().camera, "Patio")
        old, _ = fw.on_value("input_text.a", rid(now - 5 * 3600), now=now)
        self.assertIsNone(old)  # older than what the App keeps


class FrigateAPITests(unittest.TestCase):
    def test_address(self):
        self.assertEqual(url_from_title("frigate.local:5000"), "http://frigate.local:5000")
        self.assertEqual(url_from_title("https://nvr.example/"), "https://nvr.example")
        self.assertEqual(url_from_title("Mi Frigate"), "")
        settings = {"frigate_url": ""}
        api = FrigateAPI(lambda: settings)
        api.detected = "http://frigate.local:5000"
        self.assertEqual(api.base, "http://frigate.local:5000")
        settings["frigate_url"] = "http://otro:5000/"
        self.assertEqual(api.base, "http://otro:5000")
        settings["frigate_url"] = ""
        api.detected = ""
        with self.assertRaises(OSError):
            api.open("1795354401.8-abcdef", "clip.mp4")


class MediaPort(FakePort):
    def __init__(self, answers=None):
        super().__init__(answers)
        self.retained = []
        self.fetched = []

    async def frigate_retain(self, event_id):
        self.retained.append(event_id)

    async def frigate_snapshot(self, event_id):
        self.fetched.append(event_id)
        return b"\xff\xd8jpeg"


class EngineFrigateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.json")
        self.store.save_settings({"call_delay": 0, "rounds": 1, "frigate_pre_minutes": 5,
                                  "incident_entities": ["binary_sensor.puerta_cocina"]})
        self.store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003"}, PinStatus("none", []))
        self.media = MediaStore(Path(self.tmp.name) / "media")
        self.log = IncidentLog(Path(self.tmp.name) / "i.json")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def engine(self, port):
        e = Engine(self.store, port, self.log, time_scale=0, media=self.media)
        e.frigate.set_helpers({"input_text.a": "Alerta persona portal", "input_text.b": "Alerta persona patio"})
        return e

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0)

    async def test_alerts_before_and_during_the_incident(self):
        port = MediaPort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        now = time.time()
        await engine.on_person_helper("input_text.a", rid(now - 120) + "," + rid(now - 119, "det001"))
        await engine.on_person_helper("input_text.b", rid(now - 3600, "oldone"))  # outside the 5 min window
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(port.in_call.wait(), 2)
        inc = engine.current
        kinds = [e["kind"] for e in inc.events]
        self.assertEqual(kinds[0], "person")  # before the trigger, ahead of it
        self.assertIn("antes del disparo", inc.events[0]["detail"])
        self.assertLess(kinds.index("person"), kinds.index("triggered"))
        await engine.on_person_helper("input_text.b", rid(now + 5, "during") + "," + rid(now + 6, "det002"))
        await engine.on_watched_entity("binary_sensor.puerta_cocina",
                                       {"state": "on", "attributes": {"friendly_name": "Puerta cocina", "device_class": "door"}},
                                       {"state": "off"})
        await engine.on_watched_entity("binary_sensor.otra", {"state": "on", "attributes": {}}, {"state": "off"})
        await self.settle()
        texts = [e["text"] for e in inc.events]
        self.assertIn("Persona en Patio", texts)
        self.assertIn("Puerta cocina: se abre", texts)
        self.assertFalse(any("otra" in t for t in texts))
        self.assertEqual(len(inc.persons), 2)
        self.assertEqual(inc.to_dict()["last_person"]["camera"], "Patio")
        self.assertEqual(sorted(port.retained), sorted([f"{now - 119:.6f}-det001", f"{now + 6:.6f}-det002"]))
        self.assertEqual(self.media.read(inc.id, f"{now - 119:.6f}-det001"), b"\xff\xd8jpeg")
        first = next(e for e in inc.events if e["kind"] == "person")
        self.assertEqual(first["media"]["snapshot"], f"{now - 119:.6f}-det001")
        port.gate.set()
        await inc.task

    async def test_updates_do_not_duplicate(self):
        port = MediaPort()
        port.gate = asyncio.Event()
        engine = self.engine(port)
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(port.in_call.wait(), 2)
        now = time.time()
        value = rid(now, "review") + "," + rid(now, "det001")
        await engine.on_person_helper("input_text.a", value)
        await engine.on_person_helper("input_text.a", value + "," + rid(now + 2, "det002"))
        await self.settle()
        inc = engine.current
        self.assertEqual(sum(e["kind"] == "person" for e in inc.events), 1)
        self.assertEqual(port.fetched, [f"{now:.6f}-det001"])
        port.gate.set()
        await inc.task

    async def test_test_calls_and_idle_ignore_alerts(self):
        port = MediaPort()
        engine = self.engine(port)
        await engine.on_person_helper("input_text.a", rid(time.time(), "review"))
        self.assertIsNone(engine.current)
        self.assertEqual(port.retained, [])


class MediaStoreTests(unittest.TestCase):
    def test_paths_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = MediaStore(tmp)
            self.assertIsNone(m.path("../x", "y"))
            self.assertFalse(m.save("inc1", "../../etc", b"x"))
            self.assertTrue(m.save("inc1", "1795354401.8-abcdef", b"x"))
            self.assertEqual(m.read("inc1", "1795354401.8-abcdef"), b"x")
            self.assertEqual(m.cleanup(90), 0)
            self.assertEqual(m.cleanup(90, now=time.time() + 91 * 86400), 1)
            self.assertIsNone(m.read("inc1", "1795354401.8-abcdef"))


if __name__ == "__main__":
    unittest.main()


class DescribeTests(unittest.TestCase):
    def test_texts(self):
        from app.engine import describe_change

        self.assertEqual(describe_change("binary_sensor.ventana", "on", "window"), "se abre")
        self.assertEqual(describe_change("binary_sensor.ventana", "off", "window"), "se cierra")
        self.assertEqual(describe_change("binary_sensor.salon_occupancy", "on", "occupancy"), "detecta presencia")
        self.assertEqual(describe_change("binary_sensor.salon_occupancy", "off", "occupancy"), "")
        self.assertEqual(describe_change("binary_sensor.patio_person_occupancy", "on", "occupancy"), "detecta una persona")
        self.assertEqual(describe_change("binary_sensor.pir", "on", "motion"), "detecta movimiento")
        self.assertEqual(describe_change("lock.puerta", "unlocked", ""), "se abre la cerradura")
        self.assertEqual(describe_change("lock.puerta", "unlocking", ""), "")
        self.assertEqual(describe_change("lock.puerta", "unavailable", ""), "no disponible")


class PositionTests(unittest.TestCase):
    def test_end_position_sensors(self):
        from app.engine import describe_position

        self.assertEqual(describe_position("Puerta Patio Cerrada", "on", "door"), ("Puerta Patio", "abierta"))
        self.assertEqual(describe_position("Puerta Patio Cerrada", "off", "door"), ("Puerta Patio", "cerrada"))
        self.assertEqual(describe_position("Puerta Patio Abierta", "on", "door"), ("Puerta Patio", "abierta del todo"))
        self.assertEqual(describe_position("Puerta Patio Abierta", "off", "door"), ("Puerta Patio", "se está cerrando"))
        self.assertEqual(describe_position("Puerta Principal", "on", "door"), ("Puerta Principal", ""))
        self.assertEqual(describe_position("Movimiento abierto", "on", "motion"), ("Movimiento abierto", ""))
        self.assertEqual(describe_position("Cerrada", "on", "door"), ("Cerrada", ""))
