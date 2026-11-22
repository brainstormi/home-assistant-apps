import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import Engine, IncidentLog  # noqa: E402
from app.media import MediaStore  # noqa: E402
from app.notify import Notifier, email_parts, telegram_text  # noqa: E402
from app.store import SECRET_KEYS, PinStatus, Store, public_settings  # noqa: E402
from tests.test_acc_engine import TRIGGER  # noqa: E402
from tests.test_acc_frigate import MediaPort  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.photos, self.videos = [], [], [], []
        self.next_id = 100

    async def send(self, chat, text, silent=False, html_mode=False):
        self.next_id += 1
        self.sent.append((chat, text))
        return self.next_id

    async def edit(self, chat, mid, text, html_mode=True):
        self.edits.append((chat, mid, text))

    async def photo(self, chat, data, caption):
        self.photos.append((chat, caption))

    async def video(self, chat, data, caption):
        self.videos.append((chat, caption, len(data)))


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.json")
        self.store.save_settings({"call_delay": 0, "rounds": 1, "post_minutes": 10, "external_notify_delay": 30})
        none = PinStatus("none", [])
        self.pablo = self.store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003",
                                              "email": "pablo@example.com", "telegram_chat_id": "1111"}, none)
        self.ana = self.store.save_contact({"kind": "external", "name": "Ana", "email": "ana@example.com"}, none)
        self.log = IncidentLog(Path(self.tmp.name) / "i.json")
        self.media = MediaStore(Path(self.tmp.name) / "m")
        self.tg = FakeTelegram()
        self.mails = []

        async def mailer(to, subject, plain, body, attachments):
            self.mails.append((to, subject, plain, len(attachments)))

        self.notifier = Notifier(self.store, self.media, time_scale=0, telegram=self.tg, mailer=mailer)
        self.port = MediaPort({"600000003": [{"resultado": "confirmed"}]})
        self.engine = Engine(self.store, self.port, self.log, time_scale=0, media=self.media)
        self.engine.frigate.set_helpers({"input_text.a": "Portal"})
        self.engine.listeners.append(self.notifier.on_change)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)
            await self.notifier.drain()

    async def test_full_incident(self):
        now = time.time()
        await self.engine.on_person_helper("input_text.a", f"{now - 60:.6f}-review,{now - 59:.6f}-det001")
        await self.engine.on_alarm_state("armed_away")
        await self.engine.on_alarm_state("triggered", TRIGGER)
        inc = self.engine.current
        await inc.task
        await self.settle()
        # Telegram: live message, ack milestone, the photo
        live = [t for c, t in self.tg.sent if t.startswith("<b>🚨")]
        self.assertEqual(len(live), 1)
        self.assertTrue(any("Pablo atiende la alarma" in t for _, t in self.tg.sent))
        self.assertEqual([c for c, _ in self.tg.photos], ["1111"])
        # first email to both, with the timeline so far
        self.assertEqual(sorted(m[0] for m in self.mails), ["ana@example.com", "pablo@example.com"])
        self.assertTrue(all(m[1].startswith("Aviso:") for m in self.mails))
        self.assertIn("Pablo confirma", self.mails[0][2])
        self.mails.clear()

        await self.engine.on_alarm_state("disarmed")
        await self.settle()
        self.assertTrue(any("Alarma desarmada" in t for _, t in self.tg.sent))
        self.assertEqual(self.mails, [])  # nothing until it closes
        inc.record_until = time.time() - 1
        await self.engine._closer(inc)
        await self.settle()
        self.assertEqual(sorted(m[0] for m in self.mails), ["ana@example.com", "pablo@example.com"])
        self.assertTrue(all(m[1].startswith("Informe final:") and m[3] == 1 for m in self.mails))
        self.assertTrue(any("Incidente cerrado" in t for _, t in self.tg.sent))
        self.assertIn("Incidente cerrado", self.tg.edits[-1][2])
        # nothing more after the close
        n = (len(self.tg.sent), len(self.mails))
        inc.add("sensor", "algo tarde")
        await self.settle()
        self.assertEqual((len(self.tg.sent), len(self.mails)), n)

    async def test_quick_disarm_sends_nothing(self):
        self.port.gate = asyncio.Event()
        self.store.save_settings({"call_delay": 60})
        self.notifier.time_scale = 0.001  # the 30 s delay takes 30 ms
        await self.engine.on_alarm_state("armed_away")
        await self.engine.on_alarm_state("triggered", TRIGGER)
        await self.engine.on_alarm_state("disarmed")
        await asyncio.sleep(0.06)
        await self.settle()
        self.assertEqual((self.tg.sent, self.mails), ([], []))

    async def test_drill_only_participants_and_marked(self):
        await self.engine.start_drill([self.pablo["id"]], "")
        await self.engine.current.task
        await self.settle()
        self.assertEqual([m[0] for m in self.mails], ["pablo@example.com"])
        self.assertIn("SIMULACRO", self.mails[0][1])
        self.assertTrue(self.tg.sent[0][1].startswith("<b>🧪"))

    async def test_disarmed_during_calls_only_final_email(self):
        self.port.gate = asyncio.Event()
        await self.engine.on_alarm_state("armed_away")
        await self.engine.on_alarm_state("triggered", TRIGGER)
        await asyncio.wait_for(self.port.in_call.wait(), 2)
        await self.settle()
        await self.engine.on_alarm_state("disarmed")
        self.port.gate.set()
        inc = self.engine.current
        await inc.task
        await self.settle()
        self.assertEqual(self.mails, [])
        inc.record_until = time.time() - 1
        await self.engine._closer(inc)
        await self.settle()
        self.assertEqual(len(self.mails), 2)

    async def test_failures_go_to_the_timeline(self):
        async def broken(*_):
            raise OSError("smtp down")

        self.notifier._mailer = broken
        await self.engine.on_alarm_state("armed_away")
        await self.engine.on_alarm_state("triggered", TRIGGER)
        await self.engine.current.task
        await self.settle()
        texts = [e["text"] for e in self.engine.current.events]
        self.assertIn("No se pudo enviar correo a Pablo", texts)


