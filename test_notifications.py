import gc
import http.cookiejar
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import push
import server
import addresses


class NotificationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(server, "DB_PATH", Path(self.temp.name) / "db.sqlite3")
        self.path_patch.start()
        with patch.dict("os.environ", {"ADMIN_PASSWORD": "test-admin-password"}):
            server.initialize()
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ServiceHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.root = f"http://127.0.0.1:{self.http.server_port}"
        self.browser = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.path_patch.stop()
        gc.collect()
        self.temp.cleanup()

    def request(self, path, data=None, method=None):
        request = Request(self.root + path,
                          data=json.dumps(data).encode() if data is not None else None,
                          headers={"Content-Type": "application/json"}, method=method)
        try:
            with self.browser.open(request) as response:
                return response.status, json.loads(response.read() or "null")
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_full_subscription_and_assignment_lifecycle(self):
        self.assertEqual(self.request("/api/push")[0], 401)
        self.assertEqual(self.request("/api/login", {"username": "admin", "password": "test-admin-password"})[0], 200)
        user = self.request("/api/users", {"username": "tech", "password": "test-tech-password", "roles": ["technician"]})[1]["user"]
        self.assertEqual(self.request("/api/logout", {})[0], 204)
        self.request("/api/login", {"username": "tech", "password": "test-tech-password"})
        self.assertFalse(self.request("/api/push")[1]["subscribed"])
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        subscription = {"endpoint": "https://web.push.apple.com/test-only-not-sent", "keys": {
            "auth": push.encode_key(b"a" * 16), "p256dh": push.encode_key(public),
        }}
        self.assertEqual(self.request("/api/push/subscribe", {"subscription": subscription})[0], 200)
        self.assertTrue(self.request("/api/push")[1]["subscribed"])
        self.assertEqual(self.request("/api/push/test", {})[0], 200)
        # A second browser assigns work without signing the technician out.
        tech_browser = self.browser
        self.browser = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request("/api/login", {"username": "admin", "password": "test-admin-password"})
        status, result = self.request("/api/tickets", {
            "customer": "Test", "phone": "123456", "address": "Test street",
            "appliance_model": "Alfa", "assignee_id": user["id"],
        })
        self.assertEqual(status, 201)
        ticket_id = result["ticket"]["id"]
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_jobs WHERE ticket_id=?", (ticket_id,)).fetchone()[0], 1)
        self.request(f"/api/tickets/{ticket_id}", {"notes": "ordinary edit"}, "PUT")
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_jobs WHERE ticket_id=?", (ticket_id,)).fetchone()[0], 1)
        # A different account cannot take over an active endpoint.
        self.assertEqual(self.request("/api/push/subscribe", {"subscription": subscription})[0], 400)
        self.browser = tech_browser
        self.assertEqual(self.request("/api/push/unsubscribe", {})[0], 200)
        self.assertEqual(self.request("/api/push/test", {})[0], 400)
        self.request("/api/push/subscribe", {"subscription": subscription})
        self.request("/api/logout", {})
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_jobs").fetchone()[0], 0)

    def test_language_is_account_scoped_and_persistent(self):
        self.assertEqual(self.request("/api/preferences", {"language": "sr-Latn"})[0], 401)
        self.request("/api/login", {"username": "admin", "password": "test-admin-password"})
        self.assertEqual(self.request("/api/me")[1]["user"]["language"], "en")
        status, result = self.request("/api/preferences", {"language": "sr-Latn"})
        self.assertEqual(status, 200)
        self.assertEqual(result["user"]["language"], "sr-Latn")
        self.assertEqual(self.request("/api/preferences", {"language": "invalid"})[0], 400)
        self.request("/api/logout", {})
        self.browser = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.assertEqual(self.request("/api/login", {
            "username": "admin", "password": "test-admin-password",
        })[1]["user"]["language"], "sr-Latn")

    def test_default_technician_and_explicit_unassigned(self):
        self.request("/api/login", {"username": "admin", "password": "test-admin-password"})
        technicians = self.request("/api/technicians")[1]["users"]
        ognjen = next(user for user in technicians if user["username"] == "ognjen")
        self.assertEqual(ognjen["roles"], ["technician"])
        payload = {"customer": "Test", "phone": "123456", "address": "Test street", "appliance_model": "Alfa"}
        status, result = self.request("/api/tickets", payload)
        self.assertEqual(status, 201)
        self.assertEqual(result["ticket"]["assignee_id"], ognjen["id"])
        ticket_id = result["ticket"]["id"]
        self.assertEqual(self.request("/api/tickets", {**payload, "assignee_id": ""})[1]["ticket"]["assignee_id"], None)
        self.assertEqual(self.request(f"/api/tickets/{ticket_id}", {"notes": "edit"}, "PUT")[1]["ticket"]["assignee_id"], ognjen["id"])
        with server.connect() as db:
            before = db.execute("SELECT password_hash FROM users WHERE id=?", (ognjen["id"],)).fetchone()[0]
        server.initialize()
        with server.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE username='ognjen'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT password_hash FROM users WHERE id=?", (ognjen["id"],)).fetchone()[0], before)
        self.request(f"/api/users/{ognjen['id']}", {"active": False}, "PUT")
        self.assertIsNone(self.request("/api/tickets", payload)[1]["ticket"]["assignee_id"])
        server.initialize()
        self.assertFalse(any(user["username"] == "ognjen" for user in self.request("/api/technicians")[1]["users"]))

    def test_existing_ognjen_gets_technician_role_once(self):
        with server.connect() as db:
            db.execute("DELETE FROM app_migrations WHERE name='ognjen-technician-role'")
            db.execute("UPDATE users SET username='OgNjEn',roles=? WHERE username='ognjen'",
                       (json.dumps(["admin", "dispatcher"]),))
            before = dict(db.execute("SELECT * FROM users WHERE username='OgNjEn'").fetchone())
        server.initialize()
        self.request("/api/login", {"username": "admin", "password": "test-admin-password"})
        technicians = self.request("/api/technicians")[1]["users"]
        ognjen = next(user for user in technicians if user["id"] == before["id"])
        self.assertEqual(set(ognjen["roles"]), {"admin", "dispatcher", "technician"})
        with server.connect() as db:
            after = dict(db.execute("SELECT * FROM users WHERE id=?", (before["id"],)).fetchone())
            for field in ("username", "salt", "password_hash", "active"):
                self.assertEqual(before[field], after[field])
            db.execute("UPDATE users SET roles=?,active=0 WHERE id=?",
                       (json.dumps(["admin", "dispatcher"]), before["id"]))
        server.initialize()
        with server.connect() as db:
            after = db.execute("SELECT roles,active FROM users WHERE id=?", (before["id"],)).fetchone()
            self.assertEqual(json.loads(after["roles"]), ["admin", "dispatcher"])
            self.assertEqual(after["active"], 0)

    @patch("addresses.search")
    def test_address_search_authorization_and_provider_failure(self, search):
        self.assertEqual(self.request("/api/addresses/search", {"query": "mars tit elem"})[0], 401)
        self.request("/api/login", {"username": "admin", "password": "test-admin-password"})
        search.return_value = [{"label": "Maršala Tita, Elemir, 2C"}]
        status, result = self.request("/api/addresses/search", {"query": "mars tit 2c elem"})
        self.assertEqual(status, 200)
        self.assertEqual(result["suggestions"][0]["label"], "Maršala Tita, Elemir, 2C")
        search.side_effect = addresses.AddressUnavailable("Address suggestions are unavailable. You can enter the address manually.")
        self.assertEqual(self.request("/api/addresses/search", {"query": "mars tit elem"})[0], 503)
        self.request("/api/users", {"username": "tech", "password": "test-tech-password", "roles": ["technician"]})
        self.request("/api/logout", {})
        self.request("/api/login", {"username": "tech", "password": "test-tech-password"})
        self.assertEqual(self.request("/api/addresses/search", {"query": "mars tit elem"})[0], 403)


if __name__ == "__main__":
    unittest.main()
