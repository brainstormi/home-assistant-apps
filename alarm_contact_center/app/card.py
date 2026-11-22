"""Installs the dashboard card (alarm-contact-center-card.js) into Home Assistant.

At every connection the App:

1. copies the card to ``<config>/www/alarm-contact-center/``, which Home Assistant
   serves at ``/local/alarm-contact-center/`` (the App maps the configuration
   folder for this, and writes nothing else there);
2. registers it as a dashboard resource (``lovelace/resources``), with the App
   version in the URL so browsers and the mobile app load the new card after an
   update.

If the dashboards are in YAML mode, resources cannot be added from outside: the
panel then shows the line to add by hand.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

CARD_FILE = "alarm-contact-center-card.js"
SOURCE = Path(__file__).parent / "card" / CARD_FILE
FOLDER = "alarm-contact-center"
URL_PATH = f"/local/{FOLDER}/{CARD_FILE}"


def config_dir() -> Path:
    return Path(os.environ.get("ACC_HA_CONFIG", "/homeassistant"))


def resource_url(version: str = __version__) -> str:
    return f"{URL_PATH}?v={version}"


class CardInstaller:
    def __init__(self, root: Path | None = None, version: str = __version__) -> None:
        self.root = root or config_dir()
        self.version = version
        self.status: dict[str, Any] = {"file": "pending", "resource": "pending", "url": resource_url(version), "detail": ""}

    def install_file(self) -> bool:
        """Copy the card if it changed. True when the file is in place."""
        if not self.root.is_dir():
            self.status.update(file="no_config", detail="La App no tiene acceso a la configuración de Home Assistant.")
            return False
        www = self.root / "www"
        created_www = not www.exists()
        target = www / FOLDER / CARD_FILE
        content = SOURCE.read_text(encoding="utf-8").replace("__VERSION__", self.version)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                tmp = target.with_suffix(".tmp")
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, target)
                log.info("Dashboard card installed (%s)", self.version)
        except OSError as err:
            self.status.update(file="error", detail=f"No se pudo copiar la tarjeta: {err}")
            return False
        # Home Assistant only serves /local if the www folder existed when it started.
        self.status.update(file="restart" if created_www else "ok",
                           detail="Reinicia Home Assistant una vez para que sirva la carpeta www." if created_www else "")
        return True

    async def register(self, conn: Any) -> None:
        """Add or update the dashboard resource."""
        url = resource_url(self.version)
        try:
            resources = await conn.command({"type": "lovelace/resources"}, timeout=15) or []
        except Exception as err:  # noqa: BLE001 - YAML mode, or no dashboards
            self.status.update(resource="manual", detail=f"Añade el recurso a mano ({err}).")
            return
        mine = [r for r in resources if str(r.get("url", "")).split("?", 1)[0] == URL_PATH]
        try:
            if not mine:
                await conn.command({"type": "lovelace/resources/create", "res_type": "module", "url": url}, timeout=15)
                log.info("Dashboard card registered as a resource")
            elif mine[0].get("url") != url or mine[0].get("type") != "module":
                await conn.command({"type": "lovelace/resources/update", "resource_id": mine[0]["id"],
                                    "res_type": "module", "url": url}, timeout=15)
                log.info("Dashboard card resource updated to %s", self.version)
            for extra in mine[1:]:
                await conn.command({"type": "lovelace/resources/delete", "resource_id": extra["id"]}, timeout=15)
        except Exception as err:  # noqa: BLE001
            self.status.update(resource="manual", detail=f"No se pudo registrar la tarjeta: {err}. Añade el recurso a mano.")
            return
        self.status["resource"] = "ok"

    async def run(self, conn: Any) -> None:
        if self.install_file():
            await self.register(conn)
