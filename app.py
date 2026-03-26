import csv
import hashlib
import hmac
import io
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import default
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "seaview_crm.db"
HOST = "127.0.0.1"
PORT = 8000
SESSION_COOKIE_NAME = "seaview_session"
SESSION_MAX_AGE = 60 * 60 * 12
DEFAULT_STAFF_USERNAME = os.environ.get("SEAVIEW_CRM_USERNAME", "seaview")
DEFAULT_STAFF_PASSWORD = os.environ.get("SEAVIEW_CRM_PASSWORD", "crabshack-demo")
SESSION_SECRET = os.environ.get("SEAVIEW_SESSION_SECRET", "seaview-internal-demo-secret")

TOUCHPOINT_TYPES = [
    ("website_homepage", "Website signup"),
    ("online_order_flow", "Online order signup"),
    ("in_store_qr", "In-store QR signup"),
    ("counter_conversation", "Counter conversation"),
    ("event_booth", "Event or festival signup"),
    ("wholesale_inquiry", "Wholesale inquiry"),
]

PREFERRED_CHANNELS = [
    ("email", "Email"),
    ("sms", "SMS"),
    ("either", "Either"),
]

CAMPAIGN_CHANNELS = [
    ("email", "Email"),
    ("sms", "SMS"),
    ("social", "Social"),
    ("in_store", "In-store signage"),
]


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOADS_DIR.mkdir(exist_ok=True)


def db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in existing:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db() -> None:
    ensure_dirs()
    conn = db_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id TEXT,
            source_system TEXT NOT NULL,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            phone TEXT,
            city TEXT,
            state TEXT,
            tags TEXT,
            notes TEXT,
            total_spent REAL DEFAULT 0,
            last_purchase_at TEXT,
            preferred_channel TEXT,
            marketing_consent INTEGER NOT NULL DEFAULT 0,
            acquisition_source TEXT,
            last_contacted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS purchase_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            source_system TEXT NOT NULL,
            item_name TEXT,
            quantity INTEGER,
            order_total REAL,
            purchased_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_system TEXT NOT NULL,
            filename TEXT,
            rows_received INTEGER NOT NULL,
            customers_created INTEGER NOT NULL,
            customers_updated INTEGER NOT NULL,
            purchase_events_created INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            offer_details TEXT NOT NULL,
            channel TEXT NOT NULL,
            target_segment TEXT NOT NULL,
            goal TEXT,
            scheduled_for TEXT,
            status TEXT NOT NULL,
            audience_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS touchpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            touchpoint_type TEXT NOT NULL,
            summary TEXT,
            preferred_channel TEXT,
            consent_email INTEGER NOT NULL DEFAULT 0,
            consent_sms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
        CREATE INDEX IF NOT EXISTS idx_purchase_events_customer_id ON purchase_events(customer_id);
        CREATE INDEX IF NOT EXISTS idx_touchpoints_customer_id ON touchpoints(customer_id);
        """
    )

    ensure_column(conn, "customers", "preferred_channel", "TEXT")
    ensure_column(conn, "customers", "marketing_consent", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "customers", "acquisition_source", "TEXT")
    ensure_column(conn, "customers", "last_contacted_at", "TEXT")

    conn.execute(
        """
        UPDATE customers
        SET acquisition_source = source_system
        WHERE COALESCE(acquisition_source, '') = ''
        """
    )
    conn.execute(
        """
        UPDATE customers
        SET preferred_channel = CASE
            WHEN COALESCE(preferred_channel, '') <> '' THEN preferred_channel
            WHEN COALESCE(email, '') <> '' AND COALESCE(phone, '') <> '' THEN 'either'
            WHEN COALESCE(email, '') <> '' THEN 'email'
            WHEN COALESCE(phone, '') <> '' THEN 'sms'
            ELSE NULL
        END
        WHERE COALESCE(preferred_channel, '') = ''
        """
    )
    conn.execute(
        """
        UPDATE customers
        SET marketing_consent = 1
        WHERE marketing_consent = 0
          AND (
              (source_system = 'constant_contact' AND COALESCE(email, '') <> '')
              OR lower(COALESCE(tags, '')) LIKE '%newsletter%'
          )
        """
    )
    conn.commit()
    conn.close()


def seed_demo_data() -> None:
    conn = db_connection()
    now = utc_now()
    customer_count = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]

    if not customer_count:
        demo_customers = [
            (
                "CC-1001",
                "constant_contact",
                "Maria",
                "Lopez",
                "maria@example.com",
                "910-555-0111",
                "Wilmington",
                "NC",
                "newsletter, retail, families",
                "Subscribed from seafood boil promo and responds to seasonal offers.",
                320.50,
                "2026-03-18T14:30:00",
                "email",
                1,
                "newsletter_signup",
                None,
                now,
                now,
            ),
            (
                "CL-2002",
                "clover",
                "James",
                "Carter",
                "jcarter@example.com",
                "910-555-0144",
                "Leland",
                "NC",
                "repeat, wholesale",
                "High-value wholesale contact who orders trays regularly.",
                1450.00,
                "2026-03-20T09:15:00",
                "either",
                1,
                "clover",
                None,
                now,
                now,
            ),
            (
                "LEG-3003",
                "legacy_csv",
                "Dana",
                "Holt",
                "dholt@example.com",
                "910-555-0177",
                "Carolina Beach",
                "NC",
                "legacy, lapsed",
                "Imported from an older spreadsheet that had not been reused.",
                0,
                None,
                "email",
                0,
                "legacy_csv",
                None,
                now,
                now,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO customers (
                external_id, source_system, first_name, last_name, email, phone, city, state,
                tags, notes, total_spent, last_purchase_at, preferred_channel, marketing_consent,
                acquisition_source, last_contacted_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            demo_customers,
        )

        customer_lookup = {
            row["email"]: row["id"] for row in conn.execute("SELECT id, email FROM customers").fetchall()
        }
        conn.executemany(
            """
            INSERT INTO purchase_events (
                customer_id, source_system, item_name, quantity, order_total, purchased_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    customer_lookup["maria@example.com"],
                    "clover",
                    "Blue Crab Special",
                    2,
                    78.0,
                    "2026-03-18T14:30:00",
                    now,
                ),
                (
                    customer_lookup["jcarter@example.com"],
                    "clover",
                    "Wholesale Oyster Tray",
                    10,
                    450.0,
                    "2026-03-20T09:15:00",
                    now,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO import_runs (
                source_system, filename, rows_received, customers_created, customers_updated,
                purchase_events_created, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("demo_seed", "seed_data", 3, 3, 0, 2, "completed", None, now),
        )

    customer_lookup = {
        row["email"]: row["id"] for row in conn.execute("SELECT id, email FROM customers WHERE email IS NOT NULL").fetchall()
    }

    touchpoint_count = conn.execute("SELECT COUNT(*) AS count FROM touchpoints").fetchone()["count"]
    if not touchpoint_count and customer_lookup:
        conn.executemany(
            """
            INSERT INTO touchpoints (
                customer_id, touchpoint_type, summary, preferred_channel, consent_email, consent_sms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    customer_lookup["maria@example.com"],
                    "website_homepage",
                    "Signed up for weekly specials from the homepage banner.",
                    "email",
                    1,
                    0,
                    now,
                ),
                (
                    customer_lookup["jcarter@example.com"],
                    "wholesale_inquiry",
                    "Requested pricing and seasonal inventory updates for wholesale trays.",
                    "either",
                    1,
                    1,
                    now,
                ),
            ],
        )

    campaign_count = conn.execute("SELECT COUNT(*) AS count FROM campaigns").fetchone()["count"]
    if not campaign_count:
        conn.executemany(
            """
            INSERT INTO campaigns (
                title, offer_details, channel, target_segment, goal, scheduled_for, status, audience_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "Weekend Crab Boil Push",
                    "Send a Thursday deal to recent buyers and newsletter subscribers for the weekend special.",
                    "email",
                    "recent_buyers",
                    "Drive weekend repeat visits",
                    "2026-03-28",
                    "scheduled",
                    2,
                    now,
                ),
                (
                    "Wholesale Restock Reminder",
                    "Follow up with wholesale accounts before the next inventory delivery window.",
                    "sms",
                    "wholesale_accounts",
                    "Increase wholesale reorder frequency",
                    None,
                    "draft",
                    1,
                    now,
                ),
            ],
        )

    conn.commit()
    conn.close()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def normalize_header(header: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in header).strip("_")


