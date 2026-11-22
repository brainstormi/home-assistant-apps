import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.server import App, serve  # noqa: E402
from app.store import PinStatus, Store  # noqa: E402


class FakeHA:
    def __init__(self):
        self.pin_mode = "individual"
        self.pin_persons = ["person.marta"]

    def states(self):
        return [
            {"entity_id": "person.luis", "state": "home", "attributes": {"friendly_name": "Luis"}},
            {"entity_id": "person.marta", "state": "home", "attributes": {"friendly_name": "Marta"}},
            {"entity_id": "binary_sensor.ami_connected", "state": "on", "attributes": {}},
        ]

    def persons(self, states=None):
        return [{"entity_id": "person.luis", "name": "Luis"}, {"entity_id": "person.marta", "name": "Marta"}]

    def notify_targets(self):
        return ["mobile_app_phone"]

    def status(self, states=None, entities=None):
        return {"ami": {"entity_id": "binary_sensor.ami_connected", "state": "on", "since": None}}

    def pin_status(self, entity_id, states=None):
        return PinStatus(self.pin_mode, self.pin_persons)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Store(Path(self.tmp.name) / "contact_center.json"), FakeHA())
        self.server = serve(self.app, host="127.0.0.1", port=0, allow_any_client=True)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"null")

    def test_index_and_static(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            self.assertIn(b"Alarm Contact Center", resp.read())
        with urllib.request.urlopen(self.base + "/static/app.js") as resp:
            self.assertEqual(resp.status, 200)

    def test_path_traversal_blocked(self):
        status, _ = self.call("GET", "/static/..%2F..%2Fstore.py")
        self.assertEqual(status, 404)

    def test_person_name_comes_from_ha(self):
        status, body = self.call("POST", "/api/contacts", {"kind": "person", "person": "person.marta", "name": "Otro", "phone": "600111222"})
        self.assertEqual(status, 201)
        self.assertEqual(body["name"], "Marta")

    def test_manage_without_pin_rejected(self):
        status, body = self.call("POST", "/api/contacts", {"kind": "person", "person": "person.luis", "phone": "600111222", "can_manage": True})
        self.assertEqual(status, 422)
        self.assertIn("can_manage", body["fields"])

    def test_crud_and_state(self):
        status, a = self.call("POST", "/api/contacts", {"kind": "external", "name": "Pablo", "phone": "+34 600 111 222"})
        self.assertEqual(status, 201)
        self.assertEqual(a["phone"], "600111222")
        status, _ = self.call("PUT", f"/api/contacts/{a['id']}", {"kind": "external", "name": "Pablo B", "phone": "600111222"})
        self.assertEqual(status, 200)
        status, state = self.call("GET", "/api/state")
        self.assertEqual(state["contacts"][0]["name"], "Pablo B")
        self.assertEqual(state["pins"]["mode"], "individual")
        status, _ = self.call("DELETE", f"/api/contacts/{a['id']}")
        self.assertEqual(status, 200)
        status, _ = self.call("DELETE", f"/api/contacts/{a['id']}")
        self.assertEqual(status, 404)

    def test_export_import_roundtrip(self):
        self.call("POST", "/api/contacts", {"kind": "external", "name": "Pablo", "phone": "600111222"})
        status, exported = self.call("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertEqual(exported["app"], "alarm_contact_center")
        self.call("DELETE", f"/api/contacts/{exported['contacts'][0]['id']}")
        status, res = self.call("POST", "/api/import", exported)
        self.assertEqual((status, res["contacts"]), (200, 1))
        status, state = self.call("GET", "/api/state")
        self.assertEqual(state["contacts"][0]["name"], "Pablo")

    def test_import_rejects_bad_file(self):
        status, _ = self.call("POST", "/api/import", {"foo": 1})
        self.assertEqual(status, 400)
        bad = {"app": "alarm_contact_center", "settings": {}, "contacts": [{"kind": "external", "name": "", "phone": "1"}]}
        status, body = self.call("POST", "/api/import", bad)
        self.assertEqual(status, 422)

    def test_import_never_grants_manage_without_pin(self):
        data = {"app": "alarm_contact_center", "settings": {}, "contacts": [
            {"kind": "person", "person": "person.luis", "phone": "600111222", "can_manage": True}]}
        status, body = self.call("POST", "/api/import", data)
        self.assertEqual(status, 422)

    def test_chain_endpoints_without_engine(self):
        status, body = self.call("GET", "/api/incidents")
        self.assertEqual((status, body["current"]), (200, None))
        status, _ = self.call("POST", "/api/test", {"contact_id": "x"})
        self.assertEqual(status, 409)

    def test_settings_validation(self):
        status, body = self.call("PUT", "/api/settings", {"rounds": 99})
        self.assertEqual(status, 422)
        self.assertIn("rounds", body["fields"])


class ClientFilterTests(unittest.TestCase):
    def test_only_ingress_proxy(self):
        tmp = tempfile.TemporaryDirectory()
        app = App(Store(Path(tmp.name) / "a.json"), FakeHA())
        server = serve(app, host="127.0.0.1", port=0, allow_any_client=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/state")
            self.assertEqual(ctx.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "video/mp4", "Content-Length": str(len(body))}
        self._pos = 0

    def read(self, n=-1):
        chunk = self.body[self._pos:] if n < 0 else self.body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeFrigate:
    def __init__(self):
        self.opened = []
        self.base = "http://frigate.test:5000"

    def open(self, event_id, name, range_header=""):
        self.opened.append((event_id, name, range_header))
        if event_id.endswith("gone00"):
            raise urllib.error.HTTPError("u", 404, "nf", {}, None)
        if range_header:
            return FakeResponse(b"vi", 206, {"Content-Type": "video/mp4", "Content-Range": "bytes 0-1/5", "Content-Length": "2"})
        return FakeResponse(b"video" if name == "clip.mp4" else b"live-jpg",
                            headers={"Content-Type": "video/mp4" if name == "clip.mp4" else "image/jpeg"})


class MediaRouteTests(unittest.TestCase):
    def setUp(self):
        from app.media import MediaStore

        self.tmp = tempfile.TemporaryDirectory()
        self.media = MediaStore(Path(self.tmp.name) / "media")
        self.ha = FakeFrigate()
        self.app = App(Store(Path(self.tmp.name) / "c.json"), FakeHA(), media=self.media, frigate=self.ha)
        self.server = serve(self.app, host="127.0.0.1", port=0, allow_any_client=True)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def test_snapshot_copy_then_live(self):
        ev = "1795354401.831865-vqtljq"
        self.media.save("inc123", ev, b"copy-jpg")
        self.assertEqual(self.get(f"/api/media/inc123/{ev}.jpg")[1], b"copy-jpg")
        self.assertEqual(self.ha.opened, [])
        status, body, headers = self.get(f"/api/media/other/{ev}.jpg")
        self.assertEqual((status, body, headers["Content-Type"]), (200, b"live-jpg", "image/jpeg"))
        self.assertEqual(self.get(f"/api/snapshot/{ev}.jpg")[1], b"live-jpg")

    def test_clip_cached_and_ranged(self):
        ev = "1795354401.831865-vqtljq"
        status, body, headers = self.get(f"/api/clip/{ev}.mp4", {"Range": "bytes=0-1"})
        self.assertEqual((status, body, headers["Content-Range"]), (206, b"vi", "bytes 0-1/5"))
        self.assertEqual(self.get(f"/api/clip/{ev}.mp4")[1], b"video")
        self.assertEqual(self.get(f"/api/clip/{ev}.mp4", {"Range": "bytes=-3"})[1], b"deo")
        self.assertEqual(self.get(f"/api/clip/{ev}.mp4", {"Range": "bytes=9-"})[0], 416)
        self.assertEqual(len(self.ha.opened), 1)  # downloaded once
        self.assertEqual(self.get("/api/clip/1795354401.8-gone00.mp4")[0], 404)

    def test_bad_ids_rejected(self):
        self.assertEqual(self.get("/api/clip/..%2F..%2Fetc.mp4")[0], 400)
        self.assertEqual(self.get("/api/snapshot/hola.jpg")[0], 400)
        self.assertEqual(self.ha.opened, [])

    def test_state_has_frigate_block(self):
        status, body, _ = self.get("/api/state")
        self.assertEqual(json.loads(body)["frigate"], {"cameras": [], "last": None})

    def test_no_frigate(self):
        self.app.frigate = None
        self.assertEqual(self.get("/api/clip/1795354401.8-abcdef.mp4")[0], 404)
