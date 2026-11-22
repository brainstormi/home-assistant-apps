import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
from app.audiosocket import KIND_DTMF, pack_frame  # noqa: E402
from app.config import Config  # noqa: E402
from app.outbound import (  # noqa: E402
    CommandError, CommandParser, OutboundCall, Registry, parse_command, read_commands,
)
from app.session import CallSession, build_session_update  # noqa: E402

from tests.test_session import FakeHome, FakeWriter, FakeWS, call  # noqa: E402

CALL_ID = "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"


class FakeStatus:
    def __init__(self):
        self.searches = []
        self.extras = []

    async def snapshot(self, search="", extra=()):
        self.searches.append(search)
        self.extras.append(extra)
        return {"now": "2026-11-21 17:00", "entities": [
            {"name": "Puerta garaje", "entity_id": "binary_sensor.garaje", "state": "off",
             "device_class": "garage_door"}]}


# --------------------------------------------------------------- config
class PinConfigTests(unittest.TestCase):
    def _load(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(data, fh)
        try:
            return config.load(fh.name)
        finally:
            os.unlink(fh.name)

    def test_individual_pins_are_loaded(self):
        cfg = self._load({"openai_api_key": "k", "pin_mode": "individual", "pins": [
            {"person": "person.sonia", "pin": "482916", "numbers": "600 111 222, +34 600333444"}]})
        book = cfg.pin_book
        self.assertEqual((book.mode, book.complete), ("individual", True))
        self.assertEqual(book.entries[0].numbers, ("600111222", "34600333444"))

    def test_global_mode_ignores_personal_pins(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1111",
                          "pins": [{"person": "person.sonia", "pin": "482916"}]})
        book = cfg.pin_book
        self.assertEqual((book.mode, book.complete, book.entries), ("global", True, ()))
        self.assertTrue(book.ignored)

    def test_invalid_pins_make_the_mode_incomplete_without_stopping(self):
        bad = [
            [{"name": "Sonia", "pin": "482916"}],  # no person entity
            [{"person": "person.sonia", "pin": "48a916"}],
            [{"person": "person.sonia", "pin": "48291"}],  # too short
            [{"person": "person.sonia", "pin": "123456"}],
            [{"person": "person.sonia", "pin": "777777"}],
            [{"person": "person.sonia", "pin": "121212"}],
            [{"person": "person.sonia", "pin": "482916"}, {"person": "person.luis", "pin": "482916"}],
            [{"person": "person.sonia", "pin": "482916"}, {"person": "person.sonia", "pin": "739258"}],
            [],
        ]
        for pins in bad:
            cfg = self._load({"openai_api_key": "k", "pin_mode": "individual", "pins": pins})
            self.assertFalse(cfg.pin_book.complete, pins)
            self.assertTrue(cfg.pin_book.issues, pins)
            self.assertFalse(any("482916" in i or "123456" in i for i in cfg.pin_book.issues),
                             "issues never include a PIN")
        with self.assertRaises(config.ConfigError):
            self._load({"openai_api_key": "k", "pins": "1234"})
        with self.assertRaises(config.ConfigError):
            self._load({"openai_api_key": "k", "pin_mode": "both"})

    def test_pins_ignored_when_pin_disabled(self):
        cfg = self._load({"openai_api_key": "k", "pin_enabled": False,
                          "pins": [{"person": "person.sonia", "pin": "482916"}]})
        self.assertEqual(cfg.pin_book.mode, "off")

    def test_query_scope_options(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1"})
        self.assertEqual((cfg.query_include, cfg.query_exclude), ("", ""))
        cfg = self._load({"openai_api_key": "k", "pin": "1", "query_include": "Cámaras, Seguridad",
                          "query_exclude": "lock.entrada"})
        self.assertEqual((cfg.query_include, cfg.query_exclude), ("Cámaras, Seguridad", "lock.entrada"))

    def test_status_reading_rules_are_in_outbound_calls_only(self):
        cfg = Config(openai_api_key="k", pin="1234")
        self.assertIn("year 2000", cfg.instructions_for(OutboundCall(CALL_ID, "m", "QueryOnly"), "locked"))
        self.assertNotIn("home_status", cfg.instructions)

    def test_outbound_instructions(self):
        cfg = Config(openai_api_key="k", pin="1234")
        inbound = cfg.instructions_for(None, "pin")
        self.assertEqual(inbound, cfg.instructions)
        self.assertIn("Before doing anything else", inbound)

        query = cfg.instructions_for(OutboundCall(CALL_ID, "m", "QueryOnly"), "locked")
        self.assertNotIn("Before doing anything else", query)
        self.assertIn("OUTBOUND", query)
        self.assertIn("no PIN", query)
        self.assertIn("Do not ask for an explicit confirmation", query)

        manage = cfg.instructions_for(OutboundCall(CALL_ID, "m", "Management"), "pin")
        self.assertIn("verify_pin", manage)
        self.assertNotIn("Before doing anything else", manage)

        open_manage = Config(openai_api_key="k", pin_enabled=False).instructions_for(
            OutboundCall(CALL_ID, "m", "Management"), "open")
        self.assertNotIn("verify_pin", open_manage)

    def test_confirmation_rules_follow_the_call(self):
        cfg = Config(openai_api_key="k", pin="1234")
        call_ = OutboundCall(CALL_ID, "m", "QueryOnly", confirm="voice_or_dtmf", confirm_key="#",
                             confirm_text="If you confirm, nobody else will be called.")
        text = cfg.instructions_for(call_, "locked")
        self.assertIn("hash (#)", text)
        self.assertIn("If you confirm, nobody else will be called.", text)
        dtmf = cfg.instructions_for(OutboundCall(CALL_ID, "m", "QueryOnly", confirm="dtmf"), "locked")
        self.assertIn("Only the key counts", dtmf)

    def test_notice_is_part_of_the_session_instructions(self):
        # If the greeting is interrupted, the per-response instructions are lost:
        # the notice must also be in the session instructions.
        cfg = Config(openai_api_key="k", pin="1234")
        call_ = OutboundCall(CALL_ID, "The garage door opened at 08:57.", "Management")
        self.assertIn('"The garage door opened at 08:57."', cfg.instructions_for(call_, "pin"))
        self.assertNotIn("The notice to deliver", cfg.instructions)

    def test_assistant_name_per_call(self):
        cfg = Config(openai_api_key="k", pin="1234", assistant_name="Jarvis")
        self.assertIn("You are Jarvis", cfg.instructions)
        call_ = OutboundCall(CALL_ID, "m", "QueryOnly", assistant_name="the home alarm service")
        self.assertIn("You are the home alarm service", cfg.instructions_for(call_, "locked"))
        self.assertIn("You are Jarvis", cfg.instructions_for(OutboundCall(CALL_ID, "m", "QueryOnly"), "locked"))

    def test_outbound_rules_are_added_to_a_custom_prompt(self):
        cfg = Config(openai_api_key="k", pin="1234", prompt="Short custom prompt.")
        self.assertIn("confirm_notice", cfg.instructions_for(OutboundCall(CALL_ID, "m", "QueryOnly"), "locked"))
        self.assertEqual(cfg.instructions, "Short custom prompt.\n")


# ------------------------------------------------------------- commands
class CommandTests(unittest.TestCase):
    def test_parse_valid_command(self):
        cmd = parse_command({"action": "outbound", "call_id": CALL_ID.upper(), "message": " Alarm ",
                             "profile": "management", "person": "Sonia", "timeout": "45",
                             "confirm": "Voice_or_DTMF", "confirm_key": "#",
                             "confirm_text": "Nobody else will be called.",
                             "assistant_name": "the alarm service"})
        self.assertEqual(cmd.call_id, CALL_ID)
        self.assertEqual(cmd.message, "Alarm")
        self.assertEqual(cmd.profile, "Management")
        self.assertEqual(cmd.person, "Sonia")
        self.assertEqual(cmd.timeout, 45)
        self.assertEqual((cmd.confirm, cmd.confirm_key), ("voice_or_dtmf", "#"))
        self.assertEqual(cmd.confirm_text, "Nobody else will be called.")
        self.assertEqual(cmd.assistant_name, "the alarm service")

    def test_parse_defaults_and_limits(self):
        cmd = parse_command({"action": "outbound", "call_id": CALL_ID, "message": "x",
                             "profile": "QueryOnly", "timeout": 99999})
        self.assertEqual(cmd.timeout, 600)
        self.assertEqual(cmd.person, "")
        self.assertEqual((cmd.confirm, cmd.confirm_key, cmd.confirm_text, cmd.assistant_name, cmd.engine),
                         ("implicit", "1", "", "", "realtime"))
        self.assertEqual(parse_command({"action": "outbound", "call_id": CALL_ID, "message": "x",
                                        "profile": "QueryOnly", "engine": "LOCAL"}).engine, "local")

    def test_parse_invalid_commands(self):
        base = {"action": "outbound", "call_id": CALL_ID, "message": "x", "profile": "Management"}
        for change in ({"action": "dance"}, {"call_id": "not-a-uuid"}, {"message": ""},
                       {"profile": "admin"}, {"profile": "gestion"}, {"timeout": "soon"},
                       {"confirm": "maybe"}, {"confirm_key": "12"}, {"confirm_key": "A"}, {"engine": "gpt"}):
            with self.assertRaises(CommandError, msg=change):
                parse_command({**base, **change})
        with self.assertRaises(CommandError):
            parse_command(["outbound"])

    def test_parser_handles_objects_strings_and_split_chunks(self):
        parser = CommandParser()
        obj = {"action": "outbound", "call_id": CALL_ID}
        # An object, as hassio.addon_stdin sends a mapping.
        self.assertEqual(parser.feed(json.dumps(obj) + "\n"), [obj])
        # A JSON string containing the command (input given as text).
        self.assertEqual(parser.feed(json.dumps(json.dumps(obj))), [obj])
        # Split across reads, without a trailing newline.
        text = json.dumps(obj)
        self.assertEqual(parser.feed(text[:10]), [])
        self.assertEqual(parser.feed(text[10:]), [obj])
        # Two commands in one read.
        self.assertEqual(parser.feed(text + text), [obj, obj])

    def test_parser_skips_garbage_lines(self):
        parser = CommandParser()
        obj = {"a": 1}
        self.assertEqual(parser.feed("hello there\n" + json.dumps(obj)), [obj])
        self.assertEqual(parser.feed('"not json inside"\n'), [])


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_take_claims_the_call_once(self):
        registry = Registry()
        registry.add(OutboundCall(CALL_ID, "msg", "Management"))
        self.assertEqual(registry.take(CALL_ID).message, "msg")
        self.assertIsNone(registry.take(CALL_ID))
        await registry.close()

    async def test_unanswered_call_expires_and_is_reported(self):
        missed = []

        async def on_expired(c):
            missed.append(c.call_id)

        registry = Registry(on_expired=on_expired)
        call_ = OutboundCall(CALL_ID, "msg", "Management", timeout=10)
        call_.timeout = 0.05  # shorter than the parser allows, for the test
        registry.add(call_)
        await asyncio.sleep(0.12)
        self.assertEqual(missed, [CALL_ID])
        self.assertIsNone(registry.take(CALL_ID))
        await registry.close()

    async def test_read_commands_from_a_stream(self):
        reader = asyncio.StreamReader()
        got = []

        async def handle(cmd):
            got.append(cmd)
            if cmd.get("boom"):
                raise RuntimeError("bad command")

        reader.feed_data(b'{"boom": true}\n{"ok": 1}\n')
        reader.feed_eof()
        await asyncio.wait_for(read_commands(reader, handle), 1)
        self.assertEqual(got, [{"boom": True}, {"ok": 1}])


# -------------------------------------------------------------- session
class OutboundSessionTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, profile, cfg=None, status=True, message="The garage door opened at 08:57.",
                    person="Sonia", **call_options):
        if getattr(self, "task", None):  # a second call in the same test
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.cfg = cfg or Config(openai_api_key="k", pin_mode="individual", assistant_name="Jarvis",
                                 pins=(("person.sonia", "482916"), ("person.luis", "739258")),
                                 max_call_seconds=5)
        self.ws, self.writer = FakeWS(), FakeWriter()
        self.home, self.status = FakeHome(), (FakeStatus() if status else None)
        self.reader = asyncio.StreamReader()
        outbound = OutboundCall(CALL_ID, message, profile, person=person, **call_options)
        self.session = CallSession(self.cfg, "out", self.reader, self.writer, self.ws, self.home,
                                   outbound=outbound, status=self.status)
        self.task = asyncio.create_task(self.session.run())
        await asyncio.sleep(0.01)
        self.ws.push({"type": "session.updated"})
        await asyncio.sleep(0.02)

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def tool_names(self):
        return [t["name"] for t in self.ws.sent[0]["session"]["tools"]]

    def greeting(self):
        return self.ws.of_type("response.create")[0]["response"]["instructions"]

    def notes(self):
        return [e["item"]["content"][0]["text"] for e in self.ws.of_type("conversation.item.create")
                if e["item"]["type"] == "message"]

    async def tool(self, name, args, call_id):
        self.ws.push(call(name, args, call_id))
        await asyncio.sleep(0.05)
        return self.ws.tool_outputs()[-1]

    async def keys(self, digits, pause=0.0):
        for digit in digits:
            self.reader.feed_data(pack_frame(KIND_DTMF, digit.encode()))
            await asyncio.sleep(pause or 0.01)
        await asyncio.sleep(0.03)

    # ---- greeting and name
    async def test_greeting_reads_the_notice_and_uses_the_assistant_name(self):
        await self.start("QueryOnly")
        hint = self.greeting()
        self.assertIn("The garage door opened at 08:57.", hint)
        self.assertIn("as Jarvis", hint)
        self.assertNotIn("PIN", hint)
        self.assertTrue(self.session.connected)

    async def test_assistant_name_per_call(self):
        await self.start("QueryOnly", assistant_name="the alarm service of Sonia's home")
        self.assertIn("as the alarm service of Sonia's home", self.greeting())
        self.assertIn("You are the alarm service of Sonia's home", self.ws.sent[0]["session"]["instructions"])

    # ---- QueryOnly
    async def test_query_only_reads_and_has_no_pin(self):
        await self.start("QueryOnly")
        self.assertEqual(self.tool_names(), ["home_status", "hang_up", "confirm_notice"],
                         "the tool that can act is not even offered")
        self.assertIn("OUTBOUND", self.ws.sent[0]["session"]["instructions"])

        out = await self.tool("home_status", {"search": "garaje"}, "q1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["entities"][0]["state"], "off")
        self.assertEqual(self.status.searches, ["garaje"])

        # Asking the acting tool anyway (the model should not, but code decides).
        out = await self.tool("home", {"request": "open the garage door"}, "a1")
        self.assertFalse(out["ok"])
        self.assertEqual(self.home.calls, [], "the full agent is never used")

        # Even the right PIN does not unlock anything in this profile.
        self.assertFalse((await self.tool("verify_pin", {"pin": "1111"}, "p1"))["ok"])
        await self.keys("1111")
        await self.tool("home", {"request": "open the garage door"}, "a2")
        self.assertEqual(self.home.calls, [])
        self.assertIsNone(self.session.pin_person)

    async def test_status_unavailable(self):
        await self.start("QueryOnly", status=False)
        out = await self.tool("home_status", {}, "q1")
        self.assertFalse(out["ok"])

    async def test_status_errors_are_reported(self):
        await self.start("QueryOnly")

        async def broken(search=""):
            raise ConnectionError("down")

        self.status.snapshot = broken
        out = await self.tool("home_status", {}, "q1")
        self.assertEqual(out, {"ok": False, "error": "Could not read the state of the home."})

    # ---- Management and PIN scope
    async def test_management_reads_first_and_acts_after_the_called_persons_pin(self):
        await self.start("Management")
        self.assertEqual(self.tool_names(), ["verify_pin", "home_status", "home", "hang_up", "confirm_notice"])

        self.assertTrue((await self.tool("home_status", {}, "q1"))["ok"])
        out = await self.tool("home", {"request": "close the garage door"}, "a0")
        self.assertFalse(out["ok"])
        self.assertIn("PIN", out["error"])
        self.assertEqual(self.home.calls, [])

        # Luis's personal PIN is not valid in a call to Sonia.
        self.assertFalse((await self.tool("verify_pin", {"pin": "739258"}, "p1"))["ok"])
        self.assertTrue((await self.tool("verify_pin", {"pin": "cuatro ocho dos nueve uno seis"}, "p2"))["ok"])
        self.assertEqual(self.session.pin_person, "person.sonia")

        out = await self.tool("home", {"request": "close the garage door"}, "a1")
        self.assertTrue(out["ok"])
        self.assertEqual(self.home.calls, ["close the garage door"])

    async def test_management_accepts_the_global_pin_by_keypad(self):
        await self.start("Management", cfg=Config(openai_api_key="k", pin="1111", max_call_seconds=5,
                                                  pins=(("person.sonia", "482916"),)))
        await self.keys("482916")
        self.assertIsNone(self.session.pin_person, "global mode: personal PINs are ignored")
        await self.keys("1111")
        self.assertEqual(self.session.pin_person, "")
        await self.tool("home", {"request": "close the garage door"}, "a1")
        self.assertEqual(self.home.calls, ["close the garage door"])

    async def test_person_is_matched_case_insensitively(self):
        await self.start("Management", person="sonia")
        self.assertTrue((await self.tool("verify_pin", {"pin": "482916"}, "p1"))["ok"])
        await self.start("Management", person="person.luis")
        self.assertTrue((await self.tool("verify_pin", {"pin": "739258"}, "p1"))["ok"])

    async def test_management_is_locked_without_an_applicable_pin(self):
        cfg = Config(openai_api_key="k", pin_mode="individual", pins=(("person.luis", "739258"),),
                     max_call_seconds=5)
        await self.start("Management", cfg=cfg, person="Grandma")
        self.assertEqual(self.session.auth.profile, "QueryOnly")
        self.assertEqual(self.tool_names(), ["home_status", "hang_up", "confirm_notice"])
        self.assertFalse((await self.tool("verify_pin", {"pin": "739258"}, "p1"))["ok"])
        await self.tool("home", {"request": "close the garage door"}, "a1")
        self.assertEqual(self.home.calls, [])

    async def test_management_is_query_only_while_the_mode_is_incomplete(self):
        cfg = Config(openai_api_key="k", pin_mode="individual", max_call_seconds=5,
                     pins=(("person.sonia", "482916"), ("person.luis", "123456")))
        await self.start("Management", cfg=cfg)
        self.assertIn("not complete", self.session.auth.reason)
        self.assertNotIn("home", self.tool_names())
        await self.keys("482916")
        self.assertIsNone(self.session.pin_person)

    async def test_management_without_any_pin_can_act(self):
        await self.start("Management", cfg=Config(openai_api_key="k", pin_enabled=False, max_call_seconds=5))
        self.assertEqual(self.tool_names(), ["home_status", "home", "hang_up", "confirm_notice"])
        await self.tool("home", {"request": "close the garage door"}, "a1")
        self.assertEqual(self.home.calls, ["close the garage door"])

    # ---- confirmation
    async def test_implicit_confirmation(self):
        await self.start("QueryOnly")
        self.assertIn("anything else", self.greeting())
        self.assertFalse(self.session.confirmed)
        self.assertTrue((await self.tool("confirm_notice", {}, "c1"))["ok"])
        self.assertEqual((self.session.confirmed, self.session.confirmed_by), (True, "implicit"))

    async def test_voice_confirmation_with_text(self):
        await self.start("QueryOnly", confirm="voice", confirm_text="Nobody else will be called.")
        self.assertIn("explicitly", self.greeting())
        self.assertIn("Nobody else will be called.", self.greeting())
        await self.tool("confirm_notice", {}, "c1")
        self.assertEqual(self.session.confirmed_by, "voice")
        # The key does nothing in voice mode.
        await self.start("QueryOnly", confirm="voice")
        await self.keys("1")
        self.assertFalse(self.session.confirmed)

    async def test_dtmf_confirmation_ignores_words(self):
        await self.start("QueryOnly", confirm="dtmf")
        self.assertNotIn("confirm_notice", self.tool_names())
        self.assertIn("press the 1 key", self.greeting())
        self.assertFalse((await self.tool("confirm_notice", {}, "c1"))["ok"])
        self.assertFalse(self.session.confirmed)
        await self.keys("2")
        self.assertFalse(self.session.confirmed)
        await self.keys("1")
        self.assertEqual((self.session.confirmed, self.session.confirmed_by), (True, "dtmf"))
        self.assertTrue(any("pressed the 1 key" in n for n in self.notes()))

    async def test_digit_key_waits_when_a_pin_can_be_typed(self):
        import app.session as session_module
        original = session_module.CONFIRM_KEY_WAIT_S
        session_module.CONFIRM_KEY_WAIT_S = 0.1
        try:
            # Sonia's PIN starts with the confirmation key: typing it must not confirm.
            await self.start("Management", confirm="voice_or_dtmf", confirm_key="4")
            await self.keys("482916")
            await asyncio.sleep(0.15)
            self.assertFalse(self.session.confirmed)
            self.assertEqual(self.session.pin_person, "person.sonia")
            # After the PIN, the key confirms at once (nothing else to type).
            await self.keys("4")
            self.assertEqual(self.session.confirmed_by, "dtmf")

            # A lone key confirms after the wait.
            await self.start("Management", confirm="dtmf", confirm_key="1")
            await self.keys("1")
            self.assertFalse(self.session.confirmed)
            await asyncio.sleep(0.15)
            self.assertTrue(self.session.confirmed)
            self.assertIsNone(self.session.pin_person)
        finally:
            session_module.CONFIRM_KEY_WAIT_S = original

    async def test_hash_key_confirms_at_once_even_with_pin_entry(self):
        await self.start("Management", confirm="dtmf", confirm_key="#")
        await self.keys("#")
        self.assertEqual(self.session.confirmed_by, "dtmf")


class InboundPinTests(unittest.IsolatedAsyncioTestCase):
    CFG = dict(openai_api_key="k", pin_mode="individual", assistant_name="Jarvis", max_call_seconds=5)

    def cfg(self):
        from app.pins import PinEntry
        return Config(**self.CFG, pins=(
            PinEntry(person="person.luis", pin="739258", numbers=("600111222",)),
            PinEntry(person="person.sonia", pin="482916", numbers=("34600333444",)),
        ))

    async def run_inbound(self, auth):
        ws, home = FakeWS(), FakeHome()
        session = CallSession(self.cfg(), "in", asyncio.StreamReader(), FakeWriter(), ws, home,
                              status=FakeStatus(), auth=auth)
        task = asyncio.create_task(session.run())
        await asyncio.sleep(0.01)
        ws.push({"type": "session.updated"})
        await asyncio.sleep(0.02)
        return session, ws, home, task

    async def test_inbound_from_a_known_number_asks_that_persons_pin(self):
        from app.session import call_auth
        auth = call_auth(self.cfg(), None, "0034600333444")
        self.assertEqual((auth.state, auth.person), ("pin", "person.sonia"))
        update = build_session_update(self.cfg(), None, auth)
        self.assertEqual([t["name"] for t in update["session"]["tools"]], ["verify_pin", "home", "hang_up"])
        session, ws, home, task = await self.run_inbound(auth)
        hint = ws.of_type("response.create")[0]["response"]["instructions"]
        self.assertIn("PIN", hint)
        ws.push(call("home", {"request": "status"}, "h1"))
        await asyncio.sleep(0.05)
        self.assertFalse(ws.tool_outputs()[-1]["ok"], "inbound: nothing before the PIN")
        ws.push(call("verify_pin", {"pin": "739258"}, "p0"))  # Luis's PIN, Sonia's phone
        await asyncio.sleep(0.05)
        self.assertIsNone(session.pin_person)
        ws.push(call("verify_pin", {"pin": "482916"}, "p1"))
        await asyncio.sleep(0.05)
        self.assertEqual(session.pin_person, "person.sonia")
        ws.push(call("home", {"request": "status"}, "h2"))
        await asyncio.sleep(0.05)
        self.assertEqual(home.calls, ["status"])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_unlinked_or_unknown_number_is_query_only(self):
        from app.session import call_auth
        for number in ("699999999", ""):
            auth = call_auth(self.cfg(), None, number)
            self.assertEqual(auth.state, "locked")
            session, ws, home, task = await self.run_inbound(auth)
            self.assertEqual([t["name"] for t in ws.sent[0]["session"]["tools"]], ["home_status", "hang_up"])
            self.assertIn("ONLY answer questions", ws.sent[0]["session"]["instructions"])
            ws.push(call("home_status", {}, "s1"))
            await asyncio.sleep(0.05)
            self.assertTrue(ws.tool_outputs()[-1]["ok"])
            ws.push(call("home", {"request": "open"}, "h1"))
            await asyncio.sleep(0.05)
            self.assertEqual(home.calls, [])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_note_is_told_without_hanging_up(self):
        from app.session import call_auth
        session, ws, home, task = await self.run_inbound(call_auth(self.cfg(), None, ""))
        self.assertTrue(await session.note("La alarma se ha activado."))
        notes = [e["item"]["content"][0]["text"] for e in ws.of_type("conversation.item.create")
                 if e["item"]["type"] == "message"]
        self.assertIn("La alarma se ha activado.", notes[-1])
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