ALIASES = {
    "external_id": {"external_id", "customer_id", "id", "contact_id"},
    "first_name": {"first_name", "firstname", "first"},
    "last_name": {"last_name", "lastname", "last"},
    "full_name": {"name", "full_name", "customer_name"},
    "email": {"email", "email_address", "customer_email"},
    "phone": {"phone", "phone_number", "mobile", "customer_phone"},
    "city": {"city", "town"},
    "state": {"state", "province"},
    "tags": {"tags", "segment", "segments", "group", "interests"},
    "notes": {"notes", "note", "comments"},
    "order_total": {"order_total", "total", "transaction_total", "amount_spent"},
    "item_name": {"item_name", "item", "product", "product_name"},
    "quantity": {"quantity", "qty"},
    "purchased_at": {"purchased_at", "purchase_date", "date", "last_order_date", "transaction_date"},
    "preferred_channel": {"preferred_channel", "contact_channel"},
    "marketing_consent": {"marketing_consent", "consent", "opt_in", "email_opt_in", "sms_opt_in"},
}


def value_for(row: dict, logical_name: str) -> str:
    aliases = ALIASES[logical_name]
    for key, value in row.items():
        if normalize_header(key) in aliases and value:
            return value.strip()
    return ""


def split_name(full_name: str) -> tuple[str, str]:
    pieces = [piece for piece in full_name.split() if piece]
    if not pieces:
        return "", ""
    if len(pieces) == 1:
        return pieces[0], ""
    return pieces[0], " ".join(pieces[1:])


def to_float(value: str) -> float:
    cleaned = value.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def to_int(value: str) -> int:
    cleaned = value.strip()
    if not cleaned:
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def to_bool(value: str) -> int:
    return 1 if value.strip().lower() in {"1", "true", "yes", "y", "on", "subscribed"} else 0


def parsed_timestamp(value: str) -> str | None:
    parsed = parse_datetime(value.strip())
    return parsed.isoformat() if parsed else (value.strip() or None)


def parse_csv_bytes(payload: bytes) -> list[dict]:
    text = payload.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]


def save_upload(filename: str, payload: bytes) -> str:
    safe_name = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{Path(filename).name or 'upload.csv'}"
    target = UPLOADS_DIR / safe_name
    target.write_bytes(payload)
    return safe_name


def merge_list_text(*values: str) -> str:
    seen = set()
    items = []
    for value in values:
        for piece in value.split(","):
            cleaned = piece.strip()
            lowered = cleaned.lower()
            if cleaned and lowered not in seen:
                seen.add(lowered)
                items.append(cleaned)
    return ", ".join(items)


def merge_notes(*values: str | None) -> str | None:
    notes = []
    seen = set()
    for value in values:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            notes.append(cleaned)
    return " | ".join(notes) if notes else None


def infer_preferred_channel(email: str, phone: str, preferred_channel: str = "") -> str | None:
    if preferred_channel:
        return preferred_channel
    if email and phone:
        return "either"
    if email:
        return "email"
    if phone:
        return "sms"
    return None


def max_timestamp(*values: str | None) -> str | None:
    parsed = [(parse_datetime(value), value) for value in values if value]
    parsed = [item for item in parsed if item[0]]
    if not parsed:
        return next((value for value in values if value), None)
    return max(parsed, key=lambda item: item[0])[1]


def display_name(row: sqlite3.Row | dict) -> str:
    first = (row["first_name"] if isinstance(row, sqlite3.Row) else row.get("first_name")) or ""
    last = (row["last_name"] if isinstance(row, sqlite3.Row) else row.get("last_name")) or ""
    full = f"{first} {last}".strip()
    return full or "Unnamed Customer"


def yes_no(value: int | None) -> str:
    return "Yes" if value else "No"


def upsert_customer_record(
    conn: sqlite3.Connection,
    *,
    source_system: str,
    external_id: str = "",
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    city: str = "",
    state: str = "",
    tags: str = "",
    notes: str = "",
    total_spent: float = 0,
    last_purchase_at: str | None = None,
    preferred_channel: str = "",
    marketing_consent: int = 0,
    acquisition_source: str = "",
) -> tuple[int, str]:
    email = email.strip().lower()
    phone = phone.strip()
    now = utc_now()

    existing = None
    if email:
        existing = conn.execute("SELECT * FROM customers WHERE lower(email) = ?", (email,)).fetchone()
    if not existing and external_id:
        existing = conn.execute(
            "SELECT * FROM customers WHERE external_id = ? AND source_system = ?",
            (external_id, source_system),
        ).fetchone()

    inferred_channel = infer_preferred_channel(email, phone, preferred_channel)

    if existing:
        merged_tags = merge_list_text(existing["tags"] or "", tags)
        merged_notes = merge_notes(existing["notes"], notes)
        conn.execute(
            """
            UPDATE customers
            SET first_name = ?, last_name = ?, email = ?, phone = ?, city = ?, state = ?, tags = ?, notes = ?,
                total_spent = ?, last_purchase_at = ?, preferred_channel = ?, marketing_consent = ?,
                acquisition_source = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                first_name or existing["first_name"],
                last_name or existing["last_name"],
                email or existing["email"],
                phone or existing["phone"],
                city or existing["city"],
                state or existing["state"],
                merged_tags or None,
                merged_notes,
                (existing["total_spent"] or 0) + total_spent,
                max_timestamp(existing["last_purchase_at"], last_purchase_at),
                inferred_channel or existing["preferred_channel"],
                1 if (existing["marketing_consent"] or marketing_consent) else 0,
                existing["acquisition_source"] or acquisition_source or source_system,
                now,
                existing["id"],
            ),
        )
        return existing["id"], "updated"

    conn.execute(
        """
        INSERT INTO customers (
            external_id, source_system, first_name, last_name, email, phone, city, state,
            tags, notes, total_spent, last_purchase_at, preferred_channel, marketing_consent,
            acquisition_source, last_contacted_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            external_id or None,
            source_system,
            first_name or None,
            last_name or None,
            email or None,
            phone or None,
            city or None,
            state or None,
            tags or None,
            notes or None,
            total_spent,
            last_purchase_at,
            inferred_channel,
            marketing_consent,
            acquisition_source or source_system,
            None,
            now,
            now,
        ),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0], "created"