class RenderTests(unittest.TestCase):
    INC = {"kind": "alarm", "sensor": "Puerta", "started": "2026-11-22T16:05:23+01:00", "status": "acknowledged",
           "acknowledged_by": "Luis", "phase": "open",
           "events": [{"t": "2026-11-22T16:05:23+01:00", "kind": "triggered", "text": "Alarma disparada", "detail": "Sensor: Puerta"},
                      {"t": "2026-11-22T16:06:29+01:00", "kind": "confirmed", "text": "Luis confirma el aviso", "detail": ""}]}

    def test_telegram_and_email_text(self):
        text = telegram_text(self.INC)
        self.assertIn("<b>Estado: atendida por Luis. Sigue abierta hasta que se desarme.</b>", text)
        self.assertIn("🟢 <code>16:06:29</code> Luis confirma el aviso", text)
        self.assertIn("🔴 <code>16:05:23</code> Alarma disparada <i>(Sensor: Puerta)</i>", text)
        subject, plain, body = email_parts(self.INC, final=False)
        self.assertTrue(subject.startswith("Aviso: Alarma en casa · Puerta"))
        self.assertIn("informe final", plain)
        self.assertIn("<table", body)

    def test_long_timeline_is_cut(self):
        inc = dict(self.INC, events=[{"t": "2026-11-22T16:05:23+01:00", "text": "x" * 100, "detail": ""}] * 100)
        self.assertLessEqual(len(telegram_text(inc)), 4000)


class SecretTests(unittest.TestCase):
    def test_secrets_kept_hidden_and_removable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "c.json")
            token = "123456789:" + "A" * 35
            public = store.save_settings({"telegram_token": token, "smtp_password": "pw"})
            self.assertEqual((public["telegram_token"], public["secrets_set"]["telegram_token"]), ("", True))
            store.save_settings({"telegram_token": "", "smtp_password": ""})
            self.assertEqual((store.settings()["telegram_token"], store.settings()["smtp_password"]), (token, "pw"))
            store.save_settings({"smtp_password": None})
            self.assertEqual(store.settings()["smtp_password"], "")
            self.assertTrue(all(public_settings(store.settings())[k] == "" for k in SECRET_KEYS))


if __name__ == "__main__":
    unittest.main()


class SenderTests(unittest.TestCase):
    def test_from_header_with_name(self):
        import smtplib
        from unittest import mock

        from app.notify import send_email

        sent = []

        class FakeSMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self, context=None):
                pass

            def login(self, user, pw):
                pass

            def send_message(self, msg):
                sent.append(msg)

        settings = {"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_security": "starttls", "smtp_user": "u",
                    "smtp_password": "p", "smtp_from": "alarma@example.com", "smtp_from_name": "Alarma de casa"}
        with mock.patch.object(smtplib, "SMTP", FakeSMTP):
            send_email(settings, "a@example.com", "s", "t", "<p>t</p>")
        self.assertEqual(sent[0]["From"], "Alarma de casa <alarma@example.com>")


