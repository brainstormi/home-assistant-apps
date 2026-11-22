"""App configuration: contacts and settings, stored under /data.

Standard library only, so it can be tested without installing anything.

Security rules enforced here, not just in the UI:
- "Can manage" is only allowed for contacts linked to a Home Assistant person.
- When the Bridge uses individual PINs, that person must also have a PIN.

User-facing messages (validation errors, warnings) are in Spanish on purpose:
they are shown as-is in the panel.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

CONFIRM_MODES = ("voice_or_dtmf", "dtmf", "voice")
TRUNK_TEST_MODES = ("daily", "every_6h", "weekly", "off")
MESSAGE_VARIABLES = ("{nombre}", "{sensor}", "{zona}", "{hora}", "{atendido}")

DEFAULT_SETTINGS: dict[str, Any] = {
    "message": (
        "Hola {nombre}. Ha saltado la alarma en casa: {sensor}, a las {hora}. {atendido}"
    ),
    "confirm_text": "Si confirmas que lo atiendes, dejaré de avisar al resto de contactos.",
    # Grace period after the alarm triggers: if it is disarmed in time (the usual
    # "I came home and disarmed it a few seconds late"), nobody is called.
    "call_delay": 30,
    # Same idea for external contacts (Telegram, email). HA app notifications to
    # the household are always immediate.
    "external_notify_delay": 30,
    # Voicemail (Bridge 0.9.1+, OpenAI engine): the assistant recognises it, leaves
    # voicemail_message after the beep and the call counts as "did not confirm".
    "detect_voicemail": True,
    "voicemail_message": "Hola {nombre}. Ha saltado la alarma en casa: {sensor}, a las {hora}. Por favor, revisa tus notificaciones o llama a casa.",
    # Full passes over the list; the chain is circular (after the last contact it
    # goes back to the first). 0 = no limit: until someone confirms or the alarm
    # is disarmed, capped by max_chain_minutes.
    "rounds": 2,
    "max_chain_minutes": 60,
    "answer_timeout": 60,
    "round_pause_min": 2,
    "trunk_test": "daily",
    "trunk_test_hour": 4,
    # The line's own number: the test calls it through the trunk and it must come
    # back in. Empty = no real test (only the registration is watched).
    "trunk_test_number": "",
    "trunk_alert_after_min": 2,
    # Unstable registration: this many drops within this window alert even when
    # each drop is too short for trunk_alert_after_min.
    "flap_alert_count": 3,
    "flap_window_min": 60,
    "trunk_entity": "binary_sensor.siptrunk_registered",
    "ami_entity": "binary_sensor.ami_connected",
    "frigate_pre_minutes": 5,
    "post_minutes": 10,  # the incident closes this long after the disarm (0 = at once)
    "max_open_minutes": 120,  # ...or this long after the trigger if nobody disarms
    "media_retention_days": 90,
    "send_photos_external": True,
    "send_videos_telegram": True,
    "notify_url": "",  # dashboard path the HA app notification opens (e.g. /alarma/incidente)
    "incident_entities": [],
    "person_entities": [],  # Frigate person helpers (input_text); empty = detect them
    "frigate_url": "",  # Frigate's API address; empty = taken from the Frigate integration
    # Email (the App's own SMTP account) and Telegram (the App's own bot).
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",  # starttls, ssl or none
    "smtp_user": "",
    "smtp_password": "",  # secret
    "smtp_from": "",  # sender address; empty = the user
    "smtp_from_name": "Alarma de casa",  # sender name shown in the inbox
    "telegram_token": "",  # secret
    # Empty means DEFAULT_PIN_ENTITY (what the Bridge publishes out of the box).
    "bridge_pin_entity": "",
    # How this installation is wired together.
    "alarm_entity": "alarm_control_panel.alarmo",
    "call_script": "script.llamada_saliente",
    # Empty means default_bridge_addon(): same repository, same prefix as this App.
    "bridge_addon": "",
}

DEFAULT_PIN_ENTITY = "sensor.asterisk_realtime_bridge_pin"
BRIDGE_SLUG = "asterisk_realtime_bridge"


def default_bridge_addon(hostname: str | None = None) -> str:
    """The Bridge App id, derived from this App's own id.

    Both Apps come from the same repository, so they share the repository prefix.
    The Supervisor names this container ``<prefix>-alarm-contact-center`` (HOSTNAME),
    which gives ``<prefix>_asterisk_realtime_bridge``. Empty when it cannot be worked
    out (e.g. running outside Home Assistant): then it has to be set in the panel.
    """
    host = hostname if hostname is not None else os.environ.get("HOSTNAME", "")
    prefix, sep, rest = host.partition("-")
    if not sep or not rest or not re.fullmatch(r"[a-z0-9]+", prefix):
        return ""
    return f"{prefix}_{BRIDGE_SLUG}"


def bridge_addon(settings: dict[str, Any]) -> str:
    """The Bridge App id to use: the one set in the panel, else the derived one."""
    return str(settings.get("bridge_addon") or "") or default_bridge_addon()

DEFAULT_CONTACT: dict[str, Any] = {
    "id": "",
    "kind": "external",  # "person" (Home Assistant person) or "external" (external contact)
    "person": "",  # person entity_id, only when kind == "person"
    "name": "",  # for "person" it is taken from the person's name in HA
    "phone": "",
    "email": "",
    "telegram_chat_id": "",
    "notify_service": "",  # e.g. "mobile_app_phone"; HA persons only
    "attempts": 1,
    "retry_wait": 60,
    "confirm": "voice_or_dtmf",
    "can_manage": False,
    "active": True,
}

_PHONE_RE = re.compile(r"^[6789]\d{8}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CHAT_RE = re.compile(r"^-?\d{3,20}$")
_PERSON_RE = re.compile(r"^person\.[a-z0-9_]+$")
_NOTIFY_RE = re.compile(r"^[a-z0-9_]+$")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class ValidationError(Exception):
    """Invalid data. ``errors`` maps field -> message, to show next to each form field."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def normalize_phone(raw: str) -> str:
    """Return the number as the trunk dials it: 9 digits, without the +34 prefix."""
    digits = re.sub(r"[\s\-().]", "", str(raw or ""))
    if digits.startswith("+34"):
        digits = digits[3:]
    elif digits.startswith("0034"):
        digits = digits[4:]
    return digits


