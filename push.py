import base64
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from py_vapid import VapidException
from requests.exceptions import RequestException
from requests import Session

LOG = logging.getLogger("furnacedesk.push")


class PushSession(Session):
    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def encode_key(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def initialize(db, database_path):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_hash TEXT NOT NULL REFERENCES sessions(token_hash) ON DELETE CASCADE,
            endpoint TEXT NOT NULL UNIQUE,
            subscription_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS push_jobs (
            id INTEGER PRIMARY KEY,
            subscription_id INTEGER NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
            ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
            payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0
        );
    """)
    key_path = Path(database_path).parent / "push-private.pem"
    if not key_path.exists():
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        # O_EXCL preserves an existing key if two processes initialize together.
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(pem)
    return key_path


def public_key(key_path):
    private_key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    return encode_key(private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    ))


def validate_subscription(data):
    if not isinstance(data, dict):
        raise ValueError("A browser push subscription is required.")
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError("Invalid push endpoint.")
    try:
        url = urlparse(endpoint)
        host = url.hostname or ""
        valid_host = host in {
            "fcm.googleapis.com", "updates.push.services.mozilla.com",
        } or host == "push.apple.com" or host.endswith(".push.apple.com") or host.endswith(".notify.windows.com")
        if url.scheme != "https" or not valid_host or url.port not in (None, 443) or url.username or url.password or url.fragment:
            raise ValueError("Unsupported push provider. Use Safari, Chrome, Edge, or Firefox.")
    except ValueError as exc:
        raise ValueError("Invalid or unsupported push endpoint.") from exc
    keys = data.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("Missing browser encryption keys.")
    decoded = {}
    for name, size in (("auth", 16), ("p256dh", 65)):
        value = keys.get(name)
        if not isinstance(value, str) or len(value) > 128:
            raise ValueError("Invalid browser encryption keys.")
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            if len(raw) != size:
                raise ValueError("Invalid key size.")
            decoded[name] = raw
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("Invalid browser encryption keys.") from exc
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded["p256dh"])
    except ValueError as exc:
        raise ValueError("Invalid browser public key.") from exc
    return {"endpoint": endpoint, "keys": {name: keys[name] for name in ("auth", "p256dh")}}


def enqueue_assignment(db, ticket, previous_assignee=None):
    if not ticket["assignee_id"] or ticket["assignee_id"] == previous_assignee or ticket["status"] == "done":
        return
    # Do not expose customer names, addresses, or phone numbers on a lock screen.
    user = db.execute("SELECT language FROM users WHERE id=?", (ticket["assignee_id"],)).fetchone()
    serbian = user and user["language"] == "sr-Latn"
    payload = json.dumps({
        "title": "Dodeljen vam je novi servis" if serbian else "New service assigned",
        "body": f"Dodeljen vam je servis #{ticket['id']}. Dodirnite za detalje." if serbian else
            f"Service #{ticket['id']} has been assigned to you. Tap to view it.",
        "ticket_id": ticket["id"],
        "tag": f"assignment-{ticket['id']}",
    })
    db.execute("""
        INSERT INTO push_jobs (subscription_id,ticket_id,payload)
        SELECT id,?,? FROM push_subscriptions WHERE user_id=?
    """, (ticket["id"], payload, ticket["assignee_id"]))


def enqueue_test(db, subscription_id):
    user = db.execute("""
        SELECT u.language FROM users u JOIN push_subscriptions s ON s.user_id=u.id WHERE s.id=?
    """, (subscription_id,)).fetchone()
    serbian = user and user["language"] == "sr-Latn"
    db.execute("INSERT INTO push_jobs (subscription_id,payload) VALUES (?,?)", (
        subscription_id, json.dumps({
            "title": "FurnaceDesk obaveštenja" if serbian else "FurnaceDesk notifications",
            "body": "Ovaj telefon je spreman za obaveštenja o dodeljenim servisima." if serbian else
                "This phone is ready to receive service assignments.",
            "tag": "furnacedesk-test",
        }),
    ))


def deliver_one(database_path, key_path):
    with closing(sqlite3.connect(database_path, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        job = db.execute("""
            SELECT j.*,s.subscription_json,s.user_id,s.session_hash
            FROM push_jobs j JOIN push_subscriptions s ON s.id=j.subscription_id
            WHERE j.available_at<=? ORDER BY j.id LIMIT 1
        """, (time.time(),)).fetchone()
        if not job:
            return False
        recipient = db.execute("""
            SELECT u.active FROM users u JOIN sessions s ON s.user_id=u.id
            WHERE u.id=? AND s.token_hash=? AND s.expires_at>?
        """, (job["user_id"], job["session_hash"],
              datetime.now(timezone.utc).isoformat())).fetchone()
        ticket = db.execute("SELECT assignee_id,status FROM tickets WHERE id=?", (job["ticket_id"],)).fetchone() if job["ticket_id"] else None
        if not recipient or not recipient["active"] or (job["ticket_id"] and (
            not ticket or ticket["assignee_id"] != job["user_id"] or ticket["status"] == "done"
        )):
            db.execute("DELETE FROM push_jobs WHERE id=?", (job["id"],))
            db.commit()
            return True
        try:
            with PushSession() as session:
                webpush(
                    subscription_info=json.loads(job["subscription_json"]), data=job["payload"],
                    vapid_private_key=str(key_path),
                    vapid_claims={"sub": os.environ.get("PUSH_CONTACT", "https://service.lil-server-lab.xyz")},
                    ttl=3600, timeout=10, requests_session=session,
                )
        except (WebPushException, RequestException, VapidException) as exc:
            response = getattr(exc, "response", None)
            status = response.status_code if response is not None else None
            if status in (404, 410):
                db.execute("DELETE FROM push_subscriptions WHERE id=?", (job["subscription_id"],))
                LOG.warning("Removed expired push subscription %s (HTTP %s)", job["subscription_id"], status)
            elif isinstance(exc, VapidException) or job["attempts"] >= 4 or (status is not None and 400 <= status < 500 and status != 429):
                db.execute("DELETE FROM push_jobs WHERE id=?", (job["id"],))
                LOG.error("Push job %s failed permanently (HTTP %s); check provider/VAPID configuration", job["id"], status)
            else:
                db.execute("UPDATE push_jobs SET attempts=attempts+1,available_at=? WHERE id=?",
                           (time.time() + 30 * 2 ** job["attempts"], job["id"]))
                LOG.warning("Push job %s failed (HTTP %s); queued for retry", job["id"], status)
        else:
            db.execute("DELETE FROM push_jobs WHERE id=?", (job["id"],))
        db.commit()
        return True


def start_worker(database_path, key_path):
    def run():
        while True:
            try:
                if not deliver_one(database_path, key_path):
                    time.sleep(2)
            except (sqlite3.Error, OSError, ValueError):
                LOG.exception("Push worker failed; retrying in 10 seconds")
                time.sleep(10)
    threading.Thread(target=run, name="push-delivery", daemon=True).start()
