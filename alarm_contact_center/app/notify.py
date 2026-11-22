"""Email and Telegram for the contacts, alongside the calls.

What each channel gets (only contacts taking part, with an email or a Telegram chat):

* **Telegram:** one message per contact with the timeline, edited as the incident
  goes on (edits are silent), plus a short new message for what matters (someone
  takes charge, nobody answers, disarmed, closed) and the photos of people seen by
  the cameras as they arrive. The live message is finished when the incident closes.
* **Email:** one when the chain of calls ends (someone took charge, or nobody did)
  with the timeline so far, and the final report when the incident closes, with
  the whole timeline and the photos attached. Email cannot be edited, so nothing
  in between.

Nothing is sent if the alarm is disarmed within ``external_notify_delay`` seconds:
most alarms are someone at home who forgot. Drills are sent too, marked as such,
only to the contacts taking part.

The App talks to the Telegram Bot API and to an SMTP server itself, with the bot
token and the SMTP account set in the settings.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX = 4000  # Telegram's limit is 4096 characters per message
EDIT_EVERY_S = 4.0  # at most one edit per message this often (Telegram rate limits)
MAX_PHOTOS_EMAIL = 12
TELEGRAM_VIDEO_MAX = 49 * 1024 * 1024  # bots can upload up to 50 MB
VIDEO_POLL_S = 10  # how often Frigate is asked whether the event is over
VIDEO_POLLS = 120  # ...for up to 20 minutes

STATUS_TEXT = {
    "waiting": "esperando antes de llamar",
    "calling": "llamando a los contactos",
    "acknowledged": "atendida",
    "disarmed": "desarmada",
    "no_answer": "nadie la ha atendido",
    "trunk_down": "sin línea telefónica",
    "cancelled": "avisos parados desde el panel",
    "failed": "error en los avisos",
    "timeout": "sin respuesta en el tiempo máximo",
}
PHASE_TEXT = {
    "calling": "",
    "open": "Sigue abierta hasta que se desarme.",
    "closing": "Desarmada: se sigue registrando unos minutos.",
    "closed": "Incidente cerrado.",
}


class NotifyError(Exception):
    pass


# --------------------------------------------------------------------- render
def _clock(iso: str) -> str:
    return iso[11:19] if len(iso) >= 19 else iso


def title(inc: dict[str, Any]) -> str:
    what = "SIMULACRO de alarma" if inc.get("kind") == "drill" else "Alarma"
    return f"{what} en casa · {inc.get('sensor') or 'sensor desconocido'}"


def status_line(inc: dict[str, Any]) -> str:
    status = STATUS_TEXT.get(inc.get("status", ""), inc.get("status", ""))
    if inc.get("status") == "acknowledged" and inc.get("acknowledged_by"):
        status = f"atendida por {inc['acknowledged_by']}"
    extra = PHASE_TEXT.get(inc.get("phase", ""), "")
    return f"Estado: {status}." + (f" {extra}" if extra else "")


def timeline_lines(inc: dict[str, Any]) -> list[str]:
    lines = []
    for e in inc.get("events") or []:
        detail = f" ({e['detail']})" if e.get("detail") else ""
        lines.append(f"{_clock(e.get('t', ''))}  {e.get('text', '')}{detail}")
    return lines


def started_text(inc: dict[str, Any]) -> str:
    try:
        t = dt.datetime.fromisoformat(inc["started"])
        return f"{t:%d/%m/%Y %H:%M:%S}"
    except (KeyError, ValueError):
        return ""


# Same colours as the panel's timeline dots.
KIND_DOT = {
    "triggered": "🔴", "person": "🔴", "error": "🔴", "trunk_down": "🔴", "notify_error": "🔴",
    "confirmed": "🟢", "acknowledged": "🟢", "disarmed": "🟢", "resolved": "🟢", "trunk_ok": "🟢",
    "sensor": "🟡", "profile": "🟡", "voicemail": "🟡", "round": "🟡", "answered": "🟡", "failed": "🟡",
    "timeout": "🟡", "limit": "🟡",
    "call": "🔵",
    "closed": "🏁",
}


def media_numbers(inc: dict[str, Any]) -> dict[str, int]:
    """review id -> number shown next to the entry and on its photo and video (📷1, 🎬1)."""
    out: dict[str, int] = {}
    for e in inc.get("events") or []:
        rid = (e.get("media") or {}).get("review_id")
        if rid and rid not in out:
            out[rid] = len(out) + 1
    return out


def telegram_text(inc: dict[str, Any]) -> str:
    """HTML (parse_mode=HTML): bold title, a coloured dot per entry, numbered photos."""
    esc = html.escape
    numbers = media_numbers(inc)
    head = [f"<b>{'🧪' if inc.get('kind') == 'drill' else '🚨'} {esc(title(inc))}</b>",
            f"Disparo: {esc(started_text(inc))}", f"<b>{esc(status_line(inc))}</b>", ""]
    lines = []
    for e in inc.get("events") or []:
        n = numbers.get((e.get("media") or {}).get("review_id"))
        tag = f" 📷{n}" if n else ""
        detail = f" <i>({esc(e['detail'])})</i>" if e.get("detail") else ""
        lines.append(f"{KIND_DOT.get(e.get('kind', ''), '⚪')} <code>{esc(_clock(e.get('t', '')))}</code> {esc(e.get('text', ''))}{tag}{detail}")
    text = "\n".join(head + lines)
    while len(text) > TELEGRAM_MAX and len(lines) > 5:
        lines = lines[1:]
        text = "\n".join(head + ["…"] + lines)
    return text[:TELEGRAM_MAX]


def media_caption(inc: dict[str, Any], e: dict[str, Any], icon: str) -> str:
    n = media_numbers(inc).get((e.get("media") or {}).get("review_id"), 0)
    caption = f"{icon}{n} · {e.get('text', '')} · {_clock(e.get('t', ''))}"
    return caption + (f" · {e['detail']}" if e.get("detail") else "")


def email_parts(inc: dict[str, Any], final: bool) -> tuple[str, str, str]:
    """(subject, plain text, html)."""
    when = "Informe final" if final else "Aviso"
    subject = f"{when}: {title(inc)} ({started_text(inc)[:16]})"
    lines = timeline_lines(inc)
    intro = ("El incidente se ha cerrado. Esta es la cronología completa."
             if final else "Te enviaremos el informe final cuando se desarme la alarma y se cierre el incidente.")
    plain = "\n".join([title(inc), f"Disparo: {started_text(inc)}", status_line(inc), "", intro, "", "Cronología:"] + lines)
    rows = "".join(
        f"<tr><td style='padding:2px 6px 2px 0;vertical-align:top'>{KIND_DOT.get(e.get('kind', ''), '⚪')}</td>"
        f"<td style='padding:2px 12px 2px 0;color:#555;white-space:nowrap;vertical-align:top'>{html.escape(_clock(e.get('t', '')))}</td>"
        f"<td style='padding:2px 0'>{html.escape(e.get('text', ''))}"
        + (f"<br><span style='color:#777;font-size:13px'>{html.escape(e['detail'])}</span>" if e.get("detail") else "")
        + "</td></tr>"
        for e in inc.get("events") or [])
    body = (f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;color:#222'>"
            f"<h2 style='margin:0 0 4px'>{html.escape(title(inc))}</h2>"
            f"<div>Disparo: {html.escape(started_text(inc))}</div>"
            f"<div style='margin:4px 0 12px'><strong>{html.escape(status_line(inc))}</strong></div>"
            f"<p style='margin:0 0 12px'>{html.escape(intro)}</p>"
            f"<table style='border-collapse:collapse'>{rows}</table>"
            f"<p style='color:#777;font-size:12px;margin-top:16px'>Alarm Contact Center</p></div>")
    return subject, plain, body


# ------------------------------------------------------------------- clients
class Telegram:
    def __init__(self, token: str, timeout: float = 20) -> None:
        self.token = token
        self.timeout = timeout

    async def _call(self, method: str, data: Any) -> Any:
        import aiohttp  # imported here: the panel and its tests work without it

        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
            kwargs = {"data": data} if isinstance(data, aiohttp.FormData) else {"json": data}
            async with session.post(url, **kwargs) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    body = {}
        if not body.get("ok"):
            desc = str(body.get("description") or f"HTTP {resp.status}")
            if "message is not modified" in desc:
                return None
            raise NotifyError(desc)
        return body.get("result")

    async def send(self, chat_id: str, text: str, silent: bool = False, html_mode: bool = False) -> int:
        data: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_notification": silent}
        if html_mode:
            data["parse_mode"] = "HTML"
        result = await self._call("sendMessage", data)
        return int(result["message_id"])

    async def edit(self, chat_id: str, message_id: int, text: str, html_mode: bool = True) -> None:
        data: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if html_mode:
            data["parse_mode"] = "HTML"
        await self._call("editMessageText", data)

    async def video(self, chat_id: str, data: bytes, caption: str) -> None:
        import aiohttp

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption[:1000])
        form.add_field("supports_streaming", "true")
        form.add_field("video", data, filename="clip.mp4", content_type="video/mp4")
        old, self.timeout = self.timeout, 120  # uploads take longer
        try:
            await self._call("sendVideo", form)
        finally:
            self.timeout = old

    async def photo(self, chat_id: str, data: bytes, caption: str) -> None:
        import aiohttp

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption[:1000])
        form.add_field("photo", data, filename="foto.jpg", content_type="image/jpeg")
        await self._call("sendPhoto", form)

    async def me(self) -> dict[str, Any]:
        return await self._call("getMe", {}) or {}

    async def recent_chats(self) -> list[dict[str, str]]:
        """Chats that wrote to the bot recently (to fill a contact's chat id)."""
        updates = await self._call("getUpdates", {"limit": 100, "timeout": 0}) or []
        chats: dict[str, dict[str, str]] = {}
        for u in updates:
            msg = u.get("message") or u.get("edited_message") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if not chat.get("id"):
                continue
            name = chat.get("title") or " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p) \
                or chat.get("username") or str(chat["id"])
            chats[str(chat["id"])] = {"id": str(chat["id"]), "name": name, "type": str(chat.get("type") or "")}
        return list(chats.values())


def send_email(settings: dict[str, Any], to: str, subject: str, plain: str, body_html: str,
               attachments: list[tuple[str, bytes]] | None = None) -> None:
    """Blocking: run in a thread."""
    if not settings.get("smtp_host"):
        raise NotifyError("Falta el servidor de correo (Ajustes → Correo).")
    msg = EmailMessage()
    msg["Subject"] = subject
    sender = settings.get("smtp_from") or settings.get("smtp_user")
    if not sender or "@" not in sender:
        raise NotifyError("Falta la dirección del remitente (Ajustes → Correo).")
    msg["From"] = formataddr((settings.get("smtp_from_name") or "", sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="alarm-contact-center")
    msg.set_content(plain)
    msg.add_alternative(body_html, subtype="html")
    for name, data in attachments or []:
        msg.add_attachment(data, maintype="image", subtype="jpeg", filename=name)
    host, port = settings["smtp_host"], int(settings.get("smtp_port") or 587)
    security = settings.get("smtp_security") or "starttls"
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            if settings.get("smtp_user"):
                server.login(settings["smtp_user"], settings.get("smtp_password") or "")
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        raise NotifyError(str(err) or err.__class__.__name__) from err


# ------------------------------------------------------------------ notifier
class _State:
    def __init__(self) -> None:
        self.armed = False  # past the delay and not disarmed: contacts are being told
        self.skipped = False  # disarmed within the delay: nothing is ever sent
        self.tg_messages: dict[str, int] = {}  # chat id -> live message id
        self.tg_text: dict[str, str] = {}
        self.photos_sent: set[str] = set()
        self.videos: set[str] = set()  # events whose clip is being or has been sent
        self.milestones: set[str] = set()
        self.first_email = False
        self.final_email = False
        self.done = False  # closed and reported: late changes send nothing
        self.lock = asyncio.Lock()  # one update at a time: no duplicated messages
        self.edit_task: asyncio.Task | None = None
        self.last_edit = 0.0


class Notifier:
    """Engine listener: called with the Incident after every change."""

    def __init__(self, store: Any, media: Any = None, time_scale: float = 1.0,
                 telegram: Any = None, mailer: Any = None, frigate: Any = None) -> None:
        self.store = store
        self.media = media
        self.frigate = frigate  # FrigateAPI: to know when an event is over and fetch its clip
        self.time_scale = time_scale
        self._telegram = telegram  # tests inject fakes
        self._mailer = mailer
        self.states: dict[str, _State] = {}
        self.tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ plumbing
    def telegram(self) -> Any:
        if self._telegram is not None:
            return self._telegram
        token = self.store.settings().get("telegram_token")
        return Telegram(token) if token else None

    async def mail(self, to: str, subject: str, plain: str, body: str, attachments: list[tuple[str, bytes]]) -> None:
        if self._mailer is not None:
            await self._mailer(to, subject, plain, body, attachments)
            return
        await asyncio.to_thread(send_email, self.store.settings(), to, subject, plain, body, attachments)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        """Tests: wait for everything in flight."""
        for _ in range(1000):
            if not self.tasks:
                return
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            await asyncio.sleep(0)  # let the done callbacks remove finished tasks

    def recipients(self, inc: Any) -> list[dict[str, Any]]:
        out = []
        for c in self.store.contacts():
            if not c["active"]:
                continue
            if inc.only_contacts is not None and c["id"] not in inc.only_contacts:
                continue
            if c["email"] or c["telegram_chat_id"]:
                out.append(c)
        return out

    # ---------------------------------------------------------------- input
    def on_change(self, inc: Any) -> None:
        if inc.kind not in ("alarm", "drill"):
            return
        st = self.states.get(inc.id)
        if st is None:
            st = self.states[inc.id] = _State()
            self._spawn(self._arm_later(inc, st))
            return
        if st.skipped or st.done:
            return
        if not st.armed:
            if inc.disarmed_at and inc.disarmed_at - inc.started < self.store.settings()["external_notify_delay"]:
                st.skipped = True
            return
        self._spawn(self._update(inc, st))

    async def _arm_later(self, inc: Any, st: _State) -> None:
        delay = self.store.settings()["external_notify_delay"]
        await asyncio.sleep(delay * self.time_scale)
        if inc.disarmed_at and inc.disarmed_at - inc.started < delay:
            st.skipped = True
            return
        if not self.recipients(inc):
            st.skipped = True
            return
        st.armed = True
        await self._update(inc, st)

    # --------------------------------------------------------------- output
    async def _update(self, inc: Any, st: _State) -> None:
        async with st.lock:
            await self._update_locked(inc, st)

    async def _update_locked(self, inc: Any, st: _State) -> None:
        data = inc.to_dict()
        tg = self.telegram()
        people = self.recipients(inc)
        if tg is not None:
            await self._telegram_update(inc, st, tg, data, people)
        phase = data["phase"]
        if not st.first_email and phase != "calling":
            st.first_email = True
            if not inc.disarmed_at:  # disarmed during the calls: only the final report
                await self._emails(inc, data, people, final=False)
        if phase == "closed" and not st.final_email:
            st.final_email = True
            st.done = True
            await self._emails(inc, data, people, final=True)

    async def _telegram_update(self, inc: Any, st: _State, tg: Any, data: dict[str, Any], people: list[dict[str, Any]]) -> None:
        chats = [c for c in people if c["telegram_chat_id"]]
        text = telegram_text(data)
        for c in chats:
            chat = c["telegram_chat_id"]
            if chat not in st.tg_messages:
                try:
                    st.tg_messages[chat] = await tg.send(chat, text, html_mode=True)
                    st.tg_text[chat] = text
                except Exception as err:  # noqa: BLE001
                    self._failed(inc, f"Telegram a {c['name']}", err)
                    st.tg_messages[chat] = 0  # do not retry the live message on every change
        await self._schedule_edit(inc, st, tg, chats)
        for key, text_ in self._milestones(data):
            if key in st.milestones:
                continue
            st.milestones.add(key)
            for c in chats:
                try:
                    await tg.send(c["telegram_chat_id"], text_)
                except Exception as err:  # noqa: BLE001
                    self._failed(inc, f"Telegram a {c['name']}", err)
        if self.store.settings()["send_photos_external"] and self.media is not None:
            for e in data["events"]:
                snap = (e.get("media") or {}).get("snapshot")
                if not snap or snap in st.photos_sent:
                    continue
                st.photos_sent.add(snap)
                photo = self.media.read(inc.id, snap)
                if not photo:
                    continue
                caption = media_caption(data, e, "📷")
                for c in chats:
                    try:
                        await tg.photo(c["telegram_chat_id"], photo, caption)
                    except Exception as err:  # noqa: BLE001
                        self._failed(inc, f"foto por Telegram a {c['name']}", err)
        if self.store.settings()["send_videos_telegram"] and self.frigate is not None and self.media is not None and chats:
            for e in data["events"]:
                event_id = (e.get("media") or {}).get("event_id")
                if event_id and event_id not in st.videos:
                    st.videos.add(event_id)
                    self._spawn(self._video(inc, tg, e, event_id))

    async def _schedule_edit(self, inc: Any, st: _State, tg: Any, chats: list[dict[str, Any]]) -> None:
        if st.edit_task and not st.edit_task.done():
            return  # an edit is already pending: it will pick up the latest state

        async def edit() -> None:
            wait = st.last_edit + EDIT_EVERY_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait * self.time_scale)
            st.last_edit = time.monotonic()
            text = telegram_text(inc.to_dict())
            for c in chats:
                chat = c["telegram_chat_id"]
                mid = st.tg_messages.get(chat)
                if not mid or st.tg_text.get(chat) == text:
                    continue
                try:
                    await tg.edit(chat, mid, text)
                    st.tg_text[chat] = text
                except Exception as err:  # noqa: BLE001
                    log.warning("[%s] Telegram edit for %s failed: %s", inc.id, c["name"], err)

        st.edit_task = asyncio.get_running_loop().create_task(edit())
        self.tasks.add(st.edit_task)
        st.edit_task.add_done_callback(self.tasks.discard)

    async def _video(self, inc: Any, tg: Any, entry: dict[str, Any], event_id: str) -> None:
        """Frigate has the clip once the event is over: wait for it, then send it."""
        ended = False
        for _ in range(VIDEO_POLLS):
            try:
                event = await asyncio.to_thread(self.frigate.event, event_id)
            except Exception as err:  # noqa: BLE001
                log.info("[%s] Frigate event %s: %s", inc.id, event_id, err)
                event = {}
            if event.get("end_time"):
                ended = event.get("has_clip", True)
                break
            await asyncio.sleep(VIDEO_POLL_S * self.time_scale)
        if not ended:
            log.warning("[%s] no clip for %s", inc.id, event_id)
            return
        try:
            path = self.media.clip_path(event_id)
            if not path.exists():
                def fetch() -> None:
                    with self.frigate.open(event_id, "clip.mp4") as resp:
                        self.media.cache_clip(event_id, resp)
                await asyncio.to_thread(fetch)
            data = path.read_bytes()
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] clip %s not available: %s", inc.id, event_id, err)
            return
        if len(data) > TELEGRAM_VIDEO_MAX:
            log.warning("[%s] clip %s too large for Telegram (%d bytes)", inc.id, event_id, len(data))
            return
        caption = media_caption(inc.to_dict(), entry, "🎬")
        for c in self.recipients(inc):
            if not c["telegram_chat_id"]:
                continue
            try:
                await tg.video(c["telegram_chat_id"], data, caption)
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] video by Telegram to %s failed: %s", inc.id, c["name"], err)

    @staticmethod
    def _milestones(data: dict[str, Any]) -> list[tuple[str, str]]:
        drill = "Simulacro: " if data.get("kind") == "drill" else ""
        out = []
        if data.get("status") == "acknowledged" and data.get("acknowledged_by"):
            out.append(("ack", f"✅ {drill}{data['acknowledged_by']} atiende la alarma. Sigue abierta hasta que se desarme."))
        elif data["phase"] != "calling" and data.get("status") in ("no_answer", "timeout", "trunk_down", "failed"):
            out.append(("noack", f"⚠️ {drill}Nadie ha atendido la alarma ({STATUS_TEXT.get(data['status'], data['status'])})."))
        if data.get("disarmed"):
            out.append(("disarmed", f"🔓 {drill}Alarma desarmada."))
        if data["phase"] == "closed":
            out.append(("closed", f"🏁 {drill}Incidente cerrado. Te llegará el informe por correo si lo tienes configurado."))
        return out

    async def _emails(self, inc: Any, data: dict[str, Any], people: list[dict[str, Any]], final: bool) -> None:
        to = [c for c in people if c["email"]]
        if not to:
            return
        subject, plain, body = email_parts(data, final)
        attachments: list[tuple[str, bytes]] = []
        if self.store.settings()["send_photos_external"] and self.media is not None:
            for e in data["events"]:
                snap = (e.get("media") or {}).get("snapshot")
                photo = self.media.read(inc.id, snap) if snap else None
                if photo and len(attachments) < MAX_PHOTOS_EMAIL:
                    attachments.append((f"{_clock(e['t']).replace(':', '')}-{len(attachments) + 1}.jpg", photo))
        for c in to:
            try:
                await self.mail(c["email"], subject, plain, body, attachments)
                log.info("[%s] %s email sent to %s", inc.id, "final" if final else "first", c["name"])
            except Exception as err:  # noqa: BLE001
                self._failed(inc, f"correo a {c['name']}", err)

    @staticmethod
    def _failed(inc: Any, what: str, err: Exception) -> None:
        log.warning("[%s] %s failed: %s", inc.id, what, err)
        # Shown in the timeline; no listener loop: adding it only re-renders Telegram.
        inc.add("notify_error", f"No se pudo enviar {what}", str(err)[:200])