def _int_in(value: Any, low: int, high: int, field: str, errors: dict[str, str]) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors[field] = "Tiene que ser un número entero."
        return low
    if not low <= number <= high:
        errors[field] = f"Entre {low} y {high}."
    return number


class PinStatus:
    """What the Bridge publishes about PINs (never the PINs themselves).

    mode: "global", "individual" or "unknown" (the Bridge does not publish it yet).
    persons: entity_ids of the persons that have a PIN (individual mode).
    """

    def __init__(
        self,
        mode: str = "unknown",
        persons: list[str] | None = None,
        complete: bool | None = None,
        management: str = "",
        issues: list[str] | None = None,
    ) -> None:
        self.mode = mode
        self.persons = set(persons or [])
        # Diagnostics reported by the Bridge, shown as-is in the panel.
        self.complete = complete
        self.management = management
        self.issues = list(issues or [])

    def allows(self, person: str) -> bool | None:
        """True/False when known; None when the Bridge does not report it yet."""
        if self.mode == "global":
            return True
        if self.mode == "individual":
            return person in self.persons
        return None


def clean_contact(data: dict[str, Any], pins: PinStatus, others: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate and normalize a contact. Raises ValidationError with the failing fields."""
    errors: dict[str, str] = {}
    c = copy.deepcopy(DEFAULT_CONTACT)
    for key in c:
        if key in data:
            c[key] = data[key]

    c["id"] = str(c["id"] or uuid.uuid4().hex[:12])
    c["kind"] = c["kind"] if c["kind"] in ("person", "external") else "external"
    c["name"] = str(c["name"] or "").strip()[:60]
    c["person"] = str(c["person"] or "").strip()
    c["email"] = str(c["email"] or "").strip()
    c["telegram_chat_id"] = str(c["telegram_chat_id"] or "").strip()
    c["notify_service"] = str(c["notify_service"] or "").strip().removeprefix("notify.")
    c["phone"] = normalize_phone(c["phone"])
    c["active"] = bool(c["active"])
    c["can_manage"] = bool(c["can_manage"])

    if c["kind"] == "person":
        if not _PERSON_RE.match(c["person"]):
            errors["person"] = "Elige una persona de Home Assistant."
        elif any(o["kind"] == "person" and o["person"] == c["person"] and o["id"] != c["id"] for o in others):
            errors["person"] = "Esta persona ya está en la lista."
    else:
        c["person"] = ""
        c["notify_service"] = ""
        if not c["name"]:
            errors["name"] = "Escribe un nombre."

    if c["phone"] and not _PHONE_RE.match(c["phone"]):
        errors["phone"] = "Teléfono español de 9 cifras (con o sin +34)."
    if c["email"] and not _EMAIL_RE.match(c["email"]):
        errors["email"] = "Correo no válido."
    if c["telegram_chat_id"] and not _CHAT_RE.match(c["telegram_chat_id"]):
        errors["telegram_chat_id"] = "El chat de Telegram es un número."
    if c["notify_service"] and not _NOTIFY_RE.match(c["notify_service"]):
        errors["notify_service"] = "Servicio de notificación no válido."

    c["attempts"] = _int_in(c["attempts"], 1, 5, "attempts", errors)
    c["retry_wait"] = _int_in(c["retry_wait"], 10, 600, "retry_wait", errors)
    if c["confirm"] not in CONFIRM_MODES:
        errors["confirm"] = "Forma de confirmar no válida."

    if c["can_manage"]:
        if c["kind"] != "person":
            errors["can_manage"] = "Solo las personas de Home Assistant pueden gestionar."
        elif c["person"] and pins.allows(c["person"]) is False:
            errors["can_manage"] = "Esta persona no tiene PIN en el Bridge."
        elif not c["phone"]:
            errors["can_manage"] = "Para gestionar durante la llamada hace falta un teléfono."

    if errors:
        raise ValidationError(errors)
    return c


def clean_settings(data: dict[str, Any]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    s = copy.deepcopy(DEFAULT_SETTINGS)
    for key in s:
        if key in data:
            s[key] = data[key]

    s["message"] = str(s["message"] or "").strip()
    if not s["message"]:
        errors["message"] = "El mensaje no puede estar vacío."
    elif len(s["message"]) > 600:
        errors["message"] = "Máximo 600 caracteres."
    s["confirm_text"] = str(s["confirm_text"] or "").strip()[:300]
    s["detect_voicemail"] = bool(s["detect_voicemail"])
    s["voicemail_message"] = str(s["voicemail_message"] or "").strip()
    if len(s["voicemail_message"]) > 300:
        errors["voicemail_message"] = "Máximo 300 caracteres: tiene que caber antes de que el buzón corte."

    s["call_delay"] = _int_in(s["call_delay"], 0, 600, "call_delay", errors)
    s["external_notify_delay"] = _int_in(s["external_notify_delay"], 0, 600, "external_notify_delay", errors)
    s["rounds"] = _int_in(s["rounds"], 0, 10, "rounds", errors)
    s["max_chain_minutes"] = _int_in(s["max_chain_minutes"], 5, 240, "max_chain_minutes", errors)
    s["answer_timeout"] = _int_in(s["answer_timeout"], 20, 300, "answer_timeout", errors)
    s["round_pause_min"] = _int_in(s["round_pause_min"], 0, 30, "round_pause_min", errors)
    if s["trunk_test"] not in TRUNK_TEST_MODES:
        errors["trunk_test"] = "Frecuencia no válida."
    s["trunk_alert_after_min"] = _int_in(s["trunk_alert_after_min"], 1, 60, "trunk_alert_after_min", errors)
    s["trunk_test_hour"] = _int_in(s["trunk_test_hour"], 0, 23, "trunk_test_hour", errors)
    s["flap_alert_count"] = _int_in(s["flap_alert_count"], 2, 20, "flap_alert_count", errors)
    s["flap_window_min"] = _int_in(s["flap_window_min"], 10, 1440, "flap_window_min", errors)
    s["trunk_test_number"] = normalize_phone(s["trunk_test_number"])
    if s["trunk_test_number"] and not _PHONE_RE.match(s["trunk_test_number"]):
        errors["trunk_test_number"] = "Número español de 9 cifras (con o sin +34)."
    for key in ("trunk_entity", "ami_entity"):
        s[key] = str(s[key] or "").strip()
        if not (s[key].startswith("binary_sensor.") and _ENTITY_RE.match(s[key])):
            errors[key] = "Tiene que ser una entidad binary_sensor.xxx."
    s["frigate_pre_minutes"] = _int_in(s["frigate_pre_minutes"], 0, 30, "frigate_pre_minutes", errors)
    s["post_minutes"] = _int_in(s["post_minutes"], 0, 60, "post_minutes", errors)
    s["max_open_minutes"] = _int_in(s["max_open_minutes"], 15, 720, "max_open_minutes", errors)
    s["media_retention_days"] = _int_in(s["media_retention_days"], 7, 365, "media_retention_days", errors)
    s["send_photos_external"] = bool(s["send_photos_external"])
    s["send_videos_telegram"] = bool(s["send_videos_telegram"])
    s["notify_url"] = str(s["notify_url"] or "").strip()
    if s["notify_url"] and not re.match(r"^/[A-Za-z0-9_\-./?=&#%]*$", s["notify_url"]):
        errors["notify_url"] = "Tiene que ser una ruta de Home Assistant que empiece por /, p. ej. /alarma/incidente."

    entities = s["incident_entities"]
    if not isinstance(entities, list):
        entities = []
    entities = [str(e).strip() for e in entities if str(e).strip()]
    bad = [e for e in entities if not _ENTITY_RE.match(e)]
    if bad:
        errors["incident_entities"] = "Entidades no válidas: " + ", ".join(bad[:3])
    s["incident_entities"] = list(dict.fromkeys(entities))[:50]

    helpers = s["person_entities"]
    if not isinstance(helpers, list):
        helpers = []
    helpers = [str(e).strip() for e in helpers if str(e).strip()]
    bad = [e for e in helpers if not (e.startswith("input_text.") and _ENTITY_RE.match(e))]
    if bad:
        errors["person_entities"] = "Tienen que ser helpers input_text: " + ", ".join(bad[:3])
    s["person_entities"] = list(dict.fromkeys(helpers))[:20]

    s["smtp_host"] = str(s["smtp_host"] or "").strip()
    if s["smtp_host"] and not re.match(r"^[A-Za-z0-9.-]+$", s["smtp_host"]):
        errors["smtp_host"] = "Servidor no válido (p. ej. smtp.gmail.com)."
    s["smtp_port"] = _int_in(s["smtp_port"], 1, 65535, "smtp_port", errors)
    if s["smtp_security"] not in ("starttls", "ssl", "none"):
        s["smtp_security"] = "starttls"
    s["smtp_user"] = str(s["smtp_user"] or "").strip()
    s["smtp_password"] = str(s["smtp_password"] or "")
    s["smtp_from"] = str(s["smtp_from"] or "").strip()
    if s["smtp_from"] and not _EMAIL_RE.match(s["smtp_from"]):
        errors["smtp_from"] = "Correo no válido."
    s["smtp_from_name"] = str(s["smtp_from_name"] or "").strip().replace("\n", " ")[:60]
    s["telegram_token"] = str(s["telegram_token"] or "").strip()
    if s["telegram_token"] and not re.match(r"^\d{5,}:[A-Za-z0-9_-]{30,}$", s["telegram_token"]):
        errors["telegram_token"] = "No parece un token de bot (lo da @BotFather: 123456:ABC…)."

    s["frigate_url"] = str(s["frigate_url"] or "").strip().rstrip("/")
    if s["frigate_url"] and not re.match(r"^https?://[^\s/]+(:\d+)?(/[^\s]*)?$", s["frigate_url"]):
        errors["frigate_url"] = "Tiene que ser una dirección como http://servidor:5000."

    s["bridge_pin_entity"] = str(s["bridge_pin_entity"] or "").strip()
    if s["bridge_pin_entity"] and not _ENTITY_RE.match(s["bridge_pin_entity"]):
        errors["bridge_pin_entity"] = "Entidad no válida."

    for key, prefix in (("alarm_entity", "alarm_control_panel."), ("call_script", "script.")):
        s[key] = str(s[key] or "").strip()
        if not (s[key].startswith(prefix) and _ENTITY_RE.match(s[key])):
            errors[key] = f"Tiene que ser una entidad {prefix}xxx."
    s["bridge_addon"] = str(s["bridge_addon"] or "").strip()
    if s["bridge_addon"] and not re.match(r"^[a-z0-9]+_[a-z0-9_]+$", s["bridge_addon"]):
        errors["bridge_addon"] = "Identificador de App no válido (p. ej. abcd1234_asterisk_realtime_bridge). Déjalo vacío para calcularlo solo."

    if errors:
        raise ValidationError(errors)
    return s


def contact_warnings(c: dict[str, Any], pins: PinStatus) -> list[str]:
    """Non-blocking warnings worth showing in the panel."""
    out: list[str] = []
    if not (c["phone"] or c["email"] or c["telegram_chat_id"] or c["notify_service"]):
        out.append("No tiene teléfono, correo, Telegram ni notificación de la app: no recibirá ningún aviso.")
    if c["can_manage"] and c["person"] and pins.allows(c["person"]) is None:
        out.append("No se puede comprobar si tiene PIN: el Bridge aún no publica esa información.")
    if c["can_manage"] and c["person"] and pins.allows(c["person"]) is False:
        out.append("Ya no tiene PIN en el Bridge: las llamadas saldrán en solo consulta.")
    return out


SECRET_KEYS = ("smtp_password", "telegram_token")


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings for the panel and exports: secrets blanked, with whether each is set."""
    out = copy.deepcopy(settings)
    out["secrets_set"] = {k: bool(out.get(k)) for k in SECRET_KEYS}
    for k in SECRET_KEYS:
        out[k] = ""
    return out


class Store:
    """Contacts and settings in a JSON file, written atomically."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, json.JSONDecodeError):
            # Keep the damaged file next to it so it can be inspected.
            backup = self.path.with_suffix(".damaged.json")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            raw = {}
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update({k: v for k, v in (raw.get("settings") or {}).items() if k in settings})
        contacts = []
        for item in raw.get("contacts") or []:
            c = copy.deepcopy(DEFAULT_CONTACT)
            c.update({k: v for k, v in item.items() if k in c})
            if c["id"]:
                contacts.append(c)
        return {"version": SCHEMA_VERSION, "settings": settings, "contacts": contacts}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # -------------------------------------------------------------------- read
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def contacts(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data["contacts"])

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data["settings"])

    # ------------------------------------------------------------------- write
    def save_contact(self, data: dict[str, Any], pins: PinStatus) -> dict[str, Any]:
        with self._lock:
            contacts = self._data["contacts"]
            cid = str(data.get("id") or "")
            current = next((c for c in contacts if c["id"] == cid), None)
            if cid and current is None:
                raise KeyError(cid)
            clean = clean_contact(data, pins, contacts)
            if current is None:
                contacts.append(clean)
            else:
                contacts[contacts.index(current)] = clean
            self._save()
            return copy.deepcopy(clean)

    def delete_contact(self, cid: str) -> None:
        with self._lock:
            before = len(self._data["contacts"])
            self._data["contacts"] = [c for c in self._data["contacts"] if c["id"] != cid]
            if len(self._data["contacts"]) == before:
                raise KeyError(cid)
            self._save()

    def reorder(self, ids: list[str]) -> None:
        with self._lock:
            by_id = {c["id"]: c for c in self._data["contacts"]}
            if sorted(ids) != sorted(by_id):
                raise ValidationError({"order": "La lista no coincide con los contactos guardados."})
            self._data["contacts"] = [by_id[i] for i in ids]
            self._save()

    def replace_all(self, settings: dict[str, Any], contacts: list[dict[str, Any]]) -> None:
        """Replace everything at once (import). Inputs must already be validated."""
        with self._lock:
            self._data["settings"] = copy.deepcopy(settings)
            self._data["contacts"] = copy.deepcopy(contacts)
            self._save()

    def save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        """Merge and save. Secrets: "" or missing keeps the stored one, None removes it.
        Returns the settings without secrets (what the panel may see)."""
        data = dict(data)
        for key in SECRET_KEYS:
            if key in data and data[key] is None:
                data[key] = ""
            elif key in data and data[key] == "":
                del data[key]
        with self._lock:
            merged = {**self._data["settings"], **data}
            clean = clean_settings(merged)
            self._data["settings"] = clean
            self._save()
            return public_settings(clean)
