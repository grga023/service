import gc
import http.cookiejar
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import server


def unfold(body):
    return body.replace("\r\n ", "")


class IcsFoldingTests(unittest.TestCase):
    def test_short_lines_are_untouched(self):
        self.assertEqual(server.ics_fold("SUMMARY:Alfa service"), "SUMMARY:Alfa service")

    def test_long_lines_stay_within_the_octet_limit(self):
        line = "LOCATION:" + "Maršala Tita 2C, Elemir, " * 12
        folded = server.ics_fold(line)
        for index, piece in enumerate(folded.split("\r\n")):
            self.assertLessEqual(len(piece.encode("utf-8")), 75)
            self.assertEqual(piece.startswith(" "), index > 0)
        self.assertEqual(unfold(folded), line)

    def test_multibyte_characters_are_never_split(self):
        folded = server.ics_fold("DESCRIPTION:" + "š" * 120)
        for piece in folded.split("\r\n"):
            piece.encode("utf-8").decode("utf-8")
        self.assertEqual(unfold(folded), "DESCRIPTION:" + "š" * 120)


class CalendarFeedTests(unittest.TestCase):
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

    def fetch_calendar(self, path, signed_in=False):
        opener = self.browser if signed_in else build_opener()
        try:
            with opener.open(self.root + path) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except HTTPError as error:
            return error.code, dict(error.headers), error.read().decode("utf-8")

    def login(self, username="admin", password="test-admin-password"):
        self.assertEqual(self.request("/api/login", {"username": username, "password": password})[0], 200)

    def test_feed_is_readable_without_a_session_and_subscribable(self):
        self.login()
        self.request("/api/tickets", {
            "customer": "Test Customer", "phone": "060111222",
            "address": "Maršala Tita 2C, Elemir, Zrenjanin, Banat, Serbia 23208",
            "appliance_model": "Celtic", "appointment_start": "2031-04-07T09:00",
            "estimated_effort": "1.5h", "notes": "Bring the long replacement part list " * 4,
        })
        token = self.request("/api/calendar-link")[1]["token"]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{20,64}$")

        status, headers, body = self.fetch_calendar(f"/calendar/{token}.ics")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/calendar"))
        self.assertTrue(headers["Content-Disposition"].startswith("inline"))
        self.assertIn("METHOD:PUBLISH", body)
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT30M", body)
        self.assertIn("X-WR-CALNAME:FurnaceDesk", body)
        self.assertIn("Test Customer", unfold(body))
        self.assertIn("DTSTART:20310407T090000", body)
        self.assertIn("DTEND:20310407T103000", body)
        for line in body.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_appointments_without_an_end_still_get_a_dtend(self):
        self.login()
        self.request("/api/tickets", {
            "customer": "No End", "phone": "060", "address": "Street 1",
            "appliance_model": "Alfa", "appointment_start": "2031-05-02T08:00",
        })
        token = self.request("/api/calendar-link")[1]["token"]
        body = self.fetch_calendar(f"/calendar/{token}.ics")[2]
        self.assertIn("DTSTART:20310502T080000", body)
        self.assertIn("DTEND:20310502T090000", body)

    def test_unknown_and_revoked_tokens_are_rejected(self):
        self.login()
        token = self.request("/api/calendar-link")[1]["token"]
        self.assertEqual(self.fetch_calendar("/calendar/" + "z" * 30 + ".ics")[0], 404)
        self.assertEqual(self.fetch_calendar(f"/calendar/{token}.ics")[0], 200)

        rotated = self.request("/api/calendar-link/reset", {})[1]["token"]
        self.assertNotEqual(rotated, token)
        self.assertEqual(self.fetch_calendar(f"/calendar/{token}.ics")[0], 404)
        self.assertEqual(self.fetch_calendar(f"/calendar/{rotated}.ics")[0], 200)
        self.assertEqual(self.request("/api/calendar-link")[1]["token"], rotated)

    def test_feed_only_exposes_the_owning_technicians_visits(self):
        self.login()
        tech = self.request("/api/users", {
            "username": "tech", "password": "test-tech-password", "roles": ["technician"],
        })[1]["user"]
        self.request("/api/tickets", {
            "customer": "Assigned Customer", "phone": "060", "address": "Street 1",
            "appliance_model": "Alfa", "appointment_start": "2031-06-01T08:00", "assignee_id": tech["id"],
        })
        self.request("/api/tickets", {
            "customer": "Other Customer", "phone": "061", "address": "Street 2",
            "appliance_model": "Celtic", "appointment_start": "2031-06-02T08:00", "assignee_id": "",
        })
        admin_feed = self.fetch_calendar(f"/calendar/{self.request('/api/calendar-link')[1]['token']}.ics")[2]
        self.assertEqual(self.request("/api/logout", {})[0], 204)

        self.login("tech", "test-tech-password")
        tech_feed = self.fetch_calendar(f"/calendar/{self.request('/api/calendar-link')[1]['token']}.ics")[2]
        self.assertIn("Assigned Customer", unfold(tech_feed))
        self.assertNotIn("Other Customer", unfold(tech_feed))
        self.assertIn("Other Customer", unfold(admin_feed))

    def test_session_download_switches_disposition_and_still_requires_sign_in(self):
        self.assertEqual(self.fetch_calendar("/calendar.ics")[0], 401)
        self.login()
        self.assertTrue(self.fetch_calendar("/calendar.ics", True)[1]["Content-Disposition"].startswith("inline"))
        headers = self.fetch_calendar("/calendar.ics?download=1", True)[1]
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(self.request("/api/calendar-link")[0], 200)

    def test_deactivated_accounts_lose_their_feed(self):
        self.login()
        tech = self.request("/api/users", {
            "username": "tech", "password": "test-tech-password", "roles": ["technician"],
        })[1]["user"]
        self.request("/api/logout", {})
        self.login("tech", "test-tech-password")
        token = self.request("/api/calendar-link")[1]["token"]
        self.request("/api/logout", {})

        self.login()
        self.request(f"/api/users/{tech['id']}", {"active": False}, "PUT")
        self.assertEqual(self.fetch_calendar(f"/calendar/{token}.ics")[0], 404)

    def test_tokens_survive_restarts_and_stay_unique(self):
        self.login()
        token = self.request("/api/calendar-link")[1]["token"]
        server.initialize()
        self.assertEqual(self.request("/api/calendar-link")[1]["token"], token)
        with server.connect() as db:
            tokens = [row[0] for row in db.execute("SELECT calendar_token FROM users")]
        self.assertTrue(all(tokens))
        self.assertEqual(len(tokens), len(set(tokens)))


if __name__ == "__main__":
    unittest.main()
