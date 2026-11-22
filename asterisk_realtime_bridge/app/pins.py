"""PIN modes and who may change things in each call.

Modes (``pin_mode``, mutually exclusive):

* ``global``: one PIN (``pin``) for ``Management`` calls and incoming calls.
  Personal PINs are ignored.
* ``individual``: one PIN per Home Assistant person (``pins``: ``person`` is the
  ``person.*`` entity id). The global PIN is ignored. PINs must be digits only,
  at least ``MIN_INDIVIDUAL_DIGITS`` long, not trivial (``111111``,
  ``123456``, ``121212``...) and different for every person.
* ``off`` (``pin_enabled: false``): no PIN at all.

A mode is *complete* when it can be used as configured: ``global`` with a valid
global PIN; ``individual`` with at least one valid personal PIN and no invalid
entry. While it is incomplete (for example right after switching modes),
``Management`` calls run as query-only and incoming calls are query-only too.

Each call gets a ``CallAuth``: ``open`` (no PIN needed), ``pin`` (these PINs
unlock changes) or ``locked`` (query only, nothing can change). PIN digits never
leave this module except to be compared.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

MODE_GLOBAL = "global"
MODE_INDIVIDUAL = "individual"
MODE_OFF = "off"
PIN_MODES = (MODE_GLOBAL, MODE_INDIVIDUAL)
MIN_INDIVIDUAL_DIGITS = 6
PROFILE_MANAGE = "Management"
PROFILE_QUERY = "QueryOnly"

_PERSON_RE = re.compile(r"^person\.[a-z0-9_]+$")


@dataclass(frozen=True)
class PinEntry:
    """One personal PIN as configured (not validated yet)."""

    person: str = ""  # person.* entity id
    pin: str = ""
    name: str = ""  # optional label (older configurations: the only key)
    numbers: tuple[str, ...] = ()  # phone numbers of this person (digits)

    @property
    def label(self) -> str:
        return self.person or self.name or "?"


@dataclass(frozen=True)
class CallAuth:
    """What a call may do."""

    state: str  # "open", "pin" or "locked"
    pins: tuple[tuple[str, str], ...] = ()  # (who, digits); who "" = global PIN
    profile: str = ""  # effective profile of an outbound call
    person: str = ""  # person entity (or name) this call is for / from
    reason: str = ""  # why changes were turned off, when they were

    @property
    def locked(self) -> bool:
        return self.state == "locked"


def _slug(name: str) -> str:
    """"José García" -> "jose_garcia" (how Home Assistant names person entities)."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_")


def digits_of(number: str) -> str:
    return "".join(ch for ch in str(number) if ch.isdigit())


def parse_numbers(value: Any) -> tuple[str, ...]:
    """Phone numbers from a text such as "600111222, +34 600 333 444"."""
    if not value:
        return ()
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;/\n]", str(value))
    out = []
    for item in items:
        digits = digits_of(item)
        if digits and digits not in out:
            out.append(digits)
    return tuple(out)


def same_number(a: str, b: str) -> bool:
    """Two phone numbers are the same, with or without international prefix."""
    a, b = digits_of(a), digits_of(b)
    if not a or not b:
        return False
    a, b = a.removeprefix("00"), b.removeprefix("00")
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 9 and longer.endswith(shorter)


