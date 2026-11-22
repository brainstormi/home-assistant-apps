import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.dialplan import caller_number, dial_report  # noqa: E402
from app.main import report_pin_mode  # noqa: E402
from app.config import Config  # noqa: E402
from app.outbound import OutboundCall  # noqa: E402
from app.pins import PinBook, PinEntry, pin_problem, same_number  # noqa: E402

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"


class PinRuleTests(unittest.TestCase):
    def test_trivial_pins_are_rejected(self):
        for pin in ("", "12a456", "48291", "000000", "123456", "654321", "890123", "121212",
                    "123123", "4545454545"):
            self.assertIsNotNone(pin_problem(pin), pin)
        for pin in ("482916", "739258", "1029384756"):
            self.assertIsNone(pin_problem(pin), pin)

    def test_numbers(self):
        self.assertTrue(same_number("600111222", "+34 600 111 222"))
        self.assertTrue(same_number("0034600111222", "34600111222"))
        self.assertFalse(same_number("600111222", "600111223"))
        self.assertFalse(same_number("111222", "600111222"), "short numbers must match exactly")
        self.assertFalse(same_number("", "600111222"))


class PinBookTests(unittest.TestCase):
    def book(self, *entries, mode="individual", pin=""):
        return PinBook.build(True, mode, pin, entries)

    def test_outbound_profiles(self):
        book = self.book(PinEntry(person="person.sonia", pin="482916"))
        manage = OutboundCall(CALL_ID, "m", "Management", person="person.sonia")
        auth = book.for_outbound(manage)
        self.assertEqual((auth.state, auth.profile, auth.pins), ("pin", "Management", (("person.sonia", "482916"),)))
        other = book.for_outbound(OutboundCall(CALL_ID, "m", "Management", person="person.luis"))
        self.assertEqual((other.state, other.profile), ("locked", "QueryOnly"))
        self.assertIn("person.luis", other.reason)
        self.assertEqual(book.for_outbound(OutboundCall(CALL_ID, "m", "QueryOnly")).state, "locked")

    def test_person_by_friendly_name(self):
        book = self.book(PinEntry(person="person.luis_g", pin="482916"))
        book.learn_people([{"entity_id": "person.luis_g", "attributes": {"friendly_name": "Luís"}}])
        self.assertIsNotNone(book.entry_for("luís"))
        self.assertIsNone(book.entry_for("Sonia"))

    def test_unknown_person_makes_the_mode_incomplete(self):
        book = self.book(PinEntry(person="person.sonia", pin="482916"), PinEntry(person="person.luiss", pin="739258"))
        self.assertTrue(book.complete, "persons not read yet: no verdict")
        book.learn_people([{"entity_id": "person.sonia"}, {"entity_id": "person.luis"}])
        self.assertEqual(book.unknown, ("person.luiss",))
        self.assertFalse(book.complete)
        self.assertIn("person.luiss: no such person in Home Assistant", book.status()[1]["issues"])
        book.learn_people([])  # Home Assistant unreachable: keep the last verdict
        self.assertFalse(book.complete)

    def test_manifest_checks_the_person_format(self):
        import re
        manifest = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml"), encoding="utf-8").read()
        pattern = re.search(r"person: match\((.+)\)\?", manifest).group(1)
        self.assertTrue(re.match(pattern, "person.luis_garcia"))
        self.assertFalse(re.match(pattern, "Luis"))
        self.assertFalse(re.match(pattern, "person.Luis"))

    def test_status_never_shows_pins(self):
        book = self.book(PinEntry(person="person.sonia", pin="482916", numbers=("600111222",)),
                         PinEntry(person="person.luis", pin="111111"))
        state, attributes = book.status(["person.sonia", "person.luis", "person.abuela"])
        self.assertEqual(state, "individual")
        self.assertFalse(attributes["complete"])
        self.assertEqual(attributes["people_with_pin"], ["person.sonia"])
        self.assertEqual(attributes["people_without_pin"], ["person.abuela", "person.luis"])
        self.assertEqual(attributes["people_with_number"], ["person.sonia"])
        self.assertNotIn("482916", json.dumps(attributes))
        self.assertNotIn("111111", json.dumps(attributes))
        state, attributes = self.book(mode="global", pin="1111").status()
        self.assertEqual((state, attributes["global_pin"]), ("global", "set"))
        self.assertNotIn("1111", json.dumps(attributes))

    def test_mode_change_is_logged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "pin_mode")
            report_pin_mode(Config(openai_api_key="k", pin="1111"), path)
            with self.assertLogs("bridge", "WARNING") as logs:
                report_pin_mode(Config(openai_api_key="k", pin_mode="individual"), path)
            text = "\n".join(logs.output)
            self.assertIn("changed from global to individual", text)
            self.assertIn("NOT complete", text)


class DialplanTests(unittest.TestCase):
    def test_caller_number(self):
        self.assertEqual(caller_number("ca11e009-1234-0000-0000-000600111222"), "600111222")
        self.assertEqual(caller_number("ca11e011-0042-0000-0000-034600111222"), "34600111222")
        self.assertEqual(caller_number("ca11e000-0042-0000-0000-000000000000"), "")
        self.assertIsNone(caller_number(CALL_ID))
        self.assertIsNone(caller_number("ca11e009-1234-0000-0000-00060011122f"))

    def test_dial_report(self):
        self.assertEqual(dial_report("fa110002" + CALL_ID[8:]), ("BUSY", CALL_ID[8:]))
        self.assertEqual(dial_report("fa110000" + CALL_ID[8:])[0], "UNKNOWN")
        self.assertIsNone(dial_report(CALL_ID))
