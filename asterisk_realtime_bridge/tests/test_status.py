import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.status import HomeStatus, Registries, describe, parse_list, select_entities  # noqa: E402

REG = Registries(
    entities=[
        {"entity_id": "input_datetime.ultima_persona_garaje", "labels": ["camaras"], "device_id": None},
        {"entity_id": "input_text.evento_persona_garaje", "labels": ["camaras"], "device_id": None},
        {"entity_id": "switch.puerta_garaje", "labels": ["puerta_garaje"], "device_id": "dev-garage"},
        {"entity_id": "binary_sensor.fotocelula", "labels": [], "device_id": "dev-garage"},
        {"entity_id": "light.luz_sofa", "labels": ["luces"], "device_id": "dev-sofa"},
        {"entity_id": "lock.entrada", "labels": [], "device_id": "dev-lock"},
    ],
    devices=[
        {"id": "dev-garage", "name": "shelly1g3-48f6", "name_by_user": "Puerta Garaje", "labels": ["alarma"]},
        {"id": "dev-sofa", "name": "Switch Luz Sofa", "name_by_user": None, "labels": []},
        {"id": "dev-lock", "name": "Cerradura", "name_by_user": None, "labels": []},
    ],
    labels=[
        {"label_id": "camaras", "name": "Cámaras"},
        {"label_id": "puerta_garaje", "name": "Puerta Garaje"},
        {"label_id": "alarma", "name": "Seguridad"},
        {"label_id": "luces", "name": "Luces"},
    ],
    exposed={"light.luz_sofa", "input_datetime.ultima_persona_garaje", "switch.puerta_garaje", "sensor.gone"},
)
EXISTING = {e["entity_id"] for e in REG.entities}


class SelectTests(unittest.TestCase):
    def test_empty_include_is_the_assist_scope(self):
        self.assertEqual(
            select_entities([], [], REG, EXISTING),
            ["input_datetime.ultima_persona_garaje", "light.luz_sofa", "switch.puerta_garaje"],
            "exposed entities that exist; the lock is not exposed, sensor.gone has no state",
        )

    def test_include_by_label_name_or_id_case_and_accent_insensitive(self):
        self.assertEqual(
            select_entities(["camaras"], [], REG, EXISTING),
            select_entities([" CÁMARAS "], [], REG, EXISTING),
        )
        self.assertEqual(
            select_entities(["Cámaras"], [], REG, EXISTING),
            ["input_datetime.ultima_persona_garaje", "input_text.evento_persona_garaje"],
        )

    def test_device_labels_count(self):
        # "Seguridad" is on the garage device: both of its entities are included.
        self.assertEqual(select_entities(["Seguridad"], [], REG, EXISTING),
                         ["binary_sensor.fotocelula", "switch.puerta_garaje"])

    def test_unknown_labels_select_nothing(self):
        self.assertEqual(select_entities(["nope"], [], REG, EXISTING), [])

    def test_excludes_by_entity_label_and_device(self):
        both = ["Cámaras", "Seguridad"]
        self.assertNotIn("input_text.evento_persona_garaje",
                         select_entities(both, ["input_text.evento_persona_garaje"], REG, EXISTING))
        self.assertEqual(select_entities(both, ["camaras"], REG, EXISTING),
                         ["binary_sensor.fotocelula", "switch.puerta_garaje"])
        self.assertEqual(select_entities(both, ["Puerta Garaje"], REG, EXISTING),
                         ["input_datetime.ultima_persona_garaje", "input_text.evento_persona_garaje"],
                         "'Puerta Garaje' is a label and a device name: both are excluded")
        self.assertEqual(select_entities([], ["Switch Luz Sofa"], REG, EXISTING),
                         ["input_datetime.ultima_persona_garaje", "switch.puerta_garaje"])

    def test_exclude_can_never_add(self):
        self.assertNotIn("lock.entrada", select_entities([], ["lock.entrada", "Cerradura"], REG, EXISTING))

    def test_parse_list(self):
        self.assertEqual(parse_list(" a, b ,,c "), ["a", "b", "c"])
        self.assertEqual(parse_list(""), [])


class DescribeTests(unittest.TestCase):
    def test_local_times_and_ages(self):
        tz = timezone(timedelta(hours=1))
        now = datetime(2026, 11, 21, 17, 0, tzinfo=tz)
        states = [
            {"entity_id": "binary_sensor.fotocelula", "state": "off",
             "last_changed": "2026-11-21T15:30:00+00:00",
             "attributes": {"friendly_name": "Fotocélula", "device_class": "opening"}},
            {"entity_id": "sensor.temp", "state": "21.5", "last_changed": "2026-11-21T15:59:30+00:00",
             "attributes": {"friendly_name": "Temperatura garaje", "unit_of_measurement": "°C"}},
        ]
        result = describe(states, now)
        self.assertEqual(result["now"], "2026-11-21 17:00")
        foto, temp = result["entities"]
        self.assertEqual(foto["since"], "2026-11-21 16:30 (30 min ago)")
        self.assertEqual(foto["device_class"], "opening")
        self.assertEqual(temp["unit"], "°C")
        self.assertEqual(temp["since"], "2026-11-21 16:59 (less than a minute ago)")

    def test_search_is_accent_insensitive(self):
        now = datetime(2026, 11, 21, 17, 0, tzinfo=timezone.utc)
        states = [{"entity_id": "binary_sensor.fotocelula", "state": "off",
                   "attributes": {"friendly_name": "Fotocélula"}},
                  {"entity_id": "light.sofa", "state": "on", "attributes": {"friendly_name": "Luz sofá"}}]
        self.assertEqual([e["entity_id"] for e in describe(states, now, "fotocelula")["entities"]],
                         ["binary_sensor.fotocelula"])
        self.assertIn("note", describe(states, now, "piscina"))


class FakeHA:
    def __init__(self):
        self.ws_calls = []

    async def ws(self, command, **kwargs):
        self.ws_calls.append(command)
        return {
            "config/entity_registry/list": REG.entities,
            "config/device_registry/list": REG.devices,
            "config/label_registry/list": REG.labels,
            "homeassistant/expose_entity/list": {"exposed_entities": {
                "light.luz_sofa": {"conversation": True, "cloud.alexa": True},
                "lock.entrada": {"conversation": False, "cloud.alexa": True},
            }},
        }[command]

    async def get(self, path):
        assert path == "states"
        return [{"entity_id": e, "state": "on", "attributes": {}} for e in EXISTING]

    async def render(self, template):
        return "2026-11-21T17:00:00+01:00"


class HomeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_uses_assist_exposure_and_caches_registries(self):
        ha = FakeHA()
        status = HomeStatus(ha, "", "")
        result = await status.snapshot()
        self.assertEqual([e["entity_id"] for e in result["entities"]], ["light.luz_sofa"],
                         "only entities exposed to the conversation assistant")
        await status.snapshot()
        self.assertEqual(ha.ws_calls.count("config/entity_registry/list"), 1, "registries are cached")
        self.assertEqual(status.scope, "entities exposed to Assist")

    async def test_snapshot_with_labels_skips_the_exposure_list(self):
        ha = FakeHA()
        status = HomeStatus(ha, "Cámaras", "input_text.evento_persona_garaje")
        result = await status.snapshot()
        self.assertEqual([e["entity_id"] for e in result["entities"]],
                         ["input_datetime.ultima_persona_garaje"])
        self.assertNotIn("homeassistant/expose_entity/list", ha.ws_calls)


if __name__ == "__main__":
    unittest.main()