def pin_problem(pin: str, min_digits: int = MIN_INDIVIDUAL_DIGITS) -> str | None:
    """Why a PIN is not acceptable, or None. Never includes the PIN itself."""
    if not pin:
        return "no PIN"
    if not pin.isdigit():
        return "the PIN must contain digits only"
    if len(pin) < min_digits:
        return f"the PIN must have at least {min_digits} digits"
    if len(set(pin)) == 1:
        return "the PIN repeats one digit"
    steps = {(int(b) - int(a)) % 10 for a, b in zip(pin, pin[1:])}
    if steps in ({1}, {9}):
        return "the PIN is a sequence"
    for period in range(1, len(pin) // 2 + 1):
        if len(pin) % period == 0 and pin == pin[:period] * (len(pin) // period):
            return "the PIN repeats a pattern"
    return None


@dataclass
class PinBook:
    """The PIN configuration, validated, and the auth of each call."""

    mode: str = MODE_OFF
    global_pin: str = ""
    entries: tuple[PinEntry, ...] = ()  # valid personal PINs (individual mode)
    issues: tuple[str, ...] = ()  # problems found (no PINs in the text)
    ignored: tuple[str, ...] = ()  # notes about settings the mode ignores
    people: dict[str, str] = field(default_factory=dict)  # name (lower) -> person id
    unknown: tuple[str, ...] = ()  # configured persons that Home Assistant does not have

    # ------------------------------------------------------------ building
    @classmethod
    def build(
        cls, enabled: bool, mode: str, global_pin: str, entries: Iterable[PinEntry]
    ) -> "PinBook":
        entries = tuple(entries)
        if not enabled:
            return cls(mode=MODE_OFF)
        mode = mode if mode in PIN_MODES else MODE_GLOBAL
        issues: list[str] = []
        ignored: list[str] = []
        if mode == MODE_GLOBAL:
            if entries:
                ignored.append(f"{len(entries)} personal PIN(s) ignored in global mode")
            # The strict rules are for personal PINs; the global PIN keeps the
            # rule it always had (digits only).
            problem = "no PIN" if not global_pin else (
                None if global_pin.isdigit() else "the PIN must contain digits only")
            if problem:
                issues.append(f"global PIN: {problem}")
                global_pin = ""
            return cls(mode=mode, global_pin=global_pin, issues=tuple(issues), ignored=tuple(ignored))
        if global_pin:
            ignored.append("the global PIN is ignored in individual mode")
        valid: list[PinEntry] = []
        seen_people: set[str] = set()
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.pin] = counts.get(entry.pin, 0) + 1
        for entry in entries:
            person = entry.person.strip().lower()
            if not _PERSON_RE.match(person):
                issues.append(f"{entry.label}: 'person' must be a person entity id (person.xxx)")
                continue
            if person in seen_people:
                issues.append(f"{person}: appears more than once")
                continue
            seen_people.add(person)
            problem = pin_problem(entry.pin)
            if problem is None and counts[entry.pin] > 1:
                problem = "the same PIN as another person"
            if problem:
                issues.append(f"{person}: {problem}")
                continue
            valid.append(PinEntry(person=person, pin=entry.pin, name=entry.name, numbers=entry.numbers))
        if not valid and not issues:
            issues.append("no personal PIN configured")
        return cls(mode=mode, entries=tuple(valid), issues=tuple(issues), ignored=tuple(ignored))

    # ---------------------------------------------------------- properties
    @property
    def complete(self) -> bool:
        if self.mode == MODE_OFF:
            return True
        if self.mode == MODE_GLOBAL:
            return bool(self.global_pin)
        return bool(self.entries) and not self.issues and not self.unknown

    @property
    def management_enabled(self) -> bool:
        return self.mode == MODE_OFF or self.complete

    @property
    def people_with_pin(self) -> list[str]:
        return [e.person for e in self.entries]

    def learn_people(self, states: Iterable[dict[str, Any]]) -> None:
        """Friendly names of the Home Assistant persons, to match ``person`` by name."""
        people: dict[str, str] = {}
        for state in states:
            entity_id = str(state.get("entity_id", ""))
            if entity_id.startswith("person."):
                name = str((state.get("attributes") or {}).get("friendly_name") or "").strip().lower()
                people[entity_id] = entity_id
                if name:
                    people[name] = entity_id
        self.people = people
        if people:  # only when the persons could be read
            self.unknown = tuple(e.person for e in self.entries if e.person not in people)

    def entry_for(self, person: str) -> PinEntry | None:
        key = person.strip().lower()
        if not key:
            return None
        entity = self.people.get(key) or (key if key.startswith("person.") else "person." + _slug(key))
        for entry in self.entries:
            if entry.person == entity or (entry.name and entry.name.lower() == key):
                return entry
        return None

    def entry_for_number(self, number: str) -> PinEntry | None:
        for entry in self.entries:
            if any(same_number(number, n) for n in entry.numbers):
                return entry
        return None

    # ---------------------------------------------------------------- calls
    def for_outbound(self, call: Any) -> CallAuth:
        person = getattr(call, "person", "") or ""
        if call.profile != PROFILE_MANAGE:
            return CallAuth("locked", profile=PROFILE_QUERY, person=person)
        if self.mode == MODE_OFF:
            return CallAuth("open", profile=PROFILE_MANAGE, person=person)
        if not self.complete:
            return CallAuth("locked", profile=PROFILE_QUERY, person=person,
                            reason=f"PIN mode {self.mode} is not complete")
        if self.mode == MODE_GLOBAL:
            return CallAuth("pin", (("", self.global_pin),), PROFILE_MANAGE, person)
        entry = self.entry_for(person)
        if entry is None:
            return CallAuth("locked", profile=PROFILE_QUERY, person=person,
                            reason=f"no personal PIN for {person or 'the person called'}")
        return CallAuth("pin", ((entry.person, entry.pin),), PROFILE_MANAGE, entry.person)

    def for_inbound(self, number: str = "") -> CallAuth:
        if self.mode == MODE_OFF:
            return CallAuth("open")
        if not self.complete:
            return CallAuth("locked", reason=f"PIN mode {self.mode} is not complete")
        if self.mode == MODE_GLOBAL:
            return CallAuth("pin", (("", self.global_pin),))
        entry = self.entry_for_number(number) if number else None
        if entry is None:
            return CallAuth("locked", reason="number not linked to a person with a PIN"
                            if number else "caller number unknown")
        return CallAuth("pin", ((entry.person, entry.pin),), person=entry.person)

    # ------------------------------------------------------------- status
    def status(self, all_people: Iterable[str] = ()) -> tuple[str, dict[str, Any]]:
        """State and attributes for Home Assistant. No PIN is ever included."""
        with_pin = self.people_with_pin
        attributes: dict[str, Any] = {
            "friendly_name": "Asterisk Realtime Bridge: PIN",
            "icon": "mdi:shield-key" if self.complete else "mdi:shield-alert",
            "complete": self.complete,
            "management": "enabled" if self.management_enabled else "disabled (query only)",
            "issues": list(self.issues) + [f"{p}: no such person in Home Assistant" for p in self.unknown],
        }
        if self.mode == MODE_INDIVIDUAL:
            attributes["people_with_pin"] = with_pin
            attributes["people_without_pin"] = sorted(p for p in set(all_people) if p not in with_pin)
            attributes["people_with_number"] = [e.person for e in self.entries if e.numbers]
        elif self.mode == MODE_GLOBAL:
            attributes["global_pin"] = "set" if self.global_pin else "missing"
        return self.mode, attributes