def upsert_customer(conn: sqlite3.Connection, source_system: str, row: dict) -> tuple[int, str]:
    first_name = value_for(row, "first_name")
    last_name = value_for(row, "last_name")
    if not first_name and not last_name:
        first_name, last_name = split_name(value_for(row, "full_name"))

    email = value_for(row, "email")
    phone = value_for(row, "phone")
    acquisition_source = source_system
    marketing_consent = to_bool(value_for(row, "marketing_consent"))
    if not marketing_consent and source_system == "constant_contact" and email:
        marketing_consent = 1

    return upsert_customer_record(
        conn,
        source_system=source_system,
        external_id=value_for(row, "external_id"),
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        city=value_for(row, "city"),
        state=value_for(row, "state"),
        tags=value_for(row, "tags"),
        notes=value_for(row, "notes"),
        total_spent=to_float(value_for(row, "order_total")),
        last_purchase_at=parsed_timestamp(value_for(row, "purchased_at")),
        preferred_channel=value_for(row, "preferred_channel"),
        marketing_consent=marketing_consent,
        acquisition_source=acquisition_source,
    )


def import_rows(source_system: str, filename: str, rows: list[dict]) -> dict:
    conn = db_connection()
    created = 0
    updated = 0
    purchases = 0
    error_message = None
    try:
        for row in rows:
            customer_id, action = upsert_customer(conn, source_system, row)
            if action == "created":
                created += 1
            else:
                updated += 1

            item_name = value_for(row, "item_name")
            order_total = to_float(value_for(row, "order_total"))
            quantity = to_int(value_for(row, "quantity")) or 1
            purchased_at = parsed_timestamp(value_for(row, "purchased_at"))
            if item_name or order_total or purchased_at:
                conn.execute(
                    """
                    INSERT INTO purchase_events (
                        customer_id, source_system, item_name, quantity, order_total, purchased_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (customer_id, source_system, item_name or None, quantity, order_total, purchased_at, utc_now()),
                )
                purchases += 1

        conn.execute(
            """
            INSERT INTO import_runs (
                source_system, filename, rows_received, customers_created, customers_updated,
                purchase_events_created, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_system, filename, len(rows), created, updated, purchases, "completed", None, utc_now()),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        error_message = str(exc)
        conn.execute(
            """
            INSERT INTO import_runs (
                source_system, filename, rows_received, customers_created, customers_updated,
                purchase_events_created, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_system, filename, len(rows), created, updated, purchases, "failed", error_message, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "rows_received": len(rows),
        "customers_created": created,
        "customers_updated": updated,
        "purchase_events_created": purchases,
        "error_message": error_message,
    }


def create_touchpoint_capture(fields: dict, *, public_signup: bool = False) -> dict:
    email = fields.get("email", "").strip()
    phone = fields.get("phone", "").strip()
    if not email and not phone:
        return {"error": "Capture at least an email or phone number so the owners can follow up."}

    touchpoint_type = fields.get("touchpoint_type", "").strip() or ("website_homepage" if public_signup else "counter_conversation")
    preferred_channel = fields.get("preferred_channel", "").strip()
    consent_email = 1 if fields.get("consent_email") else 0
    consent_sms = 1 if fields.get("consent_sms") else 0
    marketing_consent = 1 if consent_email or consent_sms else 0
    interest_tags = fields.get("interest_tags", "").strip()
    summary = fields.get("notes", "").strip()
    source_note = f"Captured via {touchpoint_label(touchpoint_type)}."
    merged_notes = merge_notes(source_note, summary)
    merged_tags = merge_list_text(interest_tags, "captured lead", touchpoint_type.replace("_", " "))

    conn = db_connection()
    try:
        customer_id, action = upsert_customer_record(
            conn,
            source_system="touchpoint_capture",
            first_name=fields.get("first_name", "").strip(),
            last_name=fields.get("last_name", "").strip(),
            email=email,
            phone=phone,
            tags=merged_tags,
            notes=merged_notes or "",
            preferred_channel=preferred_channel,
            marketing_consent=marketing_consent,
            acquisition_source=touchpoint_type,
        )
        conn.execute(
            """
            INSERT INTO touchpoints (
                customer_id, touchpoint_type, summary, preferred_channel, consent_email, consent_sms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                touchpoint_type,
                summary or source_note,
                infer_preferred_channel(email, phone, preferred_channel),
                consent_email,
                consent_sms,
                utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {"customer_id": customer_id, "action": action, "error": None}


def segment_definitions() -> dict:
    recent_cutoff = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
    lapsed_cutoff = (datetime.now(UTC) - timedelta(days=45)).replace(microsecond=0).isoformat()
    signup_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    contact_clause = "(COALESCE(email, '') <> '' OR COALESCE(phone, '') <> '')"

    return {
        "email_ready": {
            "label": "Email-ready audience",
            "description": "Customers with an email address ready for newsletter or deal outreach.",
            "recommended_channel": "Email",
            "where": "COALESCE(email, '') <> ''",
            "params": (),
        },
        "sms_ready": {
            "label": "SMS-ready audience",
            "description": "Customers with a phone number that can receive fast, timely offers.",
            "recommended_channel": "SMS",
            "where": "COALESCE(phone, '') <> ''",
            "params": (),
        },
        "recent_buyers": {
            "label": "Recent buyers",
            "description": "Customers who purchased in the last 30 days and should get a follow-up deal.",
            "recommended_channel": "Email",
            "where": f"last_purchase_at IS NOT NULL AND last_purchase_at >= ? AND {contact_clause}",
            "params": (recent_cutoff,),
        },
        "lapsed_buyers": {
            "label": "Lapsed buyers",
            "description": "Customers who bought before but have gone quiet and need a win-back offer.",
            "recommended_channel": "SMS",
            "where": f"last_purchase_at IS NOT NULL AND last_purchase_at < ? AND total_spent > 0 AND {contact_clause}",
            "params": (lapsed_cutoff,),
        },
        "vip_customers": {
            "label": "VIP customers",
            "description": "Top spenders who should get first access to premium or seasonal inventory.",
            "recommended_channel": "Email",
            "where": f"total_spent >= 250 AND {contact_clause}",
            "params": (),
        },
        "newsletter_prospects": {
            "label": "Newsletter prospects",
            "description": "Reachable contacts with little or no purchase history who need nurturing.",
            "recommended_channel": "Email",
            "where": "COALESCE(email, '') <> '' AND total_spent = 0",
            "params": (),
        },
        "wholesale_accounts": {
            "label": "Wholesale accounts",
            "description": "Customers tagged as wholesale for B2B follow-up and restock reminders.",
            "recommended_channel": "Either",
            "where": "lower(COALESCE(tags, '')) LIKE '%wholesale%'",
            "params": (),
        },
        "new_signups": {
            "label": "New signups this week",
            "description": "Fresh website or in-person captures that should get a welcome offer fast.",
            "recommended_channel": "Email or SMS",
            "where": "acquisition_source IN ('website_homepage', 'online_order_flow', 'in_store_qr', 'counter_conversation', 'event_booth') AND created_at >= ?",
            "params": (signup_cutoff,),
        },
        "missing_contact": {
            "label": "Missing contact info",
            "description": "Customers with no email or phone who need a capture campaign in person.",
            "recommended_channel": "In-store",
            "where": "COALESCE(email, '') = '' AND COALESCE(phone, '') = ''",
            "params": (),
        },
    }


def fetch_segment_rows_with_conn(conn: sqlite3.Connection, segment_key: str) -> list[sqlite3.Row]:
    segments = segment_definitions()
    if segment_key not in segments:
        return []
    segment = segments[segment_key]
    return conn.execute(
        f"""
        SELECT id, first_name, last_name, email, phone, preferred_channel, marketing_consent,
               acquisition_source, tags, total_spent, last_purchase_at
        FROM customers
        WHERE {segment['where']}
        ORDER BY total_spent DESC, updated_at DESC
        """,
        segment["params"],
    ).fetchall()


def fetch_segment_rows(segment_key: str) -> list[sqlite3.Row]:
    conn = db_connection()
    rows = fetch_segment_rows_with_conn(conn, segment_key)
    conn.close()
    return rows


def create_campaign(fields: dict) -> dict:
    segment_key = fields.get("target_segment", "").strip()
    if segment_key not in segment_definitions():
        return {"error": "Choose a valid audience segment for the campaign."}

    title = fields.get("title", "").strip()
    offer_details = fields.get("offer_details", "").strip()
    if not title or not offer_details:
        return {"error": "Campaign title and offer details are required."}

    scheduled_for = fields.get("scheduled_for", "").strip()
    scheduled_for = parsed_timestamp(scheduled_for) if scheduled_for else None
    conn = db_connection()
    try:
        audience_count = len(fetch_segment_rows_with_conn(conn, segment_key))
        conn.execute(
            """
            INSERT INTO campaigns (
                title, offer_details, channel, target_segment, goal, scheduled_for, status, audience_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                offer_details,
                fields.get("channel", "email").strip() or "email",
                segment_key,
                fields.get("goal", "").strip() or None,
                scheduled_for,
                "scheduled" if scheduled_for else "draft",
                audience_count,
                utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"error": None, "audience_count": audience_count}


def dashboard_metrics() -> dict:
    conn = db_connection()
    week_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    metrics = {
        "customers": conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"],
        "imports": conn.execute("SELECT COUNT(*) AS count FROM import_runs").fetchone()["count"],
        "revenue": conn.execute("SELECT COALESCE(SUM(total_spent), 0) AS total FROM customers").fetchone()["total"],
        "contactable": conn.execute(
            "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> '' OR COALESCE(phone, '') <> ''"
        ).fetchone()["count"],
        "touchpoints_this_week": conn.execute(
            "SELECT COUNT(*) AS count FROM touchpoints WHERE created_at >= ?",
            (week_cutoff,),
        ).fetchone()["count"],
        "scheduled_campaigns": conn.execute(
            "SELECT COUNT(*) AS count FROM campaigns WHERE status IN ('draft', 'scheduled')"
        ).fetchone()["count"],
        "recent_imports": conn.execute(
            "SELECT * FROM import_runs ORDER BY created_at DESC LIMIT 5"
        ).fetchall(),
        "top_customers": conn.execute(
            """
            SELECT id, first_name, last_name, email, total_spent, tags
            FROM customers
            ORDER BY total_spent DESC, updated_at DESC
            LIMIT 5
            """
        ).fetchall(),
        "recent_touchpoints": conn.execute(
            """
            SELECT t.*, c.first_name, c.last_name, c.email, c.phone
            FROM touchpoints t
            JOIN customers c ON c.id = t.customer_id
            ORDER BY t.created_at DESC
            LIMIT 5
            """
        ).fetchall(),
    }
    conn.close()
    return metrics


def weekly_playbook(segment_counts: dict) -> list[dict]:
    return [
        {
            "title": "Follow recent buyers quickly",
            "body": f"Export the recent buyers segment ({segment_counts['recent_buyers']}) and send a next-visit deal within 7 days of purchase.",
            "segment": "recent_buyers",
        },
        {
            "title": "Run one win-back offer every week",
            "body": f"The lapsed buyers segment has {segment_counts['lapsed_buyers']} contacts. Use a short time-bound offer to reactivate them.",
            "segment": "lapsed_buyers",
        },
        {
            "title": "Use premium inventory for VIP retention",
            "body": f"There are {segment_counts['vip_customers']} high-value customers. Give them early notice on seasonal specials before the general list.",
            "segment": "vip_customers",
        },
        {
            "title": "Capture missing contact info at checkout",
            "body": f"{segment_counts['missing_contact']} customers still have no email or phone on file. Push QR signups and counter asks every week.",
            "segment": "missing_contact",
        },
    ]


def marketing_snapshot() -> dict:
    conn = db_connection()
    segments = []
    segment_counts = {}
    for key, segment in segment_definitions().items():
        count = conn.execute(
            f"SELECT COUNT(*) AS count FROM customers WHERE {segment['where']}",
            segment["params"],
        ).fetchone()["count"]
        segment_counts[key] = count
        segments.append(
            {
                "key": key,
                "label": segment["label"],
                "description": segment["description"],
                "recommended_channel": segment["recommended_channel"],
                "count": count,
            }
        )

    week_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    snapshot = {
        "segments": segments,
        "playbook": weekly_playbook(segment_counts),
        "contactable": conn.execute(
            "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> '' OR COALESCE(phone, '') <> ''"
        ).fetchone()["count"],
        "email_ready": segment_counts["email_ready"],
        "sms_ready": segment_counts["sms_ready"],
        "missing_contact": segment_counts["missing_contact"],
        "new_touchpoints": conn.execute(
            "SELECT COUNT(*) AS count FROM touchpoints WHERE created_at >= ?",
            (week_cutoff,),
        ).fetchone()["count"],
        "recent_campaigns": conn.execute(
            "SELECT * FROM campaigns ORDER BY COALESCE(scheduled_for, created_at) DESC LIMIT 8"
        ).fetchall(),
        "recent_touchpoints": conn.execute(
            """
            SELECT t.*, c.first_name, c.last_name, c.email, c.phone
            FROM touchpoints t
            JOIN customers c ON c.id = t.customer_id
            ORDER BY t.created_at DESC
            LIMIT 8
            """
        ).fetchall(),
    }
    conn.close()
    return snapshot


def list_customers(search: str = "") -> list[sqlite3.Row]:
    conn = db_connection()
    if search:
        like = f"%{search.lower()}%"
        rows = conn.execute(
            """
            SELECT *
            FROM customers
            WHERE lower(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) LIKE ?
               OR lower(COALESCE(email, '')) LIKE ?
               OR lower(COALESCE(tags, '')) LIKE ?
               OR lower(COALESCE(acquisition_source, '')) LIKE ?
            ORDER BY updated_at DESC
            """,
            (like, like, like, like),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM customers ORDER BY updated_at DESC").fetchall()
    conn.close()
    return rows


def get_customer(customer_id: int) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
    conn = db_connection()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    events = conn.execute(
        """
        SELECT * FROM purchase_events
        WHERE customer_id = ?
        ORDER BY purchased_at DESC, created_at DESC
        """,
        (customer_id,),
    ).fetchall()
    touchpoints = conn.execute(
        """
        SELECT * FROM touchpoints
        WHERE customer_id = ?
        ORDER BY created_at DESC
        """,
        (customer_id,),
    ).fetchall()
    conn.close()
    return customer, events, touchpoints


def touchpoint_label(value: str) -> str:
    labels = dict(TOUCHPOINT_TYPES)
    return labels.get(value, value.replace("_", " ").title())


def channel_label(value: str | None) -> str:
    labels = dict(PREFERRED_CHANNELS + CAMPAIGN_CHANNELS)
    return labels.get(value or "", (value or "").replace("_", " ").title())


def customer_context_labels(customer: sqlite3.Row) -> list[str]:
    labels = []
    if customer["total_spent"] and customer["total_spent"] >= 250:
        labels.append("VIP")
    if customer["email"]:
        labels.append("Email reachable")
    if customer["phone"]:
        labels.append("SMS reachable")
    if not customer["email"] and not customer["phone"]:
        labels.append("Needs contact capture")
    if "wholesale" in (customer["tags"] or "").lower():
        labels.append("Wholesale")
    last_purchase = parse_datetime(customer["last_purchase_at"])
    if last_purchase:
        days_since = (datetime.now(UTC) - last_purchase).days
        if days_since <= 30:
            labels.append("Recent buyer")
        if days_since > 45:
            labels.append("Lapsed")
    elif customer["email"]:
        labels.append("Newsletter prospect")
    return labels


def export_segment_csv(segment_key: str) -> bytes:
    rows = fetch_segment_rows(segment_key)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "customer_id",
            "name",
            "email",
            "phone",
            "preferred_channel",
            "marketing_consent",
            "acquisition_source",
            "tags",
            "total_spent",
            "last_purchase_at",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["id"],
                display_name(row),
                row["email"] or "",
                row["phone"] or "",
                row["preferred_channel"] or "",
                yes_no(row["marketing_consent"]),
                row["acquisition_source"] or "",
                row["tags"] or "",
                f"{(row['total_spent'] or 0):.2f}",
                row["last_purchase_at"] or "",
            ]
        )
    return output.getvalue().encode("utf-8")


def option_list(options: list[tuple[str, str]], selected: str = "") -> str:
    html = []
    for value, label in options:
        is_selected = " selected" if value == selected else ""
        html.append(f"<option value='{escape(value)}'{is_selected}>{escape(label)}</option>")
    return "".join(html)


def message_query(message: str) -> str:
    return urlencode({"message": message})


def session_signature(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def session_cookie_value(username: str) -> str:
    expires_at = int(time.time()) + SESSION_MAX_AGE
    payload = f"{username}|{expires_at}"
    return f"{payload}|{session_signature(payload)}"


def clear_session_cookie_value() -> str:
    return f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def auth_cookie_header(username: str) -> str:
    return (
        f"{SESSION_COOKIE_NAME}={session_cookie_value(username)}; "
        f"Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"
    )


def authenticated_username(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None

    session = cookie.get(SESSION_COOKIE_NAME)
    if not session or not session.value:
        return None

    try:
        username, expires_at, signature = session.value.split("|", 2)
    except ValueError:
        return None

    payload = f"{username}|{expires_at}"
    if not hmac.compare_digest(signature, session_signature(payload)):
        return None

    try:
        if int(expires_at) < int(time.time()):
            return None
    except ValueError:
        return None
    return username


def valid_staff_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, DEFAULT_STAFF_USERNAME) and hmac.compare_digest(password, DEFAULT_STAFF_PASSWORD)


def base_layout(title: str, body: str, flash: str = "") -> bytes:
    flash_html = f"<div class='flash'>{escape(flash)}</div>" if flash else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <h1>Seaview CRM</h1>
      <p>Contextual customer records and systematic, timely marketing for Seaview Crab.</p>
      <nav>
        <a href="/">Dashboard</a>
        <a href="/customers">Customers</a>
        <a href="/marketing">Marketing</a>
        <a href="/imports">Imports</a>
        <a href="/capture">Lead Capture</a>
        <a href="/logout">Logout</a>
      </nav>
    </aside>
    <main class="content">
      {flash_html}
      {body}
    </main>
  </div>
  <script src="/static/app.js" defer></script>
</body>
</html>"""
    return html.encode("utf-8")


def render_login(message: str = "") -> bytes:
    body = f"""
    <section class="hero public-hero auth-hero">
      <div>
        <h2>Seaview Staff Login</h2>
        <p>This CRM is framed as an internal system for Seaview owners and workers only. For this demo build, the login screen is just a presentation gate before entering the system.</p>
      </div>
    </section>

    <div class="panel signup-panel auth-panel">
      <h3>Internal access</h3>
      <form method="post" action="/login" class="stack">
        <label>Username
          <input type="text" name="username" autocomplete="username">
        </label>
        <label>Password
          <input type="password" name="password" autocomplete="current-password">
        </label>
        <button type="submit">Sign in</button>
      </form>
      <p class="muted">Demo mode: any username and password will enter the app right now.</p>
    </div>
    """
    return public_layout("Seaview Staff Login", body, flash=message)


def public_layout(title: str, body: str, flash: str = "") -> bytes:
    flash_html = f"<div class='flash'>{escape(flash)}</div>" if flash else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body class="public-view">
  <main class="public-shell">
    {flash_html}
    {body}
  </main>
  <script src="/static/app.js" defer></script>
</body>
</html>"""
    return html.encode("utf-8")


def render_dashboard(message: str = "") -> bytes:
    metrics = dashboard_metrics()
    snapshot = marketing_snapshot()
    imports_html = "".join(
        f"<tr><td>{escape(row['created_at'])}</td><td>{escape(row['source_system'])}</td><td>{escape(row['filename'] or '')}</td><td>{row['rows_received']}</td><td>{escape(row['status'])}</td></tr>"
        for row in metrics["recent_imports"]
    ) or "<tr><td colspan='5'>No imports yet.</td></tr>"
    top_customers_html = "".join(
        f"<li><a href='/customers/{row['id']}'>{escape(display_name(row))}</a><span>{escape(row['email'] or 'No email')}</span><strong>${row['total_spent']:.2f}</strong></li>"
        for row in metrics["top_customers"]
    ) or "<li>No customer records yet.</li>"
    touchpoints_html = "".join(
        f"<li><strong>{escape(display_name(row))}</strong><span>{escape(touchpoint_label(row['touchpoint_type']))}</span><span>{escape(row['created_at'])}</span></li>"
        for row in metrics["recent_touchpoints"]
    ) or "<li>No touchpoints yet.</li>"
    playbook_html = "".join(
        f"<li><strong>{escape(item['title'])}</strong><span>{escape(item['body'])}</span></li>"
        for item in snapshot["playbook"]
    )
    body = f"""
    <section class="hero">
      <div>
        <h2>CRM plus marketing operations in one place</h2>
        <p>This system centralizes Seaview's customer context, tracks how people are captured from website and in-person touchpoints, and turns that data into a weekly marketing rhythm.</p>
      </div>
      <div class="button-row">
        <a class="button" href="/marketing">Open marketing hub</a>
        <a class="button secondary" href="/imports">Upload fragmented data</a>
      </div>
    </section>

    <section class="stats">
      <article><span>Total customers</span><strong>{metrics['customers']}</strong></article>
      <article><span>Reachable contacts</span><strong>{metrics['contactable']}</strong></article>
      <article><span>Touchpoints this week</span><strong>{metrics['touchpoints_this_week']}</strong></article>
      <article><span>Open campaigns</span><strong>{metrics['scheduled_campaigns']}</strong></article>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Weekly marketing playbook</h3>
        <ul class="stacked-list">{playbook_html}</ul>
      </div>
      <div class="panel">
        <h3>Recent imports</h3>
        <table>
          <thead><tr><th>Date</th><th>Source</th><th>File</th><th>Rows</th><th>Status</th></tr></thead>
          <tbody>{imports_html}</tbody>
        </table>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Top customers</h3>
        <ul class="ranked">{top_customers_html}</ul>
      </div>
      <div class="panel">
        <h3>Recent captures and interactions</h3>
        <ul class="stacked-list">{touchpoints_html}</ul>
      </div>
    </section>
    """
    return base_layout("Seaview CRM Dashboard", body, flash=message)


def render_customers(search: str = "") -> bytes:
    rows = list_customers(search)
    customer_rows = "".join(
        f"<tr><td><a href='/customers/{row['id']}'>{escape(display_name(row))}</a></td><td>{escape(row['email'] or '')}</td><td>{escape(row['phone'] or '')}</td><td>{escape(channel_label(row['preferred_channel']) or 'Unknown')}</td><td>{escape(row['acquisition_source'] or row['source_system'])}</td><td>{escape(row['tags'] or '')}</td><td>${(row['total_spent'] or 0):.2f}</td></tr>"
        for row in rows
    ) or "<tr><td colspan='7'>No customers found.</td></tr>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Customers</h2>
        <p>Each customer profile is the context layer for Seaview: identity, source, spend, touchpoints, and marketing readiness in one record.</p>
      </div>
      <form method="get" class="search">
        <input type="text" name="q" value="{escape(search)}" placeholder="Search by name, email, source, or tag">
        <button type="submit">Search</button>
      </form>
    </section>
    <div class="panel">
      <table>
        <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Preferred</th><th>Source</th><th>Tags</th><th>Total spend</th></tr></thead>
        <tbody>{customer_rows}</tbody>
      </table>
    </div>
    """
    return base_layout("Customers", body)


def render_customer_detail(customer_id: int) -> bytes:
    customer, events, touchpoints = get_customer(customer_id)
    if not customer:
        return base_layout("Customer Not Found", "<div class='panel'><h2>Customer not found</h2></div>")

    event_rows = "".join(
        f"<tr><td>{escape(row['purchased_at'] or '')}</td><td>{escape(row['item_name'] or '')}</td><td>{row['quantity'] or 0}</td><td>${(row['order_total'] or 0):.2f}</td><td>{escape(row['source_system'])}</td></tr>"
        for row in events
    ) or "<tr><td colspan='5'>No purchase history yet.</td></tr>"

    touchpoint_rows = "".join(
        f"<tr><td>{escape(row['created_at'])}</td><td>{escape(touchpoint_label(row['touchpoint_type']))}</td><td>{escape(row['summary'] or '')}</td><td>{escape(channel_label(row['preferred_channel']) or '')}</td><td>{yes_no(row['consent_email'])}/{yes_no(row['consent_sms'])}</td></tr>"
        for row in touchpoints
    ) or "<tr><td colspan='5'>No touchpoints recorded yet.</td></tr>"

    context_pills = "".join(f"<span class='pill'>{escape(label)}</span>" for label in customer_context_labels(customer))
    body = f"""
    <section class="page-head">
      <div>
        <h2>{escape(display_name(customer))}</h2>
        <p>{escape(customer['email'] or 'No email on file')} | {escape(customer['phone'] or 'No phone on file')}</p>
        <div class="pill-row">{context_pills}</div>
      </div>
      <a class="button secondary" href="/customers">Back to customers</a>
    </section>
    <section class="grid">
      <div class="panel">
        <h3>Profile context</h3>
        <dl class="details">
          <dt>Primary source</dt><dd>{escape(customer['source_system'])}</dd>
          <dt>Acquisition source</dt><dd>{escape(customer['acquisition_source'] or '')}</dd>
          <dt>Preferred channel</dt><dd>{escape(channel_label(customer['preferred_channel']) or 'Unknown')}</dd>
          <dt>Marketing consent</dt><dd>{yes_no(customer['marketing_consent'])}</dd>
          <dt>Location</dt><dd>{escape(' '.join(filter(None, [customer['city'], customer['state']])) or 'Unknown')}</dd>
          <dt>Tags</dt><dd>{escape(customer['tags'] or '')}</dd>
          <dt>Total spend</dt><dd>${(customer['total_spent'] or 0):.2f}</dd>
          <dt>Last purchase</dt><dd>{escape(customer['last_purchase_at'] or 'Unknown')}</dd>
          <dt>Last contacted</dt><dd>{escape(customer['last_contacted_at'] or 'Not tracked yet')}</dd>
          <dt>Notes</dt><dd>{escape(customer['notes'] or '')}</dd>
        </dl>
      </div>
      <div class="panel">
        <h3>Purchase history</h3>
        <table>
          <thead><tr><th>Date</th><th>Item</th><th>Qty</th><th>Total</th><th>Source</th></tr></thead>
          <tbody>{event_rows}</tbody>
        </table>
      </div>
    </section>
    <div class="panel">
      <h3>Touchpoints and lead capture</h3>
      <table>
        <thead><tr><th>Date</th><th>Type</th><th>Summary</th><th>Preferred</th><th>Email/SMS consent</th></tr></thead>
        <tbody>{touchpoint_rows}</tbody>
      </table>
    </div>
    """
    return base_layout(display_name(customer), body)


def render_marketing(message: str = "") -> bytes:
    snapshot = marketing_snapshot()
    segment_rows = "".join(
        f"<tr><td><strong>{escape(segment['label'])}</strong><div class='muted'>{escape(segment['description'])}</div></td><td>{segment['count']}</td><td>{escape(segment['recommended_channel'])}</td><td><a class='button secondary small' href='/marketing/export?segment={escape(segment['key'])}'>Export CSV</a></td></tr>"
        for segment in snapshot["segments"]
    )
    campaign_rows = "".join(
        f"<tr><td>{escape(row['title'])}</td><td>{escape(segment_definitions().get(row['target_segment'], {}).get('label', row['target_segment']))}</td><td>{escape(channel_label(row['channel']))}</td><td>{row['audience_count']}</td><td>{escape(row['status'])}</td><td>{escape(row['scheduled_for'] or '')}</td></tr>"
        for row in snapshot["recent_campaigns"]
    ) or "<tr><td colspan='6'>No campaigns yet.</td></tr>"
    touchpoint_rows = "".join(
        f"<tr><td>{escape(row['created_at'])}</td><td>{escape(display_name(row))}</td><td>{escape(touchpoint_label(row['touchpoint_type']))}</td><td>{escape(row['email'] or row['phone'] or '')}</td><td>{escape(channel_label(row['preferred_channel']) or '')}</td></tr>"
        for row in snapshot["recent_touchpoints"]
    ) or "<tr><td colspan='5'>No touchpoints yet.</td></tr>"
    playbook_cards = "".join(
        f"<article class='card'><h4>{escape(item['title'])}</h4><p>{escape(item['body'])}</p><a href='/marketing/export?segment={escape(item['segment'])}'>Open audience</a></article>"
        for item in snapshot["playbook"]
    )
    body = f"""
    <section class="page-head">
      <div>
        <h2>Marketing Hub</h2>
        <p>The CRM is the system of record. Marketing sits directly on top of it: capture people, segment them, plan weekly deals, and export targeted outreach lists.</p>
      </div>
      <div class="button-row">
        <a class="button" href="/capture">Open lead capture page</a>
        <a class="button secondary" href="/imports">Import latest system exports</a>
      </div>
    </section>

    <section class="stats">
      <article><span>Email-ready</span><strong>{snapshot['email_ready']}</strong></article>
      <article><span>SMS-ready</span><strong>{snapshot['sms_ready']}</strong></article>
      <article><span>Missing contact info</span><strong>{snapshot['missing_contact']}</strong></article>
      <article><span>Touchpoints this week</span><strong>{snapshot['new_touchpoints']}</strong></article>
    </section>

    <section class="cards">
      {playbook_cards}
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Plan a campaign or deal</h3>
        <form method="post" action="/marketing/campaigns" class="stack">
          <label>Campaign title
            <input type="text" name="title" placeholder="Weekend seafood special">
          </label>
          <label>Offer details
            <textarea name="offer_details" rows="4" placeholder="Describe the deal, hook, and CTA."></textarea>
          </label>
          <div class="field-grid">
            <label>Channel
              <select name="channel">{option_list(CAMPAIGN_CHANNELS, 'email')}</select>
            </label>
            <label>Target segment
              <select name="target_segment">{option_list([(key, segment['label']) for key, segment in segment_definitions().items()], 'recent_buyers')}</select>
            </label>
          </div>
          <div class="field-grid">
            <label>Goal
              <input type="text" name="goal" placeholder="Drive repeat visits this weekend">
            </label>
            <label>Scheduled date
              <input type="date" name="scheduled_for">
            </label>
          </div>
          <button type="submit">Save campaign</button>
        </form>
      </div>
      <div class="panel">
        <h3>Capture a website or in-person lead</h3>
        <form method="post" action="/touchpoints" class="stack">
          <div class="field-grid">
            <label>First name
              <input type="text" name="first_name">
            </label>
            <label>Last name
              <input type="text" name="last_name">
            </label>
          </div>
          <div class="field-grid">
            <label>Email
              <input type="email" name="email">
            </label>
            <label>Phone
              <input type="text" name="phone">
            </label>
          </div>
          <div class="field-grid">
            <label>Touchpoint type
              <select name="touchpoint_type">{option_list(TOUCHPOINT_TYPES, 'counter_conversation')}</select>
            </label>
            <label>Preferred channel
              <select name="preferred_channel">{option_list(PREFERRED_CHANNELS, 'email')}</select>
            </label>
          </div>
          <label>Interest tags
            <input type="text" name="interest_tags" placeholder="family meals, crab boil, wholesale">
          </label>
          <label>Context / notes
            <textarea name="notes" rows="3" placeholder="What did they ask for or respond to?"></textarea>
          </label>
          <div class="checkbox-row">
            <label><input type="checkbox" name="consent_email" value="1"> Email opt-in</label>
            <label><input type="checkbox" name="consent_sms" value="1"> SMS opt-in</label>
          </div>
          <button type="submit">Save customer touchpoint</button>
        </form>
      </div>
    </section>

    <div class="panel">
      <h3>Audience segments</h3>
      <table>
        <thead><tr><th>Segment</th><th>Count</th><th>Best channel</th><th>Action</th></tr></thead>
        <tbody>{segment_rows}</tbody>
      </table>
    </div>

    <section class="grid">
      <div class="panel">
        <h3>Recent campaigns</h3>
        <table>
          <thead><tr><th>Campaign</th><th>Segment</th><th>Channel</th><th>Audience</th><th>Status</th><th>Scheduled</th></tr></thead>
          <tbody>{campaign_rows}</tbody>
        </table>
      </div>
      <div class="panel">
        <h3>Recent touchpoints</h3>
        <table>
          <thead><tr><th>Date</th><th>Customer</th><th>Touchpoint</th><th>Contact</th><th>Preferred</th></tr></thead>
          <tbody>{touchpoint_rows}</tbody>
        </table>
      </div>
    </section>

    <div class="panel">
      <h3>Weekly operating rhythm</h3>
      <ul class="stacked-list">
        <li><strong>Monday:</strong><span>Import the latest Clover, Constant Contact, and website export files so the CRM starts the week current.</span></li>
        <li><strong>Daily:</strong><span>Have staff capture contact details after online inquiries, QR scans, and counter conversations so every meaningful interaction reaches the CRM.</span></li>
        <li><strong>Wednesday:</strong><span>Review recent buyers, VIPs, and lapsed segments. Save one campaign for the week and export its audience list.</span></li>
        <li><strong>Thursday/Friday:</strong><span>Push the deal through the current email or SMS tool, then use touchpoints to record what is resonating in person.</span></li>
      </ul>
    </div>
    """
    return base_layout("Marketing Hub", body, flash=message)


def render_imports(message: str = "") -> bytes:
    conn = db_connection()
    recent = conn.execute("SELECT * FROM import_runs ORDER BY created_at DESC LIMIT 10").fetchall()
    conn.close()
    history_rows = "".join(
        f"<tr><td>{escape(row['created_at'])}</td><td>{escape(row['source_system'])}</td><td>{escape(row['filename'] or '')}</td><td>{row['rows_received']}</td><td>{row['customers_created']}</td><td>{row['customers_updated']}</td><td>{row['purchase_events_created']}</td><td>{escape(row['status'])}</td></tr>"
        for row in recent
    ) or "<tr><td colspan='8'>No imports yet.</td></tr>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Data imports</h2>
        <p>Import fragmented customer data from Clover, Constant Contact, old Shopify exports, and manual spreadsheets so the CRM stays current for marketing decisions every week.</p>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Upload CSV</h3>
        <form method="post" action="/imports" enctype="multipart/form-data" class="stack import-form">
          <label>Source system
            <select name="source_system">
              <option value="clover">Clover export</option>
              <option value="constant_contact">Constant Contact export</option>
              <option value="shopify_legacy">Legacy Shopify export</option>
              <option value="website_signup">Website signup export</option>
              <option value="legacy_csv">Other spreadsheet / manual export</option>
            </select>
          </label>
          <div class="import-upload">
            <span class="import-field-label">CSV file</span>
            <label class="import-dropzone" for="csv_file" data-dropzone tabindex="0" aria-label="Upload CSV by dragging and dropping or browsing">
              <input class="sr-only" id="csv_file" type="file" name="csv_file" accept=".csv,text/csv" data-dropzone-input>
              <span class="import-dropzone-title">Drag and drop your CSV here</span>
              <span class="import-dropzone-subtitle">or click to browse from your computer</span>
              <span class="import-dropzone-actions">
                <span class="button secondary import-dropzone-button">Choose file</span>
                <span class="import-dropzone-filename" data-file-name>No file chosen</span>
              </span>
            </label>
          </div>
          <button type="submit" class="import-submit">Import data</button>
        </form>
      </div>
      <div class="panel">
        <h3>Weekly import notes</h3>
        <p>Best-supported columns: <code>name</code>, <code>first_name</code>, <code>last_name</code>, <code>email</code>, <code>phone</code>, <code>tags</code>, <code>notes</code>, <code>order_total</code>, <code>item_name</code>, <code>quantity</code>, <code>purchase_date</code>, <code>preferred_channel</code>, and <code>consent</code>.</p>
        <p>Matching is done by email first, then by external customer ID. That lets Seaview keep consolidating old records into one customer view over time.</p>
      </div>
    </section>

    <div class="panel">
      <h3>Import history</h3>
      <table>
        <thead><tr><th>Date</th><th>Source</th><th>File</th><th>Rows</th><th>Created</th><th>Updated</th><th>Purchases</th><th>Status</th></tr></thead>
        <tbody>{history_rows}</tbody>
      </table>
    </div>
    """
    return base_layout("Imports", body, flash=message)


def render_signup(message: str = "") -> bytes:
    body = f"""
    <section class="hero public-hero">
      <div>
        <h2>Staff lead capture</h2>
        <p>Use this internal page on a staff device to record contact details from checkout conversations, phone calls, website inquiries, and event interactions. Customers do not log into the CRM directly.</p>
      </div>
    </section>

    <div class="panel signup-panel">
      <h3>Capture a new contact</h3>
      <form method="post" action="/capture" class="stack">
        <div class="field-grid">
          <label>First name
            <input type="text" name="first_name">
          </label>
          <label>Last name
            <input type="text" name="last_name">
          </label>
        </div>
        <div class="field-grid">
          <label>Email
            <input type="email" name="email">
          </label>
          <label>Phone
            <input type="text" name="phone">
          </label>
        </div>
        <div class="field-grid">
          <label>Capture source
            <select name="touchpoint_type">{option_list(TOUCHPOINT_TYPES, 'website_homepage')}</select>
          </label>
          <label>Preferred contact
            <select name="preferred_channel">{option_list(PREFERRED_CHANNELS, 'email')}</select>
          </label>
        </div>
        <label>What are you interested in?
          <input type="text" name="interest_tags" placeholder="blue crab, oysters, family packs, wholesale">
        </label>
        <label>Staff notes
          <textarea name="notes" rows="3" placeholder="What did the customer ask about or respond to?"></textarea>
        </label>
        <div class="checkbox-row">
          <label><input type="checkbox" name="consent_email" value="1"> Customer approved email outreach</label>
          <label><input type="checkbox" name="consent_sms" value="1"> Customer approved SMS outreach</label>
        </div>
        <button type="submit">Save captured contact</button>
      </form>
      <p class="muted">Operational use: this stays behind staff login and is intended for internal lead capture only.</p>
    </div>
    """
    return public_layout("Seaview Lead Capture", body, flash=message)


class SeaviewCRMHandler(BaseHTTPRequestHandler):
    server_version = "SeaviewCRM/0.2"

    def is_authenticated(self) -> bool:
        return authenticated_username(self.headers.get("Cookie")) is not None

    def requires_auth(self, path: str) -> bool:
        if path == "/login":
            return False
        if path.startswith("/static/"):
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        message = params.get("message", [""])[0]

        if self.requires_auth(parsed.path) and not self.is_authenticated():
            self.respond_redirect("/login?" + message_query("Staff login required."))
            return

        if parsed.path == "/login":
            if self.is_authenticated():
                self.respond_redirect("/")
                return
            self.respond_html(render_login(message))
            return
        if parsed.path == "/":
            self.respond_html(render_dashboard(message))
            return
        if parsed.path == "/customers":
            self.respond_html(render_customers(params.get("q", [""])[0]))
            return
        if parsed.path.startswith("/customers/"):
            try:
                customer_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.respond_not_found()
                return
            self.respond_html(render_customer_detail(customer_id))
            return
        if parsed.path == "/marketing/export":
            segment_key = params.get("segment", [""])[0]
            if segment_key not in segment_definitions():
                self.respond_not_found()
                return
            payload = export_segment_csv(segment_key)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", f"attachment; filename={segment_key}.csv")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/marketing":
            self.respond_html(render_marketing(message))
            return
        if parsed.path == "/imports":
            self.respond_html(render_imports(message))
            return
        if parsed.path in {"/signup", "/capture"}:
            self.respond_html(render_signup(message))
            return
        if parsed.path == "/logout":
            self.respond_redirect("/login?" + message_query("You have been signed out."), headers={"Set-Cookie": clear_session_cookie_value()})
            return
        if parsed.path == "/static/styles.css":
            css = (BASE_DIR / "static" / "styles.css").read_bytes()
            self.respond_bytes(css, "text/css; charset=utf-8")
            return
        if parsed.path == "/static/app.js":
            js = (BASE_DIR / "static" / "app.js").read_bytes()
            self.respond_bytes(js, "text/javascript; charset=utf-8")
            return
        self.respond_not_found()

    def do_POST(self) -> None:
        if self.path == "/login":
            fields = self.parse_urlencoded()
            username = fields.get("username", "").strip() or "seaview-demo"
            self.respond_redirect("/", headers={"Set-Cookie": auth_cookie_header(username)})
            return

        if self.requires_auth(self.path) and not self.is_authenticated():
            self.respond_redirect("/login?" + message_query("Staff login required."))
            return

        if self.path == "/imports":
            self.handle_import_upload()
            return
        if self.path == "/marketing/campaigns":
            fields = self.parse_urlencoded()
            result = create_campaign(fields)
            if result["error"]:
                self.respond_redirect(f"/marketing?{message_query(result['error'])}")
                return
            self.respond_redirect(
                f"/marketing?{message_query(f'Campaign saved for {result['audience_count']} customers.')}"
            )
            return
        if self.path == "/touchpoints":
            fields = self.parse_urlencoded()
            result = create_touchpoint_capture(fields, public_signup=False)
            if result["error"]:
                self.respond_redirect(f"/marketing?{message_query(result['error'])}")
                return
            self.respond_redirect("/marketing?" + message_query("Customer touchpoint saved and CRM record updated."))
            return
        if self.path in {"/signup", "/capture"}:
            fields = self.parse_urlencoded()
            result = create_touchpoint_capture(fields, public_signup=False)
            if result["error"]:
                self.respond_redirect(f"/capture?{message_query(result['error'])}")
                return
            self.respond_redirect("/capture?" + message_query("Captured contact saved to the CRM."))
            return
        self.respond_not_found()

    def handle_import_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.respond_redirect("/imports?" + message_query("Upload failed: expected multipart form data."))
            return

        form = self.parse_multipart()
        source_system = (form["fields"].get("source_system") or "legacy_csv").strip()
        file_part = form["files"].get("csv_file")
        if not file_part or not file_part["content"]:
            self.respond_redirect("/imports?" + message_query("Choose a CSV file before importing."))
            return

        filename = save_upload(file_part["filename"], file_part["content"])
        try:
            rows = parse_csv_bytes(file_part["content"])
        except Exception:
            self.respond_redirect("/imports?" + message_query("Upload failed: file could not be parsed as CSV."))
            return

        result = import_rows(source_system, filename, rows)
        if result["error_message"]:
            self.respond_redirect("/imports?" + message_query("Import failed."))
            return
        message = (
            f"Imported {result['rows_received']} rows. Created {result['customers_created']} customers, "
            f"updated {result['customers_updated']}, and added {result['purchase_events_created']} purchase records."
        )
        self.respond_redirect("/imports?" + message_query(message))

    def parse_multipart(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        content_type = self.headers["Content-Type"]
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw
        )

        fields = {}
        files = {}
        for part in message.iter_parts():
            disposition = part.get("Content-Disposition", "")
            if "form-data" not in disposition:
                continue
            name = part.get_param("name", header="Content-Disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = {"filename": filename, "content": payload}
            else:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return {"fields": fields, "files": files}

    def parse_urlencoded(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def respond_html(self, payload: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(payload, "text/html; charset=utf-8", status=status)

    def respond_bytes(self, payload: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def respond_redirect(self, location: str, headers: dict | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()

    def respond_not_found(self) -> None:
        self.respond_html(base_layout("Not Found", "<div class='panel'><h2>Not found</h2></div>"), status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return


def run() -> None:
    init_db()
    seed_demo_data()
    server = ThreadingHTTPServer((HOST, PORT), SeaviewCRMHandler)
    print(f"Seaview CRM running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
