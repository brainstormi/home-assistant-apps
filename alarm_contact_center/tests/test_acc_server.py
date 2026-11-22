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