# ---------------------------------------------------------------- panel tools
class PanelTools:
    """What the panel's buttons call (on the asyncio loop)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _bot(self) -> Telegram:
        token = self.store.settings().get("telegram_token")
        if not token:
            raise RuntimeError("Primero guarda el token del bot en Ajustes → Telegram.")
        return Telegram(token)

    async def test(self, channel: str, to: str) -> dict[str, Any]:
        to = (to or "").strip()
        if not to:
            raise ValueError("Falta el destinatario.")
        try:
            if channel == "telegram":
                await self._bot().send(to, "✅ Prueba de Alarm Contact Center: así te llegarán los avisos de la alarma.")
            elif channel == "email":
                await asyncio.to_thread(
                    send_email, self.store.settings(), to, "Prueba de Alarm Contact Center",
                    "Así te llegarán los avisos de la alarma por correo.",
                    "<p>Así te llegarán los avisos de la alarma por correo.</p>")
            else:
                raise ValueError("Canal desconocido.")
        except NotifyError as err:
            raise RuntimeError(f"No se pudo enviar: {err}") from err
        return {"ok": True}

    async def chats(self) -> dict[str, Any]:
        bot = self._bot()
        try:
            me = await bot.me()
            chats = await bot.recent_chats()
        except NotifyError as err:
            raise RuntimeError(f"Telegram responde: {err}") from err
        return {"bot": me.get("username") or "", "chats": chats}
