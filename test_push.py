import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException
from requests import Response

import push


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE users (id INTEGER PRIMARY KEY,active INTEGER);
            CREATE TABLE sessions (token_hash TEXT PRIMARY KEY,user_id INTEGER,expires_at TEXT);
            CREATE TABLE tickets (id INTEGER PRIMARY KEY,assignee_id INTEGER,status TEXT);
            INSERT INTO users VALUES (1,1),(2,1);
            INSERT INTO sessions VALUES ('session1',1,'9999'),('session2',2,'9999');
            INSERT INTO tickets VALUES (1,1,'received');
        """)
        self.key_path = push.initialize(self.db, self.path)
        self.db.execute("""
            INSERT INTO push_subscriptions (id,user_id,session_hash,endpoint,subscription_json)
            VALUES (1,1,'session1','https://fcm.googleapis.com/test','{}'),
                   (2,2,'session2','https://fcm.googleapis.com/test2','{}')
        """)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def queue(self):
        push.enqueue_assignment(self.db, self.db.execute("SELECT * FROM tickets WHERE id=1").fetchone())
        self.db.commit()

    def test_persistent_key(self):
        key = push.public_key(self.key_path)
        self.assertEqual(len(base64.urlsafe_b64decode(key + "=")), 65)
        push.initialize(self.db, self.path)
        self.assertEqual(key, push.public_key(self.key_path))

    def test_only_new_assignee_and_no_private_details(self):
        self.queue()
        jobs = self.db.execute("SELECT * FROM push_jobs").fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["subscription_id"], 1)
        self.assertEqual(json.loads(jobs[0]["payload"])["ticket_id"], 1)
        push.enqueue_assignment(self.db, self.db.execute("SELECT * FROM tickets").fetchone(), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM push_jobs").fetchone()[0], 1)
        self.db.execute("UPDATE tickets SET assignee_id=2")
        push.enqueue_assignment(self.db, self.db.execute("SELECT * FROM tickets").fetchone(), 1)
        self.assertEqual(self.db.execute("SELECT subscription_id FROM push_jobs ORDER BY id DESC").fetchone()[0], 2)
        self.db.execute("UPDATE tickets SET status='done'")
        push.enqueue_assignment(self.db, self.db.execute("SELECT * FROM tickets").fetchone())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM push_jobs").fetchone()[0], 2)

    @patch("push.webpush")
    def test_delivery_and_stale_assignment(self, send):
        self.queue()
        push.deliver_one(self.path, self.key_path)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["timeout"], 10)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM push_jobs").fetchone()[0], 0)
        self.queue()
        self.db.execute("UPDATE tickets SET assignee_id=2")
        self.db.commit()
        push.deliver_one(self.path, self.key_path)
        send.assert_called_once()

    @patch("push.webpush")
    def test_expired_and_transient_subscriptions(self, send):
        self.queue()
        response = Response()
        response.status_code = 503
        send.side_effect = WebPushException("test", response=response)
        push.deliver_one(self.path, self.key_path)
        self.assertEqual(self.db.execute("SELECT attempts FROM push_jobs").fetchone()[0], 1)
        self.db.execute("UPDATE push_jobs SET available_at=0")
        self.db.commit()
        response.status_code = 410
        push.deliver_one(self.path, self.key_path)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM push_jobs").fetchone()[0], 0)
        self.assertIsNone(self.db.execute("SELECT id FROM push_subscriptions WHERE id=1").fetchone())

    def test_logout_cascades(self):
        self.queue()
        self.db.execute("DELETE FROM sessions WHERE token_hash='session1'")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM push_jobs").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 1)

    @patch("push.PushSession.post")
    def test_real_encryption_and_vapid_without_external_delivery(self, post):
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        subscription = {"endpoint": "https://fcm.googleapis.com/test", "keys": {
            "auth": push.encode_key(b"a" * 16), "p256dh": push.encode_key(public),
        }}
        self.db.execute("UPDATE push_subscriptions SET subscription_json=? WHERE id=1", (json.dumps(subscription),))
        self.queue()
        response = Response()
        response.status_code = 201
        post.return_value = response
        push.deliver_one(self.path, self.key_path)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["headers"]["content-encoding"], "aes128gcm")
        self.assertTrue(post.call_args.kwargs["headers"]["authorization"].startswith("vapid "))
        self.assertNotIn(b"assigned", post.call_args.kwargs["data"])

    def test_validate_endpoint_and_keys(self):
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
        )
        data = {"endpoint": "https://web.push.apple.com/test", "keys": {
            "auth": push.encode_key(b"a" * 16), "p256dh": push.encode_key(public),
        }}
        self.assertEqual(push.validate_subscription(data), data)
        for endpoint in ("http://fcm.googleapis.com/x", "https://localhost/x",
                         "https://fcm.googleapis.com.evil.test/x", "https://web.push.apple.com:8443/x"):
            with self.assertRaises(ValueError):
                push.validate_subscription({**data, "endpoint": endpoint})
        with self.assertRaises(ValueError):
            push.validate_subscription({**data, "keys": {"auth": "bad", "p256dh": "bad"}})


if __name__ == "__main__":
    unittest.main()
