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
import contextlib
import datetime as dt
import html
import logging
import re
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
    "acknowledged": "gestionada",
    "disarmed": "desarmada",
    "no_answer": "nadie ha aceptado gestionarla",
    "trunk_down": "sin línea telefónica: se avisa por notificaciones",
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
def _detail(e: dict[str, Any]) -> str:
    """The entry's detail as contacts see it (older incidents said "Alerta de Frigate")."""
    return str(e.get("detail") or "").replace("Alerta de Frigate", "Vista por la cámara")


def _clock(iso: str) -> str:
    return iso[11:19] if len(iso) >= 19 else iso


def title(inc: dict[str, Any]) -> str:
    what = "SIMULACRO de alarma" if inc.get("kind") == "drill" else "Alarma"
    return f"{what} en casa · {inc.get('sensor') or 'sensor desconocido'}"


def status_line(inc: dict[str, Any]) -> str:
    status = STATUS_TEXT.get(inc.get("status", ""), inc.get("status", ""))
    if inc.get("status") == "acknowledged" and inc.get("acknowledged_by"):
        status = f"gestionada por {inc['acknowledged_by']}"
    extra = PHASE_TEXT.get(inc.get("phase", ""), "")
    return f"Estado: {status}." + (f" {extra}" if extra else "")


def timeline_lines(inc: dict[str, Any]) -> list[str]:
    lines = []
    for e in inc.get("events") or []:
        detail = f" ({_detail(e)})" if _detail(e) else ""
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
    "call": "🔵", "burst": "🔵", "telegram": "🔵", "retry": "🟡", "skip": "⚪",
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
        detail = f" <i>({esc(_detail(e))})</i>" if _detail(e) else ""
        lines.append(f"{KIND_DOT.get(e.get('kind', ''), '⚪')} <code>{esc(_clock(e.get('t', '')))}</code> {esc(e.get('text', ''))}{tag}{detail}")
    text = "\n".join(head + lines)
    while len(text) > TELEGRAM_MAX and len(lines) > 5:
        lines = lines[1:]
        text = "\n".join(head + ["…"] + lines)
    return text[:TELEGRAM_MAX]


MEDIA_WORD = {"📷": "Foto", "🎬": "Vídeo"}


def media_caption(inc: dict[str, Any], e: dict[str, Any], icon: str) -> str:
    """'📷1 Foto · Persona en Entrada · 17:27:22 · antes del disparo'."""
    n = media_numbers(inc).get((e.get("media") or {}).get("review_id"), 0)
    caption = f"{icon}{n} {MEDIA_WORD.get(icon, '')} · {e.get('text', '')} · {_clock(e.get('t', ''))}"
    return caption + (f" · {_detail(e)}" if _detail(e) else "")


def photo_filename(inc: dict[str, Any], e: dict[str, Any]) -> str:
    """'Foto 1 - Persona en Entrada 17-27-22.jpg' for email attachments."""
    n = media_numbers(inc).get((e.get("media") or {}).get("review_id"), 0)
    name = re.sub(r'[\\/:*?"<>|]+', " ", str(e.get("text") or "")).strip()
    return f"Foto {n} - {name} {_clock(e.get('t', '')).replace(':', '-')}.jpg"


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
        + (f"<br><span style='color:#777;font-size:13px'>{html.escape(_detail(e))}</span>" if _detail(e) else "")
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

    async def send(self, chat_id: str, text: str, silent: bool = False, html_mode: bool = False,
                   button: str = "") -> int:
        data: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_notification": silent}
        if html_mode:
            data["parse_mode"] = "HTML"
        if button:
            data["reply_markup"] = take_charge_markup(button)
        result = await self._call("sendMessage", data)
        return int(result["message_id"])

    async def edit(self, chat_id: str, message_id: int, text: str, html_mode: bool = True, button: str = "") -> None:
        # Without reply_markup an edit removes the button: passing it keeps it.
        data: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text,
                                "reply_markup": take_charge_markup(button) if button else {"inline_keyboard": []}}
        if html_mode:
            data["parse_mode"] = "HTML"
        await self._call("editMessageText", data)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    async def updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        old, self.timeout = self.timeout, timeout + 15
        try:
            return await self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                                   "allowed_updates": ["message", "callback_query"]}) or []
        finally:
            self.timeout = old

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


