"""Read-only view of the home for outbound calls without a verified PIN.

Home Assistant has no read-only Assist: an agent that can see the exposed
entities can also control them. So, for questions asked without a PIN, the
bridge builds the list itself and gives it to the model through a tool that
can only read.

Scope (add-on options only, never per call):
* ``query_include``: labels, comma separated. Empty = the entities exposed to
  Assist (the same scope you already control by voice).
* ``query_exclude``: labels, entity ids or device names to remove.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)

REGISTRY_TTL_S = 60.0
MAX_ENTITIES = 200


def parse_list(text: str) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _norm(text: str) -> str:
    """Case- and accent-insensitive key ("Cámaras" == "camaras")."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip().lower()


@dataclass
class Registries:
    entities: list[dict[str, Any]] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)
    exposed: set[str] = field(default_factory=set)


def select_entities(
    include: list[str], exclude: list[str], reg: Registries, existing: set[str]
) -> list[str]:
    """Entity ids in scope, sorted. Only entities that currently have a state count."""
    label_ids: dict[str, str] = {}
    for label in reg.labels:
        label_ids[_norm(label.get("label_id", ""))] = label.get("label_id", "")
        label_ids[_norm(label.get("name", ""))] = label.get("label_id", "")
    device_labels = {d.get("id"): set(d.get("labels") or []) for d in reg.devices}
    device_ids: dict[str, str] = {}
    for device in reg.devices:
        for name in (device.get("name_by_user"), device.get("name"), device.get("id")):
            if name:
                device_ids[_norm(name)] = device["id"]

    def labels_of(entry: dict[str, Any]) -> set[str]:
        return set(entry.get("labels") or []) | device_labels.get(entry.get("device_id"), set())

    if include:
        wanted = {label_ids[_norm(x)] for x in include if _norm(x) in label_ids}
        unknown = [x for x in include if _norm(x) not in label_ids]
        if unknown:
            log.warning("query_include: unknown label(s) ignored: %s", ", ".join(unknown))
        selected = {e["entity_id"] for e in reg.entities if labels_of(e) & wanted}
    else:
        selected = set(reg.exposed)

    by_id = {e["entity_id"]: e for e in reg.entities}
    for token in exclude:
        # An exclusion applies to everything it names (a label and a device can
        # share a name): excluding too much is the safe side.
        key = _norm(token)
        matched = False
        if key in label_ids:
            label = label_ids[key]
            selected = {i for i in selected if label not in labels_of(by_id.get(i, {}))}
            matched = True
        if key in device_ids:
            device = device_ids[key]
            selected = {i for i in selected if by_id.get(i, {}).get("device_id") != device}
            matched = True
        if "." in token:
            selected.discard(token.strip().lower())
            matched = True
        if not matched:
            log.warning("query_exclude: %r is not a known label, device or entity id; ignored", token)
    return sorted(i for i in selected if i in existing)


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute ago"
    if minutes < 120:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} days ago"


def describe(states: list[dict[str, Any]], now: datetime, search: str = "") -> dict[str, Any]:
    """The tool result: current time and one compact line per entity."""
    key = _norm(search)
    items: list[dict[str, Any]] = []
    for st in states:
        attrs = st.get("attributes") or {}
        name = attrs.get("friendly_name") or st.get("entity_id", "")
        if key and key not in _norm(name) and key not in _norm(st.get("entity_id", "")):
            continue
        item: dict[str, Any] = {"name": name, "entity_id": st.get("entity_id"), "state": st.get("state")}
        if attrs.get("device_class"):
            item["device_class"] = attrs["device_class"]
        if attrs.get("unit_of_measurement"):
            item["unit"] = attrs["unit_of_measurement"]
        changed = _parse_time(st.get("last_changed", ""))
        if changed is not None and now.tzinfo is not None:
            local = changed.astimezone(now.tzinfo)
            item["since"] = f"{local:%Y-%m-%d %H:%M} ({_ago((now - local).total_seconds())})"
        items.append(item)
    result: dict[str, Any] = {
        "now": f"{now:%Y-%m-%d %H:%M}" if now.tzinfo else now.isoformat(),
        "entities": items[:MAX_ENTITIES],
    }
    if len(items) > MAX_ENTITIES:
        result["note"] = f"Only the first {MAX_ENTITIES} of {len(items)} entities; ask with a search word."
    if key and not items:
        result["note"] = "Nothing matches that search; try another word or no search."
    return result


class _HA(Protocol):
    async def ws(self, command: str, **kwargs: Any) -> Any: ...
    async def get(self, path: str) -> Any: ...
    async def render(self, template: str) -> str: ...


class HomeStatus:
    """Builds the read-only list. Registries are cached; states are always fresh."""

    def __init__(self, ha: _HA, include: str, exclude: str) -> None:
        self._ha = ha
        self._include = parse_list(include)
        self._exclude = parse_list(exclude)
        self._reg: Registries | None = None
        self._reg_time = 0.0

    @property
    def scope(self) -> str:
        if self._include:
            return "labels " + ", ".join(self._include)
        return "entities exposed to Assist"

    async def _registries(self) -> Registries:
        if self._reg is not None and time.monotonic() - self._reg_time < REGISTRY_TTL_S:
            return self._reg
        entities = await self._ha.ws("config/entity_registry/list")
        devices = await self._ha.ws("config/device_registry/list")
        labels = await self._ha.ws("config/label_registry/list")
        exposed: set[str] = set()
        if not self._include:
            listing = await self._ha.ws("homeassistant/expose_entity/list")
            for entity_id, assistants in ((listing or {}).get("exposed_entities") or {}).items():
                if (assistants or {}).get("conversation"):
                    exposed.add(entity_id)
        self._reg = Registries(entities or [], devices or [], labels or [], exposed)
        self._reg_time = time.monotonic()
        return self._reg

    async def scope_ids(self) -> set[str]:
        reg = await self._registries()
        states = await self._ha.get("states")
        existing = {s["entity_id"] for s in states or [] if "entity_id" in s}
        return set(select_entities(self._include, self._exclude, reg, existing))

    async def allows(self, entity_ids: list[str]) -> bool:
        """True if every entity is in the read-only scope (an empty list is allowed)."""
        if not entity_ids:
            return True
        return set(entity_ids) <= await self.scope_ids()

    async def snapshot(self, search: str = "") -> dict[str, Any]:
        reg = await self._registries()
        states = await self._ha.get("states")
        by_id = {s["entity_id"]: s for s in states or [] if "entity_id" in s}
        ids = select_entities(self._include, self._exclude, reg, set(by_id))
        now_text = (await self._ha.render("{{ now().isoformat() }}")).strip()
        now = _parse_time(now_text) or datetime.now().astimezone()
        return describe([by_id[i] for i in ids], now, search)


__all__ = ["HomeStatus", "Registries", "describe", "parse_list", "select_entities"]
