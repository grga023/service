#!/usr/bin/env python3
import argparse
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import push

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "data" / "service.sqlite3"))
SESSION_COOKIE_SECONDS = 400 * 24 * 60 * 60
SESSION_EXPIRY = datetime.max.replace(tzinfo=timezone.utc).isoformat()
ROLES = {"admin", "dispatcher", "technician"}
STATUSES = {"received", "in_progress", "done", "problem"}
PBKDF2_ITERATIONS = 310_000


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password, salt, expected):
    _, actual = hash_password(password, bytes.fromhex(salt))
    return hmac.compare_digest(actual, expected)


def initialize():
    with connect() as db:
        db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                roles TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
                appliance_model TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'received',
                used_parts TEXT NOT NULL DEFAULT '',
                needed_parts TEXT NOT NULL DEFAULT '',
                needed_parts_json TEXT NOT NULL DEFAULT '[]',
                parts_json TEXT NOT NULL DEFAULT '[]',
                labor_cost REAL NOT NULL DEFAULT 0,
                transport_cost REAL NOT NULL DEFAULT 0,
                estimated_hours REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                appointment_start TEXT NOT NULL DEFAULT '',
                appointment_end TEXT NOT NULL DEFAULT '',
                assignee_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                phone_key TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
            CREATE INDEX IF NOT EXISTS idx_tickets_appointment ON tickets(appointment_start);
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
        """)
        ticket_columns = {row["name"] for row in db.execute("PRAGMA table_info(tickets)")}
        if "customer_id" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL")
        if "parts_json" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN parts_json TEXT NOT NULL DEFAULT '[]'")
        if "labor_cost" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN labor_cost REAL NOT NULL DEFAULT 0")
        if "transport_cost" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN transport_cost REAL NOT NULL DEFAULT 0")
        if "needed_parts_json" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN needed_parts_json TEXT NOT NULL DEFAULT '[]'")
            for ticket in db.execute("SELECT id,needed_parts FROM tickets WHERE needed_parts!=''").fetchall():
                db.execute(
                    "UPDATE tickets SET needed_parts_json=? WHERE id=?",
                    (json.dumps([{"name": ticket["needed_parts"], "quantity": 1}], ensure_ascii=False), ticket["id"]),
                )
        if "estimated_hours" not in ticket_columns:
            db.execute("ALTER TABLE tickets ADD COLUMN estimated_hours REAL NOT NULL DEFAULT 0")
            for ticket in db.execute(
                "SELECT id,appointment_start,appointment_end FROM tickets WHERE appointment_start!='' AND appointment_end!=''"
            ).fetchall():
                try:
                    start = datetime.fromisoformat(ticket["appointment_start"])
                    end = datetime.fromisoformat(ticket["appointment_end"])
                    hours = (end - start).total_seconds() / 3600
                    if math.isfinite(hours) and hours > 0:
                        db.execute("UPDATE tickets SET estimated_hours=? WHERE id=?", (hours, ticket["id"]))
                except ValueError:
                    continue
        for ticket in db.execute("SELECT id,parts_json FROM tickets").fetchall():
            parts = json.loads(ticket["parts_json"])
            if any("regular_price" not in part or "discounted_cost" not in part for part in parts):
                for part in parts:
                    legacy_price = part.get("unit_price", 0)
                    part.setdefault("regular_price", legacy_price)
                    part.setdefault("discounted_cost", legacy_price)
                    part.pop("unit_price", None)
                db.execute(
                    "UPDATE tickets SET parts_json=? WHERE id=?",
                    (json.dumps(parts, ensure_ascii=False), ticket["id"]),
                )
        unlinked = db.execute("SELECT * FROM tickets WHERE customer_id IS NULL ORDER BY created_at").fetchall()
        for ticket in unlinked:
            customer_id = upsert_customer(db, ticket["customer"], ticket["phone"], ticket["address"], ticket["created_at"])
            db.execute("UPDATE tickets SET customer_id=? WHERE id=?", (customer_id, ticket["id"]))
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
        push.initialize(db, DB_PATH)
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if not count:
            username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
            password = os.environ.get("ADMIN_PASSWORD")
            generated = not password
            if generated:
                alphabet = string.ascii_letters + string.digits + "-_!@"
                password = "".join(secrets.choice(alphabet) for _ in range(20))
            elif len(password) < 8 or len(password) > 200:
                raise ValueError("ADMIN_PASSWORD must be between 8 and 200 characters.")
            if len(username) > 80:
                raise ValueError("ADMIN_USERNAME must be 80 characters or fewer.")
            salt, digest = hash_password(password)
            db.execute(
                "INSERT INTO users (username,salt,password_hash,roles,created_at) VALUES (?,?,?,?,?)",
                (username, salt, digest, json.dumps(["admin"]), datetime.now(timezone.utc).isoformat()),
            )
            if generated:
                print(f"Initial admin account: {username} / {password}", flush=True)
            else:
                print(f"Initial admin account created for {username} (ADMIN_PASSWORD).", flush=True)


def phone_key(phone):
    digits = "".join(character for character in phone if character.isdigit())
    return digits or phone.strip().casefold()


def reset_admin_password(username, password):
    if not isinstance(password, str) or not 8 <= len(password) <= 200:
        raise ValueError("Password must be between 8 and 200 characters.")
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if not user or "admin" not in json.loads(user["roles"]):
            raise ValueError("Administrator account not found.")
        salt, digest = hash_password(password)
        db.execute("UPDATE users SET salt=?,password_hash=? WHERE id=?", (salt, digest, user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))


def upsert_customer(db, name, phone, address, timestamp=None):
    timestamp = timestamp or datetime.now(timezone.utc).isoformat()
    key = phone_key(phone)
    existing = db.execute("SELECT id FROM customers WHERE phone_key=?", (key,)).fetchone()
    if existing:
        db.execute(
            "UPDATE customers SET name=?,phone=?,address=?,updated_at=? WHERE id=?",
            (name, phone, address, timestamp, existing["id"]),
        )
        return existing["id"]
    cursor = db.execute(
        "INSERT INTO customers (name,phone,phone_key,address,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (name, phone, key, address, timestamp, timestamp),
    )
    return cursor.lastrowid


def clean_text(value, field, maximum, required=False):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required.")
    if len(value) > maximum:
        raise ValueError(f"{field} must be {maximum} characters or fewer.")
    return value


def parse_roles(value):
    if not isinstance(value, list) or not value or any(not isinstance(role, str) or role not in ROLES for role in value):
        raise ValueError("Select at least one valid role.")
    return sorted(set(value))


def parse_appointment(value, field):
    value = clean_text(value or "", field, 32)
    if not value:
        return ""
    try:
        appointment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid date and time.") from exc
    if appointment.tzinfo is not None:
        raise ValueError(f"{field} must use local time without a timezone.")
    return appointment.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def parse_estimated_effort(value):
    if value is None:
        return 0.0
    if not isinstance(value, str):
        raise ValueError("Estimated effort must use hours, days, or weeks, e.g. 1.5d.")
    value = value.strip()
    if not value:
        return 0.0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|d|w)", value, re.IGNORECASE)
    if not match:
        raise ValueError("Enter estimated effort as hours, days, or weeks, e.g. 1h, 1.5d, or 1w.")
    amount = float(match.group(1))
    hours = amount * {"h": 1, "d": 8, "w": 56}[match.group(2).lower()]
    if not math.isfinite(hours) or hours <= 0 or hours > 100000:
        raise ValueError("Estimated effort must be greater than 0 and no more than 100000 hours.")
    hours = round(hours, 3)
    if not hours:
        raise ValueError("Estimated effort must be at least 0.001 hours.")
    return hours


def public_user(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "roles": json.loads(row["roles"]),
        "active": bool(row["active"]),
    }


def public_ticket(row):
    ticket = {key: row[key] for key in (
        "id", "customer", "customer_id", "phone", "address", "appliance_model", "status",
        "used_parts", "needed_parts", "labor_cost", "transport_cost", "notes", "appointment_start",
        "appointment_end", "estimated_hours", "assignee_id", "created_at", "updated_at",
    )}
    ticket["parts"] = json.loads(row["parts_json"])
    ticket["needed_parts_items"] = json.loads(row["needed_parts_json"])
    ticket["parts_revenue"] = round(sum(part["quantity"] * part["regular_price"] for part in ticket["parts"]), 2)
    ticket["parts_cost"] = round(sum(part["quantity"] * part["discounted_cost"] for part in ticket["parts"]), 2)
    ticket["parts_margin"] = round(ticket["parts_revenue"] - ticket["parts_cost"], 2)
    ticket["customer_total"] = round(ticket["parts_revenue"] + ticket["labor_cost"] + ticket["transport_cost"], 2)
    ticket["earnings"] = round(ticket["parts_margin"] + ticket["labor_cost"] + ticket["transport_cost"], 2)
    ticket["estimated_effort"] = f"{ticket['estimated_hours']:g}h" if ticket["estimated_hours"] else ""
    return ticket


def parse_needed_parts(value):
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("Add no more than 50 needed part items.")
    normalized = []
    for part in value:
        if not isinstance(part, dict):
            raise ValueError("Each needed part must include a name and quantity.")
        name = clean_text(part.get("name", ""), "Part name", 120)
        quantity_value = part.get("quantity", "")
        if not name and quantity_value in ("", None):
            continue
        if not name:
            raise ValueError("Enter a name for every needed part.")
        try:
            quantity = float(quantity_value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Part quantity must be a valid number.") from exc
        if not math.isfinite(quantity) or quantity <= 0 or quantity > 100000:
            raise ValueError("Part quantity must be greater than 0 and no more than 100000.")
        normalized.append({"name": name, "quantity": quantity})
    return normalized


def completion_payload(data):
    parts = data.get("parts", [])
    if not isinstance(parts, list) or len(parts) > 50:
        raise ValueError("Add no more than 50 part items.")
    normalized = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Each part must include a name, quantity, and unit price.")
        name = clean_text(part.get("name", ""), "Part name", 120)
        quantity_value = part.get("quantity", "")
        regular_price_value = part.get("regular_price", "")
        discounted_cost_value = part.get("discounted_cost", "")
        if not name and quantity_value in ("", None) and regular_price_value in ("", None) and discounted_cost_value in ("", None):
            continue
        if not name:
            raise ValueError("Enter a name for every part.")
        try:
            quantity = float(quantity_value)
            regular_price = float(regular_price_value)
            discounted_cost = float(discounted_cost_value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Part quantity, regular price, and discounted cost must be valid numbers.") from exc
        if not math.isfinite(quantity) or quantity <= 0 or quantity > 100000:
            raise ValueError("Part quantity must be greater than 0 and no more than 100000.")
        if not math.isfinite(regular_price) or regular_price < 0 or regular_price > 100000000:
            raise ValueError("Part regular price must be a non-negative amount.")
        if not math.isfinite(discounted_cost) or discounted_cost < 0 or discounted_cost > 100000000:
            raise ValueError("Part discounted cost must be a non-negative amount.")
        normalized.append({
            "name": name,
            "quantity": quantity,
            "regular_price": round(regular_price, 2),
            "discounted_cost": round(discounted_cost, 2),
        })
    amounts = {}
    for field, label in (("labor_cost", "Labor cost"), ("transport_cost", "Transport cost")):
        try:
            amount = float(data.get(field, 0) or 0)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{label} must be a valid amount.") from exc
        if not math.isfinite(amount) or amount < 0 or amount > 100000000:
            raise ValueError(f"{label} must be a non-negative amount.")
        amounts[field] = round(amount, 2)
    return {"parts_json": json.dumps(normalized, ensure_ascii=False), **amounts}


class ServiceHandler(BaseHTTPRequestHandler):
    server_version = "FurnaceDesk/1.0"

    def end_headers(self):
        token = getattr(self, "_renew_session", None)
        if token:
            secure = "; Secure" if os.environ.get("COOKIE_SECURE") == "1" else ""
            self.send_header(
                "Set-Cookie",
                f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_COOKIE_SECONDS}{secure}",
            )
        super().end_headers()

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)

    def read_json(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request size.") from exc
        if length < 1 or length > 64 * 1024:
            raise ValueError("Request body must be between 1 byte and 64 KB.")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def current_user(self, db):
        self._renew_session = None
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get("session")
        except CookieError:
            return None
        if morsel is None:
            return None
        token = morsel.value
        row = db.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (hashlib.sha256(token.encode()).hexdigest(), datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if row:
            self._renew_session = token
            db.execute(
                "UPDATE sessions SET expires_at=? WHERE token_hash=? AND expires_at!=?",
                (SESSION_EXPIRY, hashlib.sha256(token.encode()).hexdigest(), SESSION_EXPIRY),
            )
            db.commit()
        return row

    def require_roles(self, user, *roles):
        user_roles = set(json.loads(user["roles"]))
        if user_roles.isdisjoint(roles):
            raise PermissionError("You do not have permission to perform this action.")

    def ticket_payload(self, data, existing=None):
        result = {}
        for field, maximum, required in (
            ("customer", 120, True), ("phone", 40, True), ("address", 240, True),
            ("appliance_model", 120, True), ("used_parts", 2000, False),
            ("notes", 4000, False),
        ):
            result[field] = clean_text(data.get(field, existing[field] if existing else ""), field, maximum, required)
        needed_parts = parse_needed_parts(
            data.get("needed_parts_items", json.loads(existing["needed_parts_json"]) if existing else [])
        )
        result["needed_parts_json"] = json.dumps(needed_parts, ensure_ascii=False)
        result["needed_parts"] = ", ".join(
            f"{part['name']} × {part['quantity']:g}" for part in needed_parts
        )
        status = data.get("status", existing["status"] if existing else "received")
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("Select a valid service status.")
        result["status"] = status
        result["appointment_start"] = parse_appointment(
            data.get("appointment_start", existing["appointment_start"] if existing else ""), "Appointment start"
        )
        existing_effort = f"{existing['estimated_hours']:g}h" if existing and existing["estimated_hours"] else ""
        result["estimated_hours"] = parse_estimated_effort(data.get("estimated_effort", existing_effort))
        if result["appointment_start"] and result["estimated_hours"]:
            appointment_end = datetime.fromisoformat(result["appointment_start"]) + timedelta(hours=result["estimated_hours"])
            result["appointment_end"] = appointment_end.isoformat(timespec="minutes")
        else:
            result["appointment_end"] = ""
        result["assignee_id"] = data.get("assignee_id", existing["assignee_id"] if existing else None) or None
        if result["assignee_id"] is not None:
            try:
                result["assignee_id"] = int(result["assignee_id"])
            except (ValueError, TypeError) as exc:
                raise ValueError("Select a valid technician.") from exc
        return result

    def ensure_technician(self, db, assignee_id):
        if assignee_id is not None:
            row = db.execute("SELECT roles,active FROM users WHERE id=?", (assignee_id,)).fetchone()
            if not row or not row["active"] or "technician" not in json.loads(row["roles"]):
                raise ValueError("Assigned user must be an active technician.")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self.serve_static("index.html")
            return
        if path in ("/app.js", "/styles.css", "/sw.js", "/manifest.webmanifest",
                    "/icon-192.png", "/icon-512.png"):
            self.serve_static(path[1:])
            return
        if path == "/health":
            self.send_json({"status": "ok"})
            return
        with connect() as db:
            user = self.current_user(db)
            if path == "/calendar.ics":
                if not user:
                    self.send_error_json(HTTPStatus.UNAUTHORIZED, "Sign in to download the calendar.")
                    return
                self.send_calendar(db, user)
                return
            if not user:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "Please sign in.")
                return
            if path == "/api/me":
                self.send_json({"user": public_user(user)})
            elif path == "/api/push":
                session_hash = hashlib.sha256(self.session_token().encode()).hexdigest()
                self.send_json({
                    "public_key": push.public_key(DB_PATH.parent / "push-private.pem"),
                    "subscribed": db.execute(
                        "SELECT 1 FROM push_subscriptions WHERE session_hash=?", (session_hash,),
                    ).fetchone() is not None,
                })
            elif path == "/api/tickets":
                if "technician" in json.loads(user["roles"]) and "admin" not in json.loads(user["roles"]) and "dispatcher" not in json.loads(user["roles"]):
                    rows = db.execute("SELECT * FROM tickets WHERE assignee_id=? ORDER BY updated_at DESC", (user["id"],))
                else:
                    rows = db.execute("SELECT * FROM tickets ORDER BY updated_at DESC")
                self.send_json({"tickets": [public_ticket(row) for row in rows]})
            elif path == "/api/customers":
                try:
                    self.require_roles(user, "admin", "dispatcher")
                    rows = db.execute("""
                        SELECT c.id,c.name,c.phone,c.address,c.updated_at,
                               COUNT(t.id) AS service_count,
                               MAX(t.created_at) AS last_service,
                               GROUP_CONCAT(DISTINCT t.appliance_model) AS appliances
                        FROM customers c LEFT JOIN tickets t ON t.customer_id=c.id
                        GROUP BY c.id ORDER BY c.updated_at DESC,c.name COLLATE NOCASE
                    """)
                    customers = [{
                        "id": row["id"],
                        "name": row["name"],
                        "phone": row["phone"],
                        "address": row["address"],
                        "updated_at": row["updated_at"],
                        "service_count": row["service_count"],
                        "last_service": row["last_service"],
                        "appliances": row["appliances"].split(",") if row["appliances"] else [],
                    } for row in rows]
                    self.send_json({"customers": customers})
                except PermissionError as exc:
                    self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
            elif path.startswith("/api/customers/"):
                try:
                    self.require_roles(user, "admin", "dispatcher")
                    customer_id = int(path.rsplit("/", 1)[1])
                    customer = db.execute(
                        "SELECT id,name,phone,address,created_at,updated_at FROM customers WHERE id=?",
                        (customer_id,),
                    ).fetchone()
                    if not customer:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Customer not found.")
                        return
                    tickets = db.execute(
                        "SELECT * FROM tickets WHERE customer_id=? ORDER BY created_at DESC,id DESC",
                        (customer_id,),
                    )
                    self.send_json({
                        "customer": dict(customer),
                        "services": [public_ticket(row) for row in tickets],
                    })
                except PermissionError as exc:
                    self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
                except ValueError:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid customer ID.")
            elif path == "/api/technicians":
                try:
                    self.require_roles(user, "admin", "dispatcher")
                    rows = db.execute("SELECT * FROM users WHERE active=1 ORDER BY username")
                    users = [public_user(row) for row in rows if "technician" in json.loads(row["roles"])]
                    self.send_json({"users": users})
                except PermissionError as exc:
                    self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
            elif path == "/api/users":
                try:
                    self.require_roles(user, "admin")
                    rows = db.execute("SELECT * FROM users ORDER BY username")
                    self.send_json({"users": [public_user(row) for row in rows]})
                except PermissionError as exc:
                    self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def serve_static(self, filename):
        path = STATIC / filename
        if not path.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        content_type = "text/html; charset=utf-8" if filename.endswith(".html") else (
            "text/javascript; charset=utf-8" if filename.endswith(".js") else
            "application/manifest+json" if filename.endswith(".webmanifest") else
            "image/png" if filename.endswith(".png") else "text/css; charset=utf-8"
        )
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            data = self.read_json()
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        with connect() as db:
            if path == "/api/login":
                try:
                    username = clean_text(data.get("username", ""), "Username", 80, True)
                except ValueError as exc:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                password = data.get("password", "")
                user = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)).fetchone()
                if not user or not isinstance(password, str) or not verify_password(password, user["salt"], user["password_hash"]):
                    self.send_error_json(HTTPStatus.UNAUTHORIZED, "Incorrect username or password.")
                    return
                token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO sessions (token_hash,user_id,expires_at) VALUES (?,?,?)",
                           (hashlib.sha256(token.encode()).hexdigest(), user["id"], SESSION_EXPIRY))
                db.commit()
                secure = "; Secure" if os.environ.get("COOKIE_SECURE") == "1" else ""
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_COOKIE_SECONDS}{secure}")
                body = json.dumps({"user": public_user(user)}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            user = self.current_user(db)
            if not user:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "Please sign in.")
                return
            try:
                route_parts = path.strip("/").split("/")
                if path == "/api/push/subscribe":
                    subscription = push.validate_subscription(data.get("subscription"))
                    session_hash = hashlib.sha256(self.session_token().encode()).hexdigest()
                    existing = db.execute("SELECT user_id FROM push_subscriptions WHERE endpoint=?",
                                          (subscription["endpoint"],)).fetchone()
                    if existing and existing["user_id"] != user["id"]:
                        raise ValueError("This browser is subscribed to another account. Disable its notifications first.")
                    db.execute("""
                        INSERT INTO push_subscriptions (user_id,session_hash,endpoint,subscription_json)
                        VALUES (?,?,?,?)
                        ON CONFLICT(endpoint) DO UPDATE SET session_hash=excluded.session_hash,
                            subscription_json=excluded.subscription_json
                    """, (user["id"], session_hash, subscription["endpoint"], json.dumps(subscription)))
                    db.commit()
                    self.send_json({"subscribed": True})
                elif path in ("/api/push/unsubscribe", "/api/push/test"):
                    session_hash = hashlib.sha256(self.session_token().encode()).hexdigest()
                    if path.endswith("/unsubscribe"):
                        db.execute("DELETE FROM push_subscriptions WHERE session_hash=?", (session_hash,))
                        db.commit()
                        self.send_json({"subscribed": False})
                    else:
                        subscriptions = db.execute("SELECT id FROM push_subscriptions WHERE session_hash=?",
                                                   (session_hash,)).fetchall()
                        if not subscriptions:
                            raise ValueError("Enable notifications on this device first.")
                        for subscription in subscriptions:
                            push.enqueue_test(db, subscription["id"])
                        db.commit()
                        self.send_json({"queued": True})
                elif len(route_parts) == 4 and route_parts[:2] == ["api", "tickets"] and route_parts[3] == "complete":
                    ticket_id = int(route_parts[2])
                    existing = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
                    if not existing:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Service ticket not found.")
                        return
                    roles = set(json.loads(user["roles"]))
                    if roles.isdisjoint({"admin", "dispatcher"}) and (
                        "technician" not in roles or existing["assignee_id"] != user["id"]
                    ):
                        raise PermissionError("You can only complete tickets assigned to you.")
                    costs = completion_payload(data)
                    used_parts = ", ".join(
                        f"{part['quantity']:g} × {part['name']}" for part in json.loads(costs["parts_json"])
                    )
                    db.execute(
                        """UPDATE tickets SET status='done',parts_json=?,used_parts=?,labor_cost=?,transport_cost=?,updated_at=?
                           WHERE id=?""",
                        (costs["parts_json"], used_parts, costs["labor_cost"], costs["transport_cost"],
                         datetime.now(timezone.utc).isoformat(), ticket_id),
                    )
                    row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
                    db.commit()
                    self.send_json({"ticket": public_ticket(row)})
                elif path == "/api/logout":
                    self._renew_session = None
                    token = SimpleCookie(self.headers.get("Cookie", "")).get("session")
                    if token:
                        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.value.encode()).hexdigest(),))
                    db.commit()
                    secure = "; Secure" if os.environ.get("COOKIE_SECURE") == "1" else ""
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.send_header("Set-Cookie", f"session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}")
                    self.end_headers()
                elif path == "/api/tickets":
                    self.require_roles(user, "admin", "dispatcher")
                    ticket = self.ticket_payload({**data, "status": "received"})
                    if ticket["status"] == "done":
                        raise ValueError("Complete a service from its service record to enter the costs.")
                    self.ensure_technician(db, ticket["assignee_id"])
                    now = datetime.now(timezone.utc).isoformat()
                    customer_id = upsert_customer(db, ticket["customer"], ticket["phone"], ticket["address"], now)
                    keys = list(ticket)
                    cursor = db.execute(
                        f"INSERT INTO tickets ({','.join(keys)},customer_id,created_by,created_at,updated_at) VALUES ({','.join('?' for _ in keys)},?,?,?,?)",
                        [ticket[k] for k in keys] + [customer_id, user["id"], now, now],
                    )
                    row = db.execute("SELECT * FROM tickets WHERE id=?", (cursor.lastrowid,)).fetchone()
                    push.enqueue_assignment(db, row)
                    db.commit()
                    self.send_json({"ticket": public_ticket(row)}, HTTPStatus.CREATED)
                elif path == "/api/users":
                    self.require_roles(user, "admin")
                    username = clean_text(data.get("username", ""), "Username", 80, True)
                    password = data.get("password", "")
                    if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
                        raise ValueError("Password must be between 8 and 200 characters.")
                    roles = parse_roles(data.get("roles"))
                    salt, digest = hash_password(password)
                    try:
                        cursor = db.execute(
                            "INSERT INTO users (username,salt,password_hash,roles,created_at) VALUES (?,?,?,?,?)",
                            (username, salt, digest, json.dumps(roles), datetime.now(timezone.utc).isoformat()),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError("That username is already in use.") from exc
                    row = db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
                    db.commit()
                    self.send_json({"user": public_user(row)}, HTTPStatus.CREATED)
                else:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            except PermissionError as exc:
                self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        try:
            data = self.read_json()
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        with connect() as db:
            user = self.current_user(db)
            if not user:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "Please sign in.")
                return
            try:
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "tickets"]:
                    ticket_id = int(parts[2])
                    existing = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
                    if not existing:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Service ticket not found.")
                        return
                    roles = set(json.loads(user["roles"]))
                    if roles.isdisjoint({"admin", "dispatcher"}):
                        if "technician" not in roles or existing["assignee_id"] != user["id"]:
                            raise PermissionError("You can only update tickets assigned to you.")
                        allowed = {"status", "needed_parts_items", "notes"}
                        if set(data) - allowed:
                            raise PermissionError("Technicians can only update status, parts, and notes.")
                        ticket = self.ticket_payload({**{k: existing[k] for k in (
                            "customer", "phone", "address", "appliance_model", "status", "used_parts",
                            "needed_parts", "notes", "appointment_start", "appointment_end", "assignee_id",
                        )}, **data}, existing)
                    else:
                        ticket = self.ticket_payload(data, existing)
                        self.ensure_technician(db, ticket["assignee_id"])
                    if ticket["status"] == "done" and existing["status"] != "done":
                        raise ValueError("Use the completion form to enter service costs before marking it done.")
                    now = datetime.now(timezone.utc).isoformat()
                    customer_id = upsert_customer(db, ticket["customer"], ticket["phone"], ticket["address"], now)
                    assignments = ",".join(f"{key}=?" for key in ticket)
                    db.execute(f"UPDATE tickets SET {assignments},customer_id=?,updated_at=? WHERE id=?",
                               [ticket[k] for k in ticket] + [customer_id, now, ticket_id])
                    row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
                    push.enqueue_assignment(db, row, existing["assignee_id"])
                    db.commit()
                    self.send_json({"ticket": public_ticket(row)})
                elif len(parts) == 3 and parts[:2] == ["api", "users"]:
                    self.require_roles(user, "admin")
                    user_id = int(parts[2])
                    existing = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                    if not existing:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "User not found.")
                        return
                    username = clean_text(data.get("username", existing["username"]), "Username", 80, True)
                    roles = parse_roles(data.get("roles", json.loads(existing["roles"])))
                    active = data.get("active", bool(existing["active"]))
                    if not isinstance(active, bool):
                        raise ValueError("Active must be true or false.")
                    if user_id == user["id"] and (not active or "admin" not in roles):
                        raise ValueError("You cannot deactivate yourself or remove your own admin role.")
                    removing_admin = "admin" in json.loads(existing["roles"]) and (
                        not active or "admin" not in roles
                    )
                    if removing_admin:
                        other_admins = db.execute(
                            "SELECT COUNT(*) FROM users WHERE active=1 AND id!=? AND roles LIKE '%\"admin\"%'",
                            (user_id,),
                        ).fetchone()[0]
                        if other_admins == 0:
                            raise ValueError("At least one active administrator must remain.")
                    password = data.get("password", "")
                    if password:
                        if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
                            raise ValueError("Password must be between 8 and 200 characters.")
                        salt, digest = hash_password(password)
                        db.execute("UPDATE users SET username=?,roles=?,active=?,salt=?,password_hash=? WHERE id=?",
                                   (username, json.dumps(roles), int(active), salt, digest, user_id))
                    else:
                        db.execute("UPDATE users SET username=?,roles=?,active=? WHERE id=?",
                                   (username, json.dumps(roles), int(active), user_id))
                    if not active or password:
                        if active and user_id == user["id"]:
                            db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?",
                                       (user_id, hashlib.sha256(self.session_token().encode()).hexdigest()))
                        else:
                            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                    row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                    db.commit()
                    self.send_json({"user": public_user(row)})
                else:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            except (ValueError, TypeError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc) or "Invalid request.")
            except PermissionError as exc:
                self.send_error_json(HTTPStatus.FORBIDDEN, str(exc))
            except sqlite3.IntegrityError as exc:
                self.send_error_json(HTTPStatus.CONFLICT, "That username is already in use.")

    def session_token(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return cookie["session"].value

    def send_calendar(self, db, user):
        roles = set(json.loads(user["roles"]))
        if roles.isdisjoint({"admin", "dispatcher"}):
            rows = db.execute(
                "SELECT * FROM tickets WHERE appointment_start!='' AND status!='done' AND assignee_id=? ORDER BY appointment_start",
                (user["id"],),
            )
        else:
            rows = db.execute("SELECT * FROM tickets WHERE appointment_start!='' AND status!='done' ORDER BY appointment_start")
        events = []
        for row in rows:
            start = datetime.fromisoformat(row["appointment_start"]).strftime("%Y%m%dT%H%M%S")
            end_value = row["appointment_end"]
            end = datetime.fromisoformat(end_value).strftime("%Y%m%dT%H%M%S") if end_value else ""
            def escape(value):
                return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
            events.extend([
                "BEGIN:VEVENT",
                f"UID:service-{row['id']}@furnacedesk",
                f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART:{start}",
                *([f"DTEND:{end}"] if end else []),
                f"SUMMARY:{escape(row['appliance_model'])} service - {escape(row['customer'])}",
                f"LOCATION:{escape(row['address'])}",
                f"DESCRIPTION:{escape('Phone: ' + row['phone'] + chr(10) + row['notes'])}",
                "END:VEVENT",
            ])
        body = ("\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FurnaceDesk//Service Calendar//EN",
                             "CALSCALE:GREGORIAN", *events, "END:VCALENDAR", ""]).encode("utf-8"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="furnacedesk-calendar.ics"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FurnaceDesk service desk")
    parser.add_argument("--reset-admin-password", metavar="USERNAME", help="Reset an existing administrator password")
    args = parser.parse_args()
    if args.reset_admin_password:
        password = getpass.getpass("New password: ")
        confirmation = getpass.getpass("Confirm new password: ")
        if password != confirmation:
            parser.error("Passwords do not match.")
        try:
            reset_admin_password(args.reset_admin_password, password)
        except ValueError as exc:
            parser.error(str(exc))
        print("Administrator password changed. Existing sessions revoked.")
    else:
        initialize()
        push.start_worker(DB_PATH, DB_PATH.parent / "push-private.pem")
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8000"))
        print(f"FurnaceDesk listening on {host}:{port}", flush=True)
        ThreadingHTTPServer((host, port), ServiceHandler).serve_forever()