TAKE_CHARGE = "ACC_ACK"
TAKE_CHARGE_BUTTON = "✋ Yo me encargo"
TAKE_CHARGE_EXPLAIN = "Si pulsas «Yo me encargo», dejaremos de llamar y avisar al resto de contactos. La alarma no se desarma."
TAKE_CHARGE_WORDS = ("yo me encargo", "me encargo", "lo atiendo", "lo gestiono", "yo lo gestiono")


def take_charge_markup(incident_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": TAKE_CHARGE_BUTTON, "callback_data": f"{TAKE_CHARGE}:{incident_id}"}]]}


def take_charge_reply(result: dict[str, Any], name: str) -> str:
    r = result.get("result")
    if r == "ok":
        return (f"✅ Gracias, {name}: has aceptado gestionar la alarma. Ya no se avisará a nadie más. "
                "La alarma sigue activa hasta que se desarme.")
    if r == "already":
        if result.get("by") == name:
            return "Ya habías aceptado gestionar la alarma. Sigue activa hasta que se desarme."
        return f"Ya se encarga {result.get('by')} desde las {result.get('at')}. La alarma sigue activa hasta que se desarme."
    if r == "disarmed":
        return "La alarma ya está desarmada: no hace falta hacer nada."
    return "No hay ninguna alarma abierta ahora mismo."


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
        self.engine: Any = None  # set by the App: take charge from Telegram
        self.seen_chats: dict[str, dict[str, Any]] = {}  # chats that wrote to the bot (for the chat finder)
        self._offset = 0
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
        button = self._button(data)
        for c in chats:
            chat = c["telegram_chat_id"]
            if chat not in st.tg_messages:
                try:
                    text_ = text + ("\n\n" + html.escape(TAKE_CHARGE_EXPLAIN) if button else "")
                    st.tg_messages[chat] = await tg.send(chat, text_, html_mode=True, button=button)
                    st.tg_text[chat] = text_
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
            data = inc.to_dict()
            button = self._button(data)
            text = telegram_text(data) + ("\n\n" + html.escape(TAKE_CHARGE_EXPLAIN) if button else "")
            for c in chats:
                chat = c["telegram_chat_id"]
                mid = st.tg_messages.get(chat)
                if not mid or st.tg_text.get(chat) == text:
                    continue
                try:
                    await tg.edit(chat, mid, text, button=button)
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
    def _button(data: dict[str, Any]) -> str:
        """The incident id for the "Yo me encargo" button while someone may still take charge."""
        if data.get("kind") not in ("alarm", "drill") or data.get("phase") == "closed":
            return ""
        if data.get("acknowledged_by") or data.get("disarmed"):
            return ""
        return str(data.get("id") or "")

    async def alert_contact(self, inc: Any, contact: dict[str, Any]) -> None:
        """No phone line: the contact's turn in the chain becomes a Telegram message."""
        tg = self.telegram()
        if tg is None:
            raise NotifyError("falta el token del bot")
        drill = "SIMULACRO · " if inc.kind == "drill" else ""
        text = (f"🚨 {drill}{title(inc.to_dict())}\n"
                f"No hay línea telefónica para llamarte, así que te avisamos por aquí.\n\n{TAKE_CHARGE_EXPLAIN}")
        await tg.send(contact["telegram_chat_id"], text, button=inc.id)

    # ------------------------------------------------------ bot updates
    async def poll(self) -> None:
        """Read what people send to the bot: "Yo me encargo" (button or text), and the
        chats that wrote to it, for the chat finder."""
        while True:
            tg = self.telegram()
            if tg is None:
                await asyncio.sleep(30)
                continue
            try:
                updates = await tg.updates(self._offset)
            except Exception as err:  # noqa: BLE001
                log.info("Telegram updates: %s", err)
                await asyncio.sleep(15)
                continue
            for u in updates:
                self._offset = max(self._offset, int(u.get("update_id", 0)) + 1)
                try:
                    await self.on_update(tg, u)
                except Exception:  # noqa: BLE001
                    log.exception("Telegram update failed")

    async def on_update(self, tg: Any, u: dict[str, Any]) -> None:
        cq = u.get("callback_query")
        msg = (cq or {}).get("message") or u.get("message") or {}
        chat = msg.get("chat") or {}
        sender = (cq or {}).get("from") or msg.get("from") or {}
        chat_id = str(chat.get("id") or "")
        if chat_id:
            name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p) or chat.get("title") \
                or chat.get("username") or sender.get("first_name") or chat_id
            self.seen_chats[chat_id] = {"id": chat_id, "name": name, "type": str(chat.get("type") or ""), "t": time.time()}
        if cq:
            data = str(cq.get("data") or "")
            if not data.startswith(TAKE_CHARGE + ":"):
                return
            reply = await self._take_charge(chat_id, data.split(":", 1)[1])
            with contextlib.suppress(Exception):
                await tg.answer_callback(str(cq.get("id")), reply.split(":")[0])
            await tg.send(chat_id, reply)
            return
        text = " ".join(str(msg.get("text") or "").lower().replace("«", "").replace("»", "").split()).strip(".!¡ ")
        if text in TAKE_CHARGE_WORDS:
            inc = getattr(self.engine, "current", None)
            await tg.send(chat_id, await self._take_charge(chat_id, inc.id if inc else ""))

    async def _take_charge(self, chat_id: str, incident_id: str) -> str:
        if self.engine is None or not incident_id:
            return take_charge_reply({"result": "unknown"}, "")
        contact = self.engine.contact_by_chat(chat_id)
        if contact is None:
            return "Este chat no está entre los contactos de la alarma."
        result = await self.engine.take_charge(incident_id, contact["id"], "Telegram")
        return take_charge_reply(result, contact["name"])

    @staticmethod
    def _milestones(data: dict[str, Any]) -> list[tuple[str, str]]:
        drill = "Simulacro: " if data.get("kind") == "drill" else ""
        out = []
        if data.get("status") == "acknowledged" and data.get("acknowledged_by"):
            out.append(("ack", f"✅ {drill}{data['acknowledged_by']} ha aceptado gestionar la alarma. "
                               "Ya no se avisará a nadie más; sigue activa hasta que se desarme."))
        elif data["phase"] != "calling" and data.get("status") == "trunk_down":
            out.append(("noack", f"⚠️ {drill}No se ha podido llamar a nadie: la línea telefónica no funciona, "
                                 "y nadie ha aceptado gestionar la alarma."))
        elif data["phase"] != "calling" and data.get("status") in ("no_answer", "timeout", "failed"):
            out.append(("noack", f"⚠️ {drill}Nadie ha aceptado gestionar la alarma ({STATUS_TEXT.get(data['status'], data['status'])})."))
        if any(e.get("kind") == "trunk_down" for e in data.get("events") or []):
            out.append(("line", f"📵 {drill}La línea telefónica no funciona: no se puede llamar. Se avisa por notificaciones."))
        if any(e.get("kind") == "trunk_ok" for e in data.get("events") or []):
            out.append(("line_ok", f"📞 {drill}La línea telefónica vuelve a funcionar."))
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
                    attachments.append((photo_filename(data, e), photo))
        for c in to:
            try:
                await self.mail(c["email"], subject, plain, body, attachments)
                log.info("[%s] %s email sent to %s", inc.id, "final" if final else "first", c["name"])
            except Exception as err:  # noqa: BLE001
                self._failed(inc, f"correo a {c['name']}", err)

    # ---------------------------------------------------------- resend
    async def resend(self, data: dict[str, Any], contact_ids: list[str]) -> dict[str, Any]:
        """Send an incident's report again (panel button): the Telegram timeline with
        its photos (videos follow in the background) and the final email."""
        chosen = [c for c in self.store.contacts() if c["id"] in set(contact_ids)]
        chosen = [c for c in chosen if c["email"] or c["telegram_chat_id"]]
        if not chosen:
            raise RuntimeError("Elige al menos un contacto con correo o Telegram.")
        sent: list[str] = []
        errors: list[str] = []
        settings = self.store.settings()
        photos = [(e, self.media.read(data["id"], e["media"]["snapshot"]))
                  for e in data.get("events") or []
                  if self.media is not None and (e.get("media") or {}).get("snapshot") and settings["send_photos_external"]]
        photos = [(e, p) for e, p in photos if p]
        tg = self.telegram()
        for c in chosen:
            if c["telegram_chat_id"]:
                if tg is None:
                    errors.append(f"{c['name']}: falta el token del bot")
                else:
                    try:
                        await tg.send(c["telegram_chat_id"], telegram_text(data), html_mode=True)
                        for e, photo in photos:
                            await tg.photo(c["telegram_chat_id"], photo, media_caption(data, e, "📷"))
                        sent.append(f"Telegram a {c['name']}")
                    except Exception as err:  # noqa: BLE001
                        errors.append(f"Telegram a {c['name']}: {err}")
            if c["email"]:
                subject, plain, body = email_parts(data, final=True)
                attachments = [(photo_filename(data, e), p) for e, p in photos[:MAX_PHOTOS_EMAIL]]
                try:
                    await self.mail(c["email"], "Reenvío · " + subject, plain, body, attachments)
                    sent.append(f"correo a {c['name']}")
                except Exception as err:  # noqa: BLE001
                    errors.append(f"correo a {c['name']}: {err}")
        chats = [c for c in chosen if c["telegram_chat_id"]]
        if tg is not None and chats and settings["send_videos_telegram"] and self.frigate is not None and self.media is not None:
            for e in data.get("events") or []:
                event_id = (e.get("media") or {}).get("event_id")
                if event_id:
                    self._spawn(self._send_clip(data, tg, e, event_id, chats))
        return {"sent": sent, "errors": errors}

    async def _send_clip(self, data: dict[str, Any], tg: Any, entry: dict[str, Any], event_id: str,
                         chats: list[dict[str, Any]]) -> None:
        try:
            path = self.media.clip_path(event_id)
            if not path.exists():
                def fetch() -> None:
                    with self.frigate.open(event_id, "clip.mp4") as resp:
                        self.media.cache_clip(event_id, resp)
                await asyncio.to_thread(fetch)
            clip = path.read_bytes()
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] clip %s not available: %s", data.get("id"), event_id, err)
            return
        if len(clip) > TELEGRAM_VIDEO_MAX:
            return
        for c in chats:
            try:
                await tg.video(c["telegram_chat_id"], clip, media_caption(data, entry, "🎬"))
            except Exception as err:  # noqa: BLE001
                log.warning("[%s] video by Telegram to %s failed: %s", data.get("id"), c["name"], err)

    @staticmethod
    def _failed(inc: Any, what: str, err: Exception) -> None:
        log.warning("[%s] %s failed: %s", inc.id, what, err)
        # Shown in the timeline; no listener loop: adding it only re-renders Telegram.
        inc.add("notify_error", f"No se pudo enviar {what}", str(err)[:200])


# ---------------------------------------------------------------- panel tools
class PanelTools:
    """What the panel's buttons call (on the asyncio loop)."""

    def __init__(self, store: Any, notifier: Any = None) -> None:
        self.store = store
        self.notifier = notifier  # its poller reads the bot's updates: the chats come from it

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
            fresh = [] if self.notifier is not None else await bot.recent_chats()
        except NotifyError as err:
            raise RuntimeError(f"Telegram responde: {err}") from err
        seen = list((self.notifier.seen_chats if self.notifier is not None else {}).values())
        chats = {c["id"]: {k: c[k] for k in ("id", "name", "type")} for c in seen + fresh}
        return {"bot": me.get("username") or "", "chats": list(chats.values())}
