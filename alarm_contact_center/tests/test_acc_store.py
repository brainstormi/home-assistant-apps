import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store import (  # noqa: E402
    PinStatus,
    Store,
    ValidationError,
    clean_contact,
    clean_settings,
    contact_warnings,
    normalize_phone,
)


def external(**kw):
    return {"kind": "external", "name": "Pablo", "phone": "600111222", **kw}


def person(**kw):
    return {"kind": "person", "person": "person.luis", "name": "Luis", "phone": "600111333", **kw}


class PhoneTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_phone("+34 600 11 12 22"), "600111222")
        self.assertEqual(normalize_phone("0034600111222"), "600111222")
        self.assertEqual(normalize_phone("600-111-222"), "600111222")

    def test_invalid_phone(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(external(phone="12345"), PinStatus(), [])
        self.assertIn("phone", ctx.exception.errors)


class ContactTests(unittest.TestCase):
    def test_external_needs_name(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(external(name=""), PinStatus(), [])
        self.assertIn("name", ctx.exception.errors)

    def test_external_cannot_manage(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(external(can_manage=True), PinStatus("global"), [])
        self.assertIn("can_manage", ctx.exception.errors)

    def test_external_drops_person_and_app(self):
        c = clean_contact(external(person="person.luis", notify_service="mobile_app_x"), PinStatus(), [])
        self.assertEqual(c["person"], "")
        self.assertEqual(c["notify_service"], "")

    def test_person_requires_entity(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(person(person="luis"), PinStatus(), [])
        self.assertIn("person", ctx.exception.errors)

    def test_person_not_duplicated(self):
        others = [clean_contact(person(), PinStatus(), [])]
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(person(), PinStatus(), others)
        self.assertIn("person", ctx.exception.errors)

    def test_manage_individual_without_pin_rejected(self):
        pins = PinStatus("individual", ["person.marta"])
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(person(can_manage=True), pins, [])
        self.assertIn("can_manage", ctx.exception.errors)

    def test_manage_individual_with_pin_ok(self):
        pins = PinStatus("individual", ["person.luis"])
        c = clean_contact(person(can_manage=True), pins, [])
        self.assertTrue(c["can_manage"])

    def test_manage_global_ok(self):
        c = clean_contact(person(can_manage=True), PinStatus("global"), [])
        self.assertTrue(c["can_manage"])

    def test_manage_unknown_allowed_with_warning(self):
        c = clean_contact(person(can_manage=True), PinStatus(), [])
        self.assertTrue(any("No se puede comprobar" in w for w in contact_warnings(c, PinStatus())))

    def test_manage_needs_phone(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(person(phone="", can_manage=True), PinStatus("global"), [])
        self.assertIn("can_manage", ctx.exception.errors)

    def test_ranges(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_contact(external(attempts=9, retry_wait=1), PinStatus(), [])
        self.assertIn("attempts", ctx.exception.errors)
        self.assertIn("retry_wait", ctx.exception.errors)

    def test_no_channels_warning(self):
        c = clean_contact(external(phone=""), PinStatus(), [])
        self.assertTrue(contact_warnings(c, PinStatus()))


class SettingsTests(unittest.TestCase):
    def test_defaults_valid(self):
        s = clean_settings({})
        self.assertEqual(s["rounds"], 2)

    def test_bad_values(self):
        with self.assertRaises(ValidationError) as ctx:
            clean_settings({"message": "", "rounds": 11, "incident_entities": ["no valido"]})
        for key in ("message", "rounds", "incident_entities"):
            self.assertIn(key, ctx.exception.errors)

    def test_delays(self):
        s = clean_settings({})
        self.assertEqual((s["call_delay"], s["external_notify_delay"]), (30, 30))
        with self.assertRaises(ValidationError) as ctx:
            clean_settings({"call_delay": -1, "external_notify_delay": 601})
        self.assertIn("call_delay", ctx.exception.errors)
        self.assertIn("external_notify_delay", ctx.exception.errors)

    def test_voicemail_settings(self):
        s = clean_settings({})
        self.assertTrue(s["detect_voicemail"])
        self.assertIn("{nombre}", s["voicemail_message"])
        with self.assertRaises(ValidationError):
            clean_settings({"voicemail_message": "x" * 301})

    def test_entities_deduplicated(self):
        s = clean_settings({"incident_entities": ["binary_sensor.a", "binary_sensor.a", " "]})
        self.assertEqual(s["incident_entities"], ["binary_sensor.a"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "contact_center.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_persist_and_reorder(self):
        store = Store(self.path)
        a = store.save_contact(external(name="A"), PinStatus())
        b = store.save_contact(external(name="B", phone="600111999"), PinStatus())
        store.reorder([b["id"], a["id"]])
        again = Store(self.path)
        self.assertEqual([c["name"] for c in again.contacts()], ["B", "A"])

    def test_reorder_must_match(self):
        store = Store(self.path)
        store.save_contact(external(), PinStatus())
        with self.assertRaises(ValidationError):
            store.reorder(["otro"])

    def test_update_unknown_id(self):
        store = Store(self.path)
        with self.assertRaises(KeyError):
            store.save_contact(external(id="nope"), PinStatus())

    def test_delete(self):
        store = Store(self.path)
        a = store.save_contact(external(), PinStatus())
        store.delete_contact(a["id"])
        self.assertEqual(store.contacts(), [])

    def test_damaged_file_is_kept(self):
        self.path.write_text("{roto", encoding="utf-8")
        store = Store(self.path)
        self.assertEqual(store.contacts(), [])
        self.assertTrue(self.path.with_suffix(".damaged.json").exists())

    def test_settings_saved(self):
        store = Store(self.path)
        store.save_settings({"rounds": 3})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["settings"]["rounds"], 3)


class BridgeAddonTests(unittest.TestCase):
    def test_derived_from_hostname(self):
        from app.store import bridge_addon, default_bridge_addon
        self.assertEqual(default_bridge_addon("abcd1234-alarm-contact-center"), "abcd1234_asterisk_realtime_bridge")
        self.assertEqual(default_bridge_addon("local-alarm-contact-center"), "local_asterisk_realtime_bridge")
        self.assertEqual(default_bridge_addon(""), "")
        self.assertEqual(default_bridge_addon("laptop"), "")
        self.assertEqual(bridge_addon({"bridge_addon": "zz99_asterisk_realtime_bridge"}), "zz99_asterisk_realtime_bridge")

    def test_empty_setting_is_valid(self):
        self.assertEqual(clean_settings({"bridge_addon": ""})["bridge_addon"], "")
        with self.assertRaises(ValidationError):
            clean_settings({"bridge_addon": "Not Valid"})


class PinEntityTests(unittest.TestCase):
    def test_bridge_format(self):
        from app.ha import HomeAssistant
        states = [{"entity_id": "sensor.asterisk_realtime_bridge_pin", "state": "individual", "attributes": {
            "complete": False, "management": "disabled (query only)",
            "issues": ["person.luis: the PIN must have at least 6 digits"],
            "people_with_pin": ["person.marta"]}}]
        pins = HomeAssistant(token="").pin_status("", states)
        self.assertEqual(pins.mode, "individual")
        self.assertTrue(pins.allows("person.marta"))
        self.assertFalse(pins.allows("person.luis"))
        self.assertFalse(pins.complete)
        self.assertEqual(len(pins.issues), 1)

    def test_missing_entity_is_unknown(self):
        from app.ha import HomeAssistant
        self.assertEqual(HomeAssistant(token="").pin_status("", []).mode, "unknown")


if __name__ == "__main__":
    unittest.main()
