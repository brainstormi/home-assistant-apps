"""Panel server (Ingress): static page + JSON API.

Only accepts connections from the Supervisor Ingress proxy (172.30.32.2), which
already requires a Home Assistant login and, with ``panel_admin``, an admin user.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import mimetypes
import os
import re
import threading
import urllib.error
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .ha import HomeAssistant
from .store import DEFAULT_CONTACT, ValidationError, Store, clean_contact, clean_settings, contact_warnings, default_bridge_addon

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INGRESS_PROXY = "172.30.32.2"
MAX_BODY = 256 * 1024
EVENT_RE = re.compile(r"^[0-9]{9,11}(\.[0-9]+)?-[a-z0-9]{4,10}$")  # Frigate event id


_CLIP_LOCKS: dict[str, threading.Lock] = {}
_CLIP_LOCKS_GUARD = threading.Lock()


def _clip_lock(event_id: str) -> threading.Lock:
    """One download per clip, however many range requests arrive at once."""
    with _CLIP_LOCKS_GUARD:
        if len(_CLIP_LOCKS) > 200:
            _CLIP_LOCKS.clear()
        return _CLIP_LOCKS.setdefault(event_id, threading.Lock())


class App:
    """API logic, kept apart from the HTTP server so it can be tested."""

    def __init__(self, store: Store, ha: HomeAssistant, engine: Any = None, loop: asyncio.AbstractEventLoop | None = None,
                 incidents: Any = None, trunk: Any = None, media: Any = None, watcher: Any = None,
                 frigate: Any = None) -> None:
        self.store = store
        self.frigate = frigate  # FrigateAPI: snapshots and clips
        self.media = media  # MediaStore with the incidents' snapshot copies
        self.watcher = watcher  # EntityWatcher: re-subscribed when the settings change
        self.trunk = trunk
        self.ha = ha
        self.engine = engine  # None in phase-1 style tests: chain features answer 503
        self.loop = loop
        self.incidents = incidents

    def _on_loop(self, method: str, *args: Any, timeout: float = 10, target: Any = None) -> Any:
        """Run an engine (or trunk monitor) coroutine on the asyncio loop from an HTTP thread."""
        target = self.engine if target is None else target
        if target is None or self.loop is None:
            raise RuntimeError("La cadena de avisos no está disponible.")
        fut: Future = asyncio.run_coroutine_threadsafe(getattr(target, method)(*args), self.loop)
        return fut.result(timeout)

    def trunk_test(self) -> dict[str, Any]:
        if self.trunk is None:
            raise RuntimeError("La vigilancia de la troncal no está disponible.")
        return self._on_loop("run_test", True, timeout=120, target=self.trunk)

    def start_test(self, contact_id: str) -> dict[str, Any]:
        return self._on_loop("start_test", contact_id)

    def cancel(self) -> None:
        self._on_loop("cancel")

    def start_drill(self, contact_ids: list[str], sensor: str) -> dict[str, Any]:
        return self._on_loop("start_drill", contact_ids, sensor)

    def simulate_disarm(self) -> None:
        self._on_loop("simulate_disarm")

    def incident_list(self) -> dict[str, Any]:
        current = self.engine.status() if self.engine is not None else None
        items = self.incidents.items() if self.incidents is not None else []
        # Only the engine knows what is really running: nothing in the log is active.
        for item in items:
            item["active"] = False
            until = item.get("recording_until")
            if until and until <= dt.datetime.now().astimezone().isoformat(timespec="seconds"):
                item["recording_until"] = None
        if current:
            items = [current] + [i for i in items if i.get("id") != current["id"]]
        return {"current": current if current and current.get("active") else None, "items": items}

    def export_config(self) -> dict[str, Any]:
        snap = self.store.snapshot()
        return {"app": "alarm_contact_center", "version": __version__, "settings": snap["settings"], "contacts": snap["contacts"]}

    def import_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """Replace contacts and settings with an export. Everything is validated first."""
        if not isinstance(data, dict) or data.get("app") != "alarm_contact_center":
            raise ValueError("No es un fichero exportado de Alarm Contact Center.")
        _, pins = self._context()
        settings = clean_settings(data.get("settings") or {})
        contacts: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for i, raw in enumerate(data.get("contacts") or []):
            try:
                # Imported PIN status may differ from today's: never grant "can manage" blindly.
                contacts.append(clean_contact({k: v for k, v in raw.items() if k in DEFAULT_CONTACT}, pins, contacts))
            except ValidationError as err:
                errors[f"contacto {i + 1}"] = "; ".join(err.errors.values())
        if errors:
            raise ValidationError(errors)
        self.store.replace_all(settings, contacts)
        return {"contacts": len(contacts)}

    def _context(self) -> tuple[list[dict[str, Any]], Any]:
        states = self.ha.states()
        pins = self.ha.pin_status(self.store.settings()["bridge_pin_entity"], states)
        return states, pins

    def state(self) -> dict[str, Any]:
        states, pins = self._context()
        persons = self.ha.persons(states)
        names = {p["entity_id"]: p["name"] for p in persons}
        contacts = []
        for c in self.store.contacts():
            view = dict(c)
            if c["kind"] == "person":
                view["name"] = names.get(c["person"], c["name"] or c["person"])
                view["person_missing"] = c["person"] not in names
            view["warnings"] = contact_warnings(c, pins)
            if view.get("person_missing"):
                view["warnings"].insert(0, "Esta persona ya no existe en Home Assistant.")
            contacts.append(view)
        return {
            "version": __version__,
            "settings": self.store.settings(),
            "contacts": contacts,
            "persons": persons,
            "notify_targets": self.ha.notify_targets(),
            "sensors": self.ha.alarm_sensors(states) if hasattr(self.ha, "alarm_sensors") else [],
            "status": self.ha.status(states, {"trunk": self.store.settings()["trunk_entity"],
                                              "ami": self.store.settings()["ami_entity"]}),
            "trunk": self.trunk.status() if self.trunk is not None else None,
            "frigate": self.frigate_status(),
            "defaults": {"bridge_addon": default_bridge_addon()},
            "pins": {
                "mode": pins.mode,
                "persons": sorted(pins.persons),
                "complete": pins.complete,
                "management": pins.management,
                "issues": pins.issues,
            },
        }

    def save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        saved = self.store.save_settings(data)
        if self.watcher is not None and self.loop is not None:
            try:
                self._on_loop("refresh", target=self.watcher, timeout=15)
            except Exception as err:  # noqa: BLE001 - saving worked; following the new entities can wait for a reconnect
                log.warning("Could not follow the new entities yet: %s", err)
        return saved

    def frigate_status(self) -> dict[str, Any]:
        """Followed person helpers and the last person seen, for the panel."""
        if self.engine is None:
            return {"cameras": [], "last": None}
        fw = self.engine.frigate
        last = fw.latest()
        return {"cameras": [{"entity_id": e, "name": n} for e, n in fw.cameras.items()],
                "configured": bool(self.store.settings()["person_entities"]),
                "api": bool(self.frigate is not None and self.frigate.base),
                "last": last.to_dict() if last else None}

    def save_contact(self, data: dict[str, Any], cid: str | None = None) -> dict[str, Any]:
        _, pins = self._context()
        if cid is not None:
            data = {**data, "id": cid}
        else:
            data = {k: v for k, v in data.items() if k != "id"}
        if data.get("kind") == "person":
            # An HA person's name is never typed by hand: it comes from HA.
            names = {p["entity_id"]: p["name"] for p in self.ha.persons()}
            data["name"] = names.get(str(data.get("person") or ""), "")
        return self.store.save_contact(data, pins)


def make_handler(app: App, allow_any_client: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"AlarmContactCenter/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet unless debug
            log.debug("%s %s", self.address_string(), fmt % args)

        # --------------------------------------------------------- helpers
        def _allowed(self) -> bool:
            if allow_any_client or self.client_address[0] == INGRESS_PROXY:
                return True
            self._send_json({"error": "Solo accesible desde el panel de Home Assistant."}, HTTPStatus.FORBIDDEN)
            return False

        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("Petición demasiado grande.")
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def _send_file(self, path: Path) -> None:
            try:
                resolved = path.resolve()
                resolved.relative_to(STATIC_DIR.resolve())
                body = resolved.read_bytes()
            except (ValueError, OSError):
                self._send_json({"error": "No encontrado."}, HTTPStatus.NOT_FOUND)
                return
            ctype = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _snapshot(self, incident_id: str, event_id: str) -> None:
            """The incident's own copy when there is one, otherwise Frigate's."""
            if not EVENT_RE.match(event_id):
                raise ValueError("Evento no válido.")
            data = app.media.read(incident_id, event_id) if (incident_id and app.media is not None) else None
            if data is None:
                self._proxy_frigate(event_id, "snapshot.jpg")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def _proxy_frigate(self, event_id: str, name: str) -> None:
            """Stream a snapshot or clip from Home Assistant (Range kept for video on iOS)."""
            if not EVENT_RE.match(event_id):
                raise ValueError("Evento no válido.")
            if app.frigate is None:
                self._send_json({"error": "Frigate no está configurado."}, HTTPStatus.NOT_FOUND)
                return
            try:
                resp = app.frigate.open(event_id, name, self.headers.get("Range") or "")
            except urllib.error.HTTPError as err:
                log.warning("Frigate %s for %s: HTTP %s %s", name, event_id, err.code, err.reason)
                status = HTTPStatus.NOT_FOUND if err.code == 404 else HTTPStatus.BAD_GATEWAY
                self._send_json({"error": "Frigate ya no tiene este vídeo." if err.code == 404 else "Frigate no responde."}, status)
                return
            except (urllib.error.URLError, OSError) as err:
                log.warning("Frigate %s for %s: %s", name, event_id, err)
                self._send_json({"error": "Frigate no responde."}, HTTPStatus.BAD_GATEWAY)
                return
            with resp:
                self.send_response(resp.status)
                for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if resp.headers.get(header):
                        self.send_header(header, resp.headers[header])
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                try:
                    while chunk := resp.read(64 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the viewer closed the video

        def _clip(self, event_id: str) -> None:
            """Clips are cached and served with Range: Frigate builds them on the fly and
            ignores Range, and Safari on iOS will not play a video without it."""
            if not EVENT_RE.match(event_id):
                raise ValueError("Evento no válido.")
            if app.media is None:
                self._proxy_frigate(event_id, "clip.mp4")
                return
            path = app.media.clip_path(event_id)
            with _clip_lock(event_id):
                if not path.exists():
                    if app.frigate is None:
                        self._send_json({"error": "Frigate no está configurado."}, HTTPStatus.NOT_FOUND)
                        return
                    try:
                        with app.frigate.open(event_id, "clip.mp4") as resp:
                            app.media.cache_clip(event_id, resp)
                    except urllib.error.HTTPError as err:
                        log.warning("Frigate clip.mp4 for %s: HTTP %s %s", event_id, err.code, err.reason)
                        self._send_json({"error": "Frigate ya no tiene este vídeo." if err.code == 404 else "Frigate no responde."},
                                        HTTPStatus.NOT_FOUND if err.code == 404 else HTTPStatus.BAD_GATEWAY)
                        return
                    except (urllib.error.URLError, OSError, ValueError) as err:
                        log.warning("Frigate clip.mp4 for %s: %s", event_id, err)
                        self._send_json({"error": "Frigate no responde."}, HTTPStatus.BAD_GATEWAY)
                        return
            self._send_ranged(path, "video/mp4")

        def _send_ranged(self, path: Path, ctype: str) -> None:
            size = path.stat().st_size
            start, end = 0, size - 1
            status = HTTPStatus.OK
            m = re.match(r"^bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:  # suffix: the last N bytes
                    start = max(size - int(m.group(2)), 0)
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0 and (chunk := f.read(min(256 * 1024, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_index(self) -> None:
            """index.html points at the static files with the version in the URL, so a
            browser (or the HA mobile app) never keeps an old panel after an update."""
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            for name in ("app.js", "style.css", "theme.js"):
                html = html.replace(f'"static/{name}"', f'"static/{name}?v={__version__}"')
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> list[str]:
            path = self.path.split("?", 1)[0]
            return [p for p in path.split("/") if p]

        def _handle(self, method: str) -> None:
            if not self._allowed():
                return
            parts = self._route()
            try:
                if method == "GET" and not parts:
                    self._send_index()
                elif method == "GET" and parts[0] == "static" and len(parts) == 2:
                    self._send_file(STATIC_DIR / parts[1])
                elif parts[:1] == ["api"]:
                    self._api(method, parts[1:])
                else:
                    self._send_json({"error": "No encontrado."}, HTTPStatus.NOT_FOUND)
            except ValidationError as err:
                self._send_json({"error": "Revisa los campos marcados.", "fields": err.errors}, HTTPStatus.UNPROCESSABLE_ENTITY)
            except KeyError:
                self._send_json({"error": "No existe."}, HTTPStatus.NOT_FOUND)
            except RuntimeError as err:
                self._send_json({"error": str(err)}, HTTPStatus.CONFLICT)
            except (ValueError, json.JSONDecodeError) as err:
                self._send_json({"error": str(err) or "Petición no válida."}, HTTPStatus.BAD_REQUEST)
            except Exception:  # noqa: BLE001
                log.exception("Error handling %s %s", method, self.path)
                self._send_json({"error": "Error interno."}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _api(self, method: str, parts: list[str]) -> None:
            if method == "GET" and parts == ["state"]:
                self._send_json(app.state())
            elif method == "POST" and parts == ["contacts"]:
                self._send_json(app.save_contact(self._read_json()), HTTPStatus.CREATED)
            elif method == "PUT" and len(parts) == 2 and parts[0] == "contacts":
                self._send_json(app.save_contact(self._read_json(), parts[1]))
            elif method == "DELETE" and len(parts) == 2 and parts[0] == "contacts":
                app.store.delete_contact(parts[1])
                self._send_json({"ok": True})
            elif method == "PUT" and parts == ["order"]:
                ids = self._read_json().get("ids")
                if not isinstance(ids, list):
                    raise ValueError("Falta la lista de contactos.")
                app.store.reorder([str(i) for i in ids])
                self._send_json({"ok": True})
            elif method == "PUT" and parts == ["settings"]:
                self._send_json(app.save_settings(self._read_json()))
            elif method == "GET" and parts == ["incidents"]:
                self._send_json(app.incident_list())
            elif method == "POST" and parts == ["test"]:
                cid = str(self._read_json().get("contact_id") or "")
                self._send_json(app.start_test(cid), HTTPStatus.ACCEPTED)
            elif method == "POST" and parts == ["drill"]:
                body = self._read_json()
                ids = body.get("contact_ids")
                if not isinstance(ids, list):
                    raise ValueError("Falta la lista de contactos.")
                self._send_json(app.start_drill([str(i) for i in ids], str(body.get("sensor") or "")), HTTPStatus.ACCEPTED)
            elif method == "POST" and parts == ["drill", "disarm"]:
                app.simulate_disarm()
                self._send_json({"ok": True})
            elif method == "POST" and parts == ["trunk", "test"]:
                self._send_json(app.trunk_test())
            elif method == "POST" and parts == ["cancel"]:
                app.cancel()
                self._send_json({"ok": True})
            elif method == "GET" and len(parts) == 3 and parts[0] == "media" and parts[2].endswith(".jpg"):
                self._snapshot(parts[1], parts[2][:-4])
            elif method == "GET" and len(parts) == 2 and parts[0] == "snapshot" and parts[1].endswith(".jpg"):
                self._snapshot("", parts[1][:-4])
            elif method == "GET" and len(parts) == 2 and parts[0] == "clip" and parts[1].endswith(".mp4"):
                self._clip(parts[1][:-4])
            elif method == "GET" and parts == ["export"]:
                self._send_json(app.export_config())
            elif method == "POST" and parts == ["import"]:
                self._send_json(app.import_config(self._read_json()))
            else:
                self._send_json({"error": "No encontrado."}, HTTPStatus.NOT_FOUND)

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

    return Handler


def serve(app: App, host: str = "0.0.0.0", port: int = 8099, allow_any_client: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app, allow_any_client))
    server.daemon_threads = True
    return server


def main() -> None:
    level = "info"
    try:
        options = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
        level = str(options.get("log_level") or "info")
    except (OSError, json.JSONDecodeError):
        pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    asyncio.run(_run())


async def _run() -> None:
    # Imported here so the panel and its tests work without aiohttp installed.
    from .engine import Engine, IncidentLog
    from .ha_ws import EntityWatcher, HAConnection, HAPort, wire
    from .media import MediaStore
    from .report import Reporter
    from .trunk import TrunkMonitor

    data_dir = Path(os.environ.get("ACC_DATA_DIR", "/data"))
    store = Store(data_dir / "contact_center.json")
    incidents = IncidentLog(data_dir / "incidents.json")
    conn = HAConnection()
    port_ = HAPort(conn, store)
    media = MediaStore(data_dir / "media")
    engine = Engine(store, port_, incidents, media=media)
    trunk = TrunkMonitor(store, port_, incidents, engine, data_dir / "trunk.json")
    watcher = EntityWatcher(conn, engine, store)
    reporter = Reporter(port_.mqtt_publish, incidents)
    reporter.load()
    engine.listeners.append(reporter.on_change)
    loop = asyncio.get_running_loop()
    app = App(store, HomeAssistant(), engine, loop, incidents, trunk, media, watcher, port_.frigate)

    allow_any = os.environ.get("ACC_ALLOW_ANY_CLIENT") == "1"
    port = int(os.environ.get("ACC_PORT", "8099"))
    server = serve(app, port=port, allow_any_client=allow_any)
    threading.Thread(target=server.serve_forever, name="panel", daemon=True).start()
    log.info("Alarm Contact Center %s: panel listening on port %d", __version__, port)
    scheduler = asyncio.create_task(trunk.scheduler())
    cleaner = asyncio.create_task(_clean_media(media, store))
    try:
        await conn.run(lambda: wire(conn, engine, store, trunk, watcher, reporter))
    finally:
        scheduler.cancel()
        cleaner.cancel()
        server.shutdown()
        server.server_close()


async def _clean_media(media: Any, store: Store) -> None:
    """Once a day, delete snapshot copies older than the retention setting."""
    while True:
        try:
            removed = await asyncio.to_thread(media.cleanup, store.settings()["media_retention_days"])
            if removed:
                log.info("Deleted the photos of %d old incident(s)", removed)
        except Exception:  # noqa: BLE001
            log.exception("Media cleanup failed")
        await asyncio.sleep(86400)


if __name__ == "__main__":
    main()
