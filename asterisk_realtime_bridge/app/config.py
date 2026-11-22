"""Add-on configuration loading and validation (/data/options.json)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from .pins import MODE_GLOBAL, PIN_MODES, PinBook, PinEntry, parse_numbers

# Default prompt template. It is also pre-filled in the add-on options, so it can
# be edited there; an empty option falls back to this built-in version.
#
# Placeholders (anything else in braces is left untouched):
#   {assistant_name}  the `assistant_name` option
#   {language}        the `language` option (code, e.g. "es")
#   {language_name}   the language name (e.g. "Spanish")
#   {accent_line}     the `accent` option as a line; dropped when empty
#                     (use it on a line of its own)
#   {pin_rules}       the PIN instructions; dropped when the PIN is disabled
#                     (use it on a line of its own)
DEFAULT_PROMPT = """\
You are {assistant_name}, the voice assistant of this home. You are speaking on the phone, so keep your replies short and natural: no lists, no long explanations.

Language: always speak {language_name} (language code "{language}"), starting with your very first words, unless the caller clearly speaks another language.
{accent_line}

Security:
{pin_rules}
- For locks, alarms, doors, gates and garage doors always ask for explicit confirmation ("do you really want me to open it?") before acting.

Home:
- For any action or question about the home, use the home tool with ONE clear request in natural language, for example "turn on the living room light" or "is the garage door open?". Write the request in {language_name} (language code "{language}"), because that is the language Home Assistant understands.
- Tell the caller the result in your own words. If the tool returns an error, say so honestly. Never invent states or actions.