class NumberingAndVideoTests(unittest.IsolatedAsyncioTestCase):
    def test_numbers_and_captions(self):
        from app.notify import media_caption, media_numbers

        inc = {"kind": "alarm", "sensor": "Puerta", "started": "2026-11-22T16:05:23+01:00", "status": "calling", "phase": "calling",
               "events": [{"t": "2026-11-22T16:04:57+01:00", "kind": "person", "text": "Persona en Portal", "detail": "antes del disparo",
                           "media": {"review_id": "r1", "event_id": "e1"}},
                          {"t": "2026-11-22T16:05:23+01:00", "kind": "triggered", "text": "Alarma disparada", "detail": ""},
                          {"t": "2026-11-22T16:06:00+01:00", "kind": "person", "text": "Persona en Patio", "detail": "",
                           "media": {"review_id": "r2", "event_id": "e2"}}]}
        self.assertEqual(media_numbers(inc), {"r1": 1, "r2": 2})
        text = telegram_text(inc)
        self.assertIn("Persona en Portal 📷1", text)
        self.assertIn("Persona en Patio 📷2", text)
        self.assertEqual(media_caption(inc, inc["events"][2], "🎬"), "🎬2 Vídeo · Persona en Patio · 16:06:00")
        self.assertEqual(media_caption(inc, inc["events"][0], "📷"), "📷1 Foto · Persona en Portal · 16:04:57 · antes del disparo")

    def test_attachment_names_and_no_frigate_wording(self):
        from app.notify import photo_filename

        e = {"t": "2026-11-22T17:27:22+01:00", "kind": "person", "text": "Persona en Entrada", "detail": "Alerta de Frigate · antes del disparo",
             "media": {"review_id": "r1"}}
        inc = {"kind": "alarm", "sensor": "Puerta", "started": "2026-11-22T17:27:54+01:00", "status": "calling", "phase": "calling", "events": [e]}
        self.assertEqual(photo_filename(inc, e), "Foto 1 - Persona en Entrada 17-27-22.jpg")
        self.assertNotIn("Frigate", telegram_text(inc))
        self.assertNotIn("Frigate", "".join(email_parts(inc, final=True)))
        self.assertIn("Vista por la cámara", telegram_text(inc))

    async def test_video_sent_once_the_event_is_over(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.json")
        store.save_settings({"call_delay": 0, "rounds": 1, "external_notify_delay": 0})
        store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003", "telegram_chat_id": "1111"}, PinStatus("none", []))
        media = MediaStore(Path(tmp.name) / "m")

        class FakeFrigate:
            def __init__(self):
                self.calls = 0

            def event(self, event_id):
                self.calls += 1
                return {"end_time": 1.0 if self.calls >= 3 else None, "has_clip": True}

            def open(self, event_id, name, range_header=""):
                import io

                class R(io.BytesIO):
                    pass
                return R(b"mp4-bytes")

        frigate = FakeFrigate()
        tg = FakeTelegram()
        notifier = Notifier(store, media, time_scale=0, telegram=tg, mailer=None, frigate=frigate)
        notifier._mailer = lambda *a: asyncio.sleep(0)
        engine = Engine(store, MediaPort({"600000003": [{"resultado": "confirmed"}]}), IncidentLog(Path(tmp.name) / "i.json"),
                        time_scale=0, media=media)
        engine.frigate.set_helpers({"input_text.a": "Portal"})
        engine.listeners.append(notifier.on_change)
        now = time.time()
        await engine.on_person_helper("input_text.a", f"{now - 30:.6f}-review,{now - 29:.6f}-det001")
        await engine.on_alarm_state("armed_away")
        await engine.on_alarm_state("triggered", TRIGGER)
        await engine.current.task
        for _ in range(5):
            await asyncio.sleep(0)
            await notifier.drain()
        self.assertEqual(tg.videos, [("1111", "🎬1 Vídeo · Persona en Portal · " + tg.photos[0][1].split(" · ")[2] + " · Vista por la cámara · antes del disparo", 9)])
        self.assertTrue(tg.photos[0][1].startswith("📷1 Foto · Persona en Portal"))
        self.assertGreaterEqual(frigate.calls, 3)
        self.assertTrue(media.clip_path(f"{now - 29:.6f}-det001").exists())


class ResendTests(unittest.IsolatedAsyncioTestCase):
    async def test_resend_report(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.json")
        c = store.save_contact({"kind": "external", "name": "Pablo", "phone": "600000003", "email": "p@example.com",
                                "telegram_chat_id": "1111"}, PinStatus("none", []))
        media = MediaStore(Path(tmp.name) / "m")
        media.save("inc1", "1795364841.846399-ig3yer", b"jpg")
        tg = FakeTelegram()
        mails = []

        async def mailer(to, subject, plain, body, attachments):
            mails.append((to, subject, len(attachments)))

        notifier = Notifier(store, media, time_scale=0, telegram=tg, mailer=mailer)
        data = {"id": "inc1", "kind": "drill", "sensor": "Puerta", "started": "2026-11-22T17:27:54+01:00", "status": "acknowledged",
                "acknowledged_by": "Pablo", "phase": "closed",
                "events": [{"t": "2026-11-22T17:27:22+01:00", "kind": "person", "text": "Persona en Portal", "detail": "",
                            "media": {"review_id": "r1", "event_id": "1795364841.846399-ig3yer", "snapshot": "1795364841.846399-ig3yer"}}]}
        result = await notifier.resend(data, [c["id"]])
        self.assertEqual(result, {"sent": ["Telegram a Pablo", "correo a Pablo"], "errors": []})
        self.assertIn("📷1", tg.sent[0][1])
        self.assertEqual(tg.photos[0][1], "📷1 Foto · Persona en Portal · 17:27:22")
        self.assertEqual(mails, [("p@example.com", "Reenvío · Informe final: SIMULACRO de alarma en casa · Puerta (22/11/2026 17:27)", 1)])
        with self.assertRaises(RuntimeError):
            await notifier.resend(data, [])