Call:
- If the caller says goodbye or needs nothing else, say a brief goodbye and call the hang_up tool.
- If you are interrupted, stop talking and listen.
"""

PIN_RULES = (
    "- Before doing anything else, the caller must verify their PIN. Ask for it at "
    "the start and use the verify_pin tool with the digits they say. Until the PIN "
    "is correct, do nothing else and reveal nothing about the home.\n"
    "- If the PIN is wrong, say so and ask again. Never reveal or hint at the PIN."
)

# Incoming calls that cannot change anything (caller without a PIN, or the PIN
# mode is not complete): questions only, with the read-only home_status tool.
INBOUND_QUERY_RULES = (
    "- In this call you can ONLY answer questions about the state of the home, "
    "with the home_status tool. You cannot change anything and there is no PIN "
    "or any other way to unlock changes. If they ask to change something, say "
    "that it cannot be done from this call."
)

# Outbound calls: the bridge placed the call to deliver a notice. Their rules
# replace {pin_rules} and are appended after the prompt, so that they are there
# with any custom prompt.
PROFILE_QUERY = "QueryOnly"
PROFILE_MANAGE = "Management"
PROFILE_INBOUND = "InboundCall"  # only used in events: incoming calls
PROFILES = (PROFILE_QUERY, PROFILE_MANAGE)

CONFIRM_MODES = ("implicit", "voice", "dtmf", "voice_or_dtmf")
CONFIRM_KEYS = "0123456789*#"

_KEY_NAMES = {"#": "hash (#)", "*": "star (*)"}


def key_name(key: str) -> str:
    return _KEY_NAMES.get(key, key)


def outbound_rules(
    profile: str,
    *,
    pins: str,
    confirm: str = "implicit",
    confirm_key: str = "1",
    confirm_text: str = "",
) -> str:
    """Rules for an outbound call.

    ``pins`` is "open" (no PIN configured at all: anything goes), "pin" (a PIN
    can unlock changes) or "locked" (no PIN applies to this call).
    """
    rules = [
        "- This is an OUTBOUND call: you called this person to deliver a notice. "
        "The person who answers may not be who was expected.",
        "- If you hear a voicemail greeting or nobody responds, read the notice "
        "once and call the hang_up tool.",
    ]
    ask_also = f' When you ask, also tell them: "{confirm_text}"' if confirm_text else ""
    key = key_name(confirm_key)
    if confirm == "implicit":
        rules.append(
            "- Do not ask for an explicit confirmation. As soon as the person shows "
            "that they heard the notice (they reply, ask something, say ok or "
            "goodbye), call the confirm_notice tool once. A voicemail greeting or "
            "silence is not a confirmation."
        )
    elif confirm == "voice":
        rules.append(
            "- After the notice, ask them explicitly to confirm that they have "
            f"received it.{ask_also} Only a clear yes counts: then call the "
            "confirm_notice tool once. A question, a doubt or a voicemail is not a "
            "confirmation."
        )
    elif confirm == "dtmf":
        rules.append(
            f"- After the notice, ask them to press the {key} key on the phone "
            f"keypad to confirm that they have received it.{ask_also} Only the key "
            "counts, not words: the system detects it and tells you."
        )
    else:  # voice_or_dtmf
        rules.append(
            "- After the notice, ask them to confirm that they have received it, "
            f"by saying so or by pressing the {key} key.{ask_also} If they confirm "
            "with a clear yes, call the confirm_notice tool once; if they press the "
            "key, the system tells you. A question, a doubt or a voicemail is not a "
            "confirmation."
        )
    if profile == PROFILE_MANAGE and pins == "open":
        rules.append(
            "- Answer questions with the home_status tool; make changes with the home tool."
        )
    elif profile == PROFILE_MANAGE and pins == "pin":
        rules.append(
            "- Questions about the state of the home can be answered right away "
            "with the home_status tool.\n"
            "- To CHANGE anything (turn on or off, open, close, arm, disarm...) the "
            "person must first verify their PIN with the verify_pin tool; then use "
            "the home tool. Ask for the PIN only when they want to change something. "
            "Never reveal or hint at the PIN."
        )
    else:  # QueryOnly, or Management without a PIN that applies to this call
        rules.append(
            "- In this call you can ONLY answer questions about the state of the "
            "home, with the home_status tool. You cannot change anything and there "
            "is no PIN or any other way to unlock changes. If they ask to change "
            "something, say that it cannot be done from this call."
        )
    rules.append(STATUS_READING_RULES)
    return "\n".join(rules)


# How to read the home_status tool (outbound calls).
STATUS_READING_RULES = (
    "- home_status returns the current state of the entities you may talk about. "
    "Call it EVERY time before saying how something is now: never answer from "
    "memory, things change during the call. Use its search argument with one word "
    "(for example \"garaje\") to narrow the list.\n"
    "- Reading it: \"now\" is the current local time; \"since\" is when that state "
    "began. Values that are dates are local times: compare them with \"now\" to say "
    "how long ago. A date in the year 2000 or earlier means never. For on/off "
    "states use device_class: door, garage_door, window or opening on = open; "
    "motion, occupancy or presence on = detected. unavailable or unknown = no data.\n"
    "- If something is not in the list, say you have no information about it. "
    "Never guess or invent a state."
)


_LANGUAGE_NAMES = {
    "ar": "Arabic", "ca": "Catalan", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "eu": "Basque", "fi": "Finnish",
    "fr": "French", "gl": "Galician", "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "nl": "Dutch", "no": "Norwegian",
    "nb": "Norwegian", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sv": "Swedish", "tr": "Turkish", "uk": "Ukrainian",
    "zh": "Chinese",
}


def language_name(code: str) -> str:
    """Human-readable language name for an ISO code such as "es" or "es-ES"."""
    base = code.strip().lower().replace("_", "-").split("-")[0]
    return _LANGUAGE_NAMES.get(base, code)


def render_prompt(
    template: str,
    *,
    assistant_name: str,
    language: str,
    accent: str,
    pin_enabled: bool,
    pin_rules: str | None = None,
) -> str:
    """Fill the placeholders of a prompt template.

    Block placeholders (``{pin_rules}``, ``{accent_line}``) sitting alone on a
    line are replaced by their text, or the whole line is dropped when empty.
    Unknown placeholders and any other braces are left as they are.
    """
    values = {
        "assistant_name": assistant_name,
        "language": language,
        "language_name": language_name(language),
    }
    blocks = {
        "pin_rules": pin_rules if pin_rules is not None else (PIN_RULES if pin_enabled else ""),
        "accent_line": f"Accent and dialect: {accent}" if accent else "",
    }
    lines: list[str] = []
    for line in template.splitlines():
        stripped = line.strip()
        if stripped in ("{pin_rules}", "{accent_line}"):
            if blocks[stripped[1:-1]]:
                lines.append(blocks[stripped[1:-1]])
            continue
        for key, value in {**values, **blocks}.items():
            line = line.replace("{" + key + "}", value)
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


# Retired by OpenAI on 2027-01-20.
DEPRECATED_MODELS = {"gpt-realtime", "gpt-realtime-mini"}

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    """Invalid configuration (the add-on stops with a clear message)."""


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    language: str = "en"
    assistant_name: str = "Assistant"
    accent: str = ""
    prompt: str = ""
    extra_instructions: str = ""
    pin_enabled: bool = True
    # "global" (one PIN: ``pin``) or "individual" (one PIN per person: ``pins``).
    pin_mode: str = MODE_GLOBAL
    pin: str = ""
    # Personal PINs (individual mode): PinEntry, or (person, digits) pairs.
    pins: tuple[Any, ...] = ()
    max_pin_attempts: int = 3
    max_call_seconds: int = 300
    # Seconds without the caller speaking or pressing a key (after the assistant
    # has finished) before asking again; after silence_reprompts, hang up. 0 = off.
    silence_seconds: int = 8
    silence_reprompts: int = 2
    # The Asterisk app (slug), to stop a withdrawn outbound call from ringing.
    asterisk_app: str = "3e533915_asterisk"
    conversation_agent: str = "conversation.home_assistant"
    fallback_agent: str = ""
    # Read-only scope for outbound calls without a PIN (home_status tool):
    # labels to include (empty = entities exposed to Assist) and labels,
    # entity ids or device names to exclude. Add-on options only, never per call.
    query_include: str = ""
    query_exclude: str = ""
    # Fallback when OpenAI is unavailable: a Home Assistant TTS entity
    # (e.g. tts.piper) and an optional voice. Empty = no fallback.
    fallback_tts: str = ""
    fallback_tts_voice: str = ""
    # Periodic OpenAI check (minutes; 0 = only at start-up).
    openai_check_minutes: int = 15
    # When OpenAI is unavailable, converse locally (STT -> Assist -> TTS)
    # instead of only reading notices. Needs fallback_tts.
    local_conversation: bool = False
    local_stt: str = "stt.faster_whisper"
    vad: str = "server_vad"
    vad_threshold: float = 0.5
    vad_silence_ms: int = 500
    noise_reduction: str = "near_field"
    max_concurrent_calls: int = 1
    port: int = 9092
    bind_address: str = "127.0.0.1"
    log_level: str = "info"
    log_transcripts: bool = False
    # Advanced settings, only through environment variables (useful for tests).
    realtime_url: str = "wss://api.openai.com/v1/realtime"
    ha_api_urls: tuple[str, ...] = (
        "http://supervisor/core/api",
        "http://172.30.32.2/core/api",
    )
    ha_token: str = ""

    @property
    def language_name(self) -> str:
        return language_name(self.language)

    @cached_property
    def pin_book(self) -> PinBook:
        entries = [
            e if isinstance(e, PinEntry) else PinEntry(person=str(e[0]), pin=str(e[1]))
            for e in self.pins
        ]
        return PinBook.build(self.pin_enabled, self.pin_mode, self.pin, entries)

    @property
    def instructions(self) -> str:
        return self.instructions_for(None, self.pin_book.for_inbound().state)

    def instructions_for(self, call: Any = None, pins: str = "open") -> str:
        """Instructions for an incoming call (``call`` None) or an outbound call.

        ``call`` is an ``OutboundCall``; ``pins`` is described in ``outbound_rules``.
        """
        name = (getattr(call, "assistant_name", "") or self.assistant_name) if call else self.assistant_name
        if call is not None:
            rules_block = ""
        else:
            rules_block = {"pin": PIN_RULES, "locked": INBOUND_QUERY_RULES}.get(pins, "")
        text = render_prompt(
            self.prompt.strip() or DEFAULT_PROMPT,
            assistant_name=name,
            language=self.language,
            accent=self.accent,
            pin_enabled=pins == "pin",
            pin_rules=rules_block,
        )
        if call is None and pins == "locked":
            text += "\nReading the home state:\n" + STATUS_READING_RULES + "\n"
        if call is not None:
            rules = outbound_rules(
                call.profile,
                pins=pins,
                confirm=call.confirm,
                confirm_key=call.confirm_key,
                confirm_text=call.confirm_text,
            )
            text += "\nThis call (these rules take precedence):\n" + rules + "\n"
            # The notice also lives here, not only in the greeting: if the person
            # talks over the greeting, the model must still know what to say.
            text += f'\nThe notice to deliver in this call:\n"{call.message}"\n'
        if self.extra_instructions.strip():
            text += "\nAdditional instructions:\n" + self.extra_instructions.strip() + "\n"
        return text


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _personal_pins(value: object) -> tuple[PinEntry, ...]:
    """The ``pins`` option. Only its shape is checked here; the PIN rules are
    applied by ``PinBook`` (an invalid PIN disables Management, it does not stop
    the add-on)."""
    if not value:
        return ()
    if not isinstance(value, list):
        raise ConfigError("pins must be a list of {person, pin, numbers} entries.")
    entries: list[PinEntry] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("pins must be a list of {person, pin, numbers} entries.")
        entries.append(
            PinEntry(
                person=str(item.get("person") or "").strip().lower(),
                pin=str(item.get("pin") or "").strip(),
                name=str(item.get("name") or "").strip(),
                numbers=parse_numbers(item.get("numbers")),
            )
        )
    return tuple(entries)


def load(path: str | None = None) -> Config:
    options_path = Path(path or os.environ.get("BRIDGE_OPTIONS", "/data/options.json"))
    try:
        raw = json.loads(options_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as err:
        raise ConfigError(f"{options_path} is not valid JSON: {err}") from err

    def text(key: str, default: str) -> str:
        value = raw.get(key)
        return default if value is None else str(value).strip()

    defaults = Config(openai_api_key="")
    api_key = text("openai_api_key", "")
    if not api_key:
        raise ConfigError("openai_api_key is missing from the add-on configuration.")

    pin_enabled = _as_bool(raw.get("pin_enabled"), True)
    pin_mode = text("pin_mode", MODE_GLOBAL).lower()
    if pin_mode not in PIN_MODES:
        raise ConfigError(f"pin_mode must be one of {', '.join(PIN_MODES)}.")
    pin = text("pin", "") if pin_enabled else ""
    pins = _personal_pins(raw.get("pins")) if pin_enabled else ()

    model = text("model", defaults.model)
    if model.lower().startswith("gpt-live"):
        raise ConfigError(
            f"{model} uses the GPT-Live API (/v1/live/sessions), which has a "
            "different protocol and is not implemented by this bridge yet. Use a "
            "gpt-realtime-* model, for example gpt-realtime-2.1 or "
            "gpt-realtime-2.1-mini."
        )
    if not model.lower().startswith("gpt-realtime"):
        raise ConfigError(
            f"Unsupported model: {model!r}. It must be a Realtime API model "
            "(gpt-realtime-2.1, gpt-realtime-2.1-mini, gpt-realtime-2, ...)."
        )

    vad = text("vad", defaults.vad)
    if vad not in ("server_vad", "semantic_vad"):
        raise ConfigError("vad must be server_vad or semantic_vad.")
    noise = text("noise_reduction", defaults.noise_reduction)
    if noise not in ("near_field", "far_field", "off"):
        raise ConfigError("noise_reduction must be near_field, far_field or off.")

    ha_urls = defaults.ha_api_urls
    if os.environ.get("HA_API_URL"):
        ha_urls = (os.environ["HA_API_URL"].rstrip("/"),)

    return Config(
        openai_api_key=api_key,
        model=model,
        voice=text("voice", defaults.voice),
        language=text("language", defaults.language),
        assistant_name=text("assistant_name", defaults.assistant_name),
        accent=text("accent", ""),
        # The options form may be a single-line field: accept a literal \n too.
        prompt=text("prompt", "").replace("\\n", "\n"),
        extra_instructions=text("extra_instructions", ""),
        pin_enabled=pin_enabled,
        pin_mode=pin_mode,
        pin=pin,
        pins=pins,
        max_pin_attempts=int(raw.get("max_pin_attempts", defaults.max_pin_attempts)),
        max_call_seconds=int(raw.get("max_call_seconds", defaults.max_call_seconds)),
        silence_seconds=max(0, int(raw.get("silence_seconds", defaults.silence_seconds))),
        silence_reprompts=max(0, int(raw.get("silence_reprompts", defaults.silence_reprompts))),
        asterisk_app=str(raw.get("asterisk_app") if raw.get("asterisk_app") is not None
                         else defaults.asterisk_app).strip(),
        conversation_agent=text("conversation_agent", defaults.conversation_agent),
        fallback_agent=text("fallback_agent", ""),
        query_include=text("query_include", ""),
        query_exclude=text("query_exclude", ""),
        fallback_tts=text("fallback_tts", ""),
        fallback_tts_voice=text("fallback_tts_voice", ""),
        openai_check_minutes=max(0, min(1440, int(raw.get("openai_check_minutes", 15)))),
        local_conversation=_as_bool(raw.get("local_conversation"), False),
        local_stt=text("local_stt", "stt.faster_whisper"),
        vad=vad,
        vad_threshold=float(raw.get("vad_threshold", defaults.vad_threshold)),
        vad_silence_ms=int(raw.get("vad_silence_ms", defaults.vad_silence_ms)),
        noise_reduction=noise,
        max_concurrent_calls=int(
            raw.get("max_concurrent_calls", defaults.max_concurrent_calls)
        ),
        port=int(raw.get("port", defaults.port)),
        bind_address=text("bind_address", defaults.bind_address),
        log_level=text("log_level", defaults.log_level).lower(),
        log_transcripts=_as_bool(raw.get("log_transcripts"), False),
        realtime_url=os.environ.get("OPENAI_REALTIME_URL", defaults.realtime_url),
        ha_api_urls=ha_urls,
        ha_token=os.environ.get("SUPERVISOR_TOKEN", ""),
    )
