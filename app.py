import csv
import hashlib
import hmac
import io
import os
import sqlite3
import time
import uuid
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
HOST = os.environ.get("HOST", "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_COOKIE_NAME = "seaview_session"
SESSION_MAX_AGE = 60 * 60 * 12
PENDING_IMPORT_TTL_SECONDS = 60 * 30
DEFAULT_STAFF_USERNAME = os.environ.get("SEAVIEW_CRM_USERNAME", "seaview")
DEFAULT_STAFF_PASSWORD = os.environ.get("SEAVIEW_CRM_PASSWORD", "crabshack-demo")
SESSION_SECRET = os.environ.get("SEAVIEW_SESSION_SECRET", "seaview-internal-demo-secret")

TOUCHPOINT_TYPES = [
    ("website_homepage", "Website specials signup"),
    ("online_order_flow", "Online order signup"),
    ("in_store_qr", "In-store QR signup"),
    ("receipt_qr", "Receipt or bag QR"),
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

CAMPAIGN_STATUSES = [
    ("draft", "Draft"),
    ("scheduled", "Scheduled"),
    ("sent", "Sent"),
]

OUTREACH_EVENT_TYPES = [
    ("saved", "Saved"),
    ("exported", "Audience exported"),
    ("sent", "Marked sent"),
]

TASK_TYPES = [
    ("follow_up", "Follow-up"),
    ("campaign", "Campaign"),
    ("import", "Import"),
    ("general", "General"),
]

IMPORT_FIELD_LABELS = {
    "external_id": "External ID",
    "first_name": "First name",
    "last_name": "Last name",
    "full_name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "city": "City",
    "state": "State",
    "tags": "Tags",
    "notes": "Notes",
    "order_total": "Order total",
    "item_name": "Item name",
    "quantity": "Quantity",
    "purchased_at": "Purchase date",
    "preferred_channel": "Preferred channel",
    "marketing_consent": "Marketing consent",
}

IMPORT_FIELD_GROUPS = [
    ("Identity", ["external_id", "email", "phone", "full_name", "first_name", "last_name"]),
    ("Marketing context", ["tags", "notes", "preferred_channel", "marketing_consent", "city", "state"]),
    ("Purchase context", ["order_total", "item_name", "quantity", "purchased_at"]),
]

PENDING_IMPORTS: dict[str, dict] = {}
STATIC_ASSET_CACHE: dict[str, tuple[float, bytes]] = {}
PUBLIC_CAPTURE_VARIANTS = {
    "/join": {
        "label": "Website specials page",
        "headline": "Get Seaview specials and fresh catch alerts",
        "description": "A lightweight public signup page for weekly seafood specials, preorder alerts, and seasonal inventory updates.",
        "touchpoint_type": "website_homepage",
        "preferred_channel": "email",
        "cta": "Join Seaview updates",
        "interest_placeholder": "blue crab, shrimp platters, oyster specials, family packs",
        "capture_note": "Use this on the website homepage, footer, social bio links, and email signatures.",
        "benefits": [
            "Weekly seafood specials and seasonal catch updates",
            "A simple opt-in that creates a usable CRM contact record",
            "Better first-party customer data for repeat outreach",
        ],
    },
    "/join/qr": {
        "label": "In-store QR page",
        "headline": "Scan for weekly seafood specials",
        "description": "A mobile-first QR destination for receipts, counter cards, table tents, bags, and event signage.",
        "touchpoint_type": "in_store_qr",
        "preferred_channel": "sms",
        "cta": "Send me the deals",
        "interest_placeholder": "weekend specials, crab boil, family trays, fresh catch",
        "capture_note": "Best for QR codes at checkout, on packaging, and at event booths where staff need a fast opt-in flow.",
        "benefits": [
            "Captures contact info without slowing down checkout",
            "Turns in-person traffic into reachable CRM leads",
            "Works well for time-sensitive SMS or email offers",
        ],
    },
    "/join/wholesale": {
        "label": "Wholesale inquiry page",
        "headline": "Get wholesale inventory and pricing updates",
        "description": "A focused contact page for restaurant, retail, and partner accounts that need restock and availability updates.",
        "touchpoint_type": "wholesale_inquiry",
        "preferred_channel": "either",
        "cta": "Request wholesale updates",
        "interest_placeholder": "restaurant supply, oyster trays, soft shell crab, seasonal inventory",
        "capture_note": "Use this for a wholesale link on the website or in direct outreach with prospective business accounts.",
        "benefits": [
            "Separates wholesale demand from consumer demand",
            "Creates a stronger follow-up pipeline for B2B contacts",
            "Feeds the CRM with a clear acquisition source for segmentation",
        ],
    },
}
PUBLIC_CAPTURE_TOUCHPOINTS = tuple(page["touchpoint_type"] for page in PUBLIC_CAPTURE_VARIANTS.values())


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
            skipped_rows INTEGER NOT NULL DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS outreach_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            channel TEXT,
            segment_key TEXT,
            audience_count INTEGER NOT NULL DEFAULT 0,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            title TEXT NOT NULL,
            details TEXT,
            task_type TEXT NOT NULL,
            due_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE TABLE IF NOT EXISTS customer_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
        CREATE INDEX IF NOT EXISTS idx_customers_updated_at ON customers(updated_at);
        CREATE INDEX IF NOT EXISTS idx_customers_last_purchase_at ON customers(last_purchase_at);
        CREATE INDEX IF NOT EXISTS idx_purchase_events_customer_id ON purchase_events(customer_id);
        CREATE INDEX IF NOT EXISTS idx_purchase_events_customer_purchased_at ON purchase_events(customer_id, purchased_at);
        CREATE INDEX IF NOT EXISTS idx_touchpoints_customer_id ON touchpoints(customer_id);
        CREATE INDEX IF NOT EXISTS idx_touchpoints_customer_created_at ON touchpoints(customer_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_touchpoints_created_at ON touchpoints(created_at);
        CREATE INDEX IF NOT EXISTS idx_import_runs_created_at ON import_runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_campaigns_status_scheduled_for ON campaigns(status, scheduled_for, created_at);
        CREATE INDEX IF NOT EXISTS idx_outreach_history_created_at ON outreach_history(created_at);
        CREATE INDEX IF NOT EXISTS idx_outreach_history_campaign_id ON outreach_history(campaign_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_status_due_at ON tasks(status, due_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_customer_id ON tasks(customer_id);
        CREATE INDEX IF NOT EXISTS idx_customer_notes_customer_id ON customer_notes(customer_id, created_at);
        """
    )

    ensure_column(conn, "customers", "preferred_channel", "TEXT")
    ensure_column(conn, "customers", "marketing_consent", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "customers", "acquisition_source", "TEXT")
    ensure_column(conn, "customers", "last_contacted_at", "TEXT")
    ensure_column(conn, "import_runs", "skipped_rows", "INTEGER NOT NULL DEFAULT 0")

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

    outreach_count = conn.execute("SELECT COUNT(*) AS count FROM outreach_history").fetchone()["count"]
    if not outreach_count:
        campaigns = conn.execute(
            "SELECT id, title, channel, target_segment, audience_count, status FROM campaigns ORDER BY id"
        ).fetchall()
        if campaigns:
            seed_events = []
            for row in campaigns:
                seed_events.append(
                    (
                        row["id"],
                        "saved",
                        row["title"],
                        row["channel"],
                        row["target_segment"],
                        row["audience_count"],
                        "Campaign saved from the marketing workspace.",
                        now,
                    )
                )
            if len(campaigns) > 1:
                second = campaigns[1]
                seed_events.append(
                    (
                        second["id"],
                        "exported",
                        second["title"],
                        second["channel"],
                        second["target_segment"],
                        second["audience_count"],
                        "Audience exported to CSV for outreach planning.",
                        now,
                    )
                )
            conn.executemany(
                """
                INSERT INTO outreach_history (
                    campaign_id, event_type, title, channel, segment_key, audience_count, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                seed_events,
            )

    task_count = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()["count"]
    if not task_count and customer_lookup:
        conn.executemany(
            """
            INSERT INTO tasks (
                customer_id, title, details, task_type, due_at, status, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    customer_lookup["maria@example.com"],
                    "Follow up on newsletter signup",
                    "Send a welcome offer and note which seasonal specials she responds to.",
                    "follow_up",
                    "2026-04-05",
                    "open",
                    now,
                    None,
                ),
                (
                    customer_lookup["jcarter@example.com"],
                    "Check wholesale reorder timing",
                    "Confirm expected reorder date before the next delivery window.",
                    "campaign",
                    "2026-04-06",
                    "open",
                    now,
                    None,
                ),
                (
                    None,
                    "Import latest Clover export",
                    "Refresh customer and purchase data before this week's outreach.",
                    "import",
                    "2026-04-04",
                    "open",
                    now,
                    None,
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
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


def preview_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return list(rows[0].keys())


def preview_rows(rows: list[dict], limit: int = 5) -> list[dict]:
    return rows[:limit]


def matched_preview_columns(columns: list[str], logical_name: str) -> list[str]:
    aliases = ALIASES[logical_name]
    return [column for column in columns if normalize_header(column) in aliases]


def import_row_is_identifiable(row: dict) -> bool:
    first_name = value_for(row, "first_name")
    last_name = value_for(row, "last_name")
    full_name = value_for(row, "full_name")
    return any(
        [
            value_for(row, "external_id"),
            value_for(row, "email"),
            value_for(row, "phone"),
            full_name,
            first_name and last_name,
        ]
    )


def analyze_import_rows(rows: list[dict]) -> dict:
    columns = preview_columns(rows)
    mapped_fields = []
    mapped_field_keys = set()
    for group_label, field_keys in IMPORT_FIELD_GROUPS:
        fields = []
        for field_key in field_keys:
            matched_columns = matched_preview_columns(columns, field_key)
            if not matched_columns:
                continue
            mapped_field_keys.add(field_key)
            fields.append(
                {
                    "key": field_key,
                    "label": IMPORT_FIELD_LABELS[field_key],
                    "columns": matched_columns,
                }
            )
        mapped_fields.append({"group": group_label, "fields": fields})

    unmapped_columns = [
        column
        for column in columns
        if not any(column in field["columns"] for group in mapped_fields for field in group["fields"])
    ]
    identifiable_rows = sum(1 for row in rows if import_row_is_identifiable(row))
    contactable_rows = sum(1 for row in rows if value_for(row, "email") or value_for(row, "phone"))
    purchase_rows = sum(
        1
        for row in rows
        if value_for(row, "item_name") or value_for(row, "order_total") or value_for(row, "purchased_at")
    )
    consent_rows = sum(1 for row in rows if to_bool(value_for(row, "marketing_consent")))
    skipped_rows = max(len(rows) - identifiable_rows, 0)

    warnings = []
    blocking_warnings = []
    identity_columns_present = any(
        matched_preview_columns(columns, key)
        for key in ("external_id", "email", "phone", "full_name", "first_name", "last_name")
    )
    if not rows:
        blocking_warnings.append("No data rows were detected in this file. Upload a CSV with a header row and customer records.")
    if not identity_columns_present:
        blocking_warnings.append("No supported identity columns were detected. Add email, phone, external ID, or customer name fields before importing.")
    elif identifiable_rows == 0:
        blocking_warnings.append("None of the rows contain enough identity data to create or update a customer record.")
    elif skipped_rows:
        warnings.append(f"{skipped_rows} rows are missing usable identity data and will be skipped during import.")
    if contactable_rows == 0:
        warnings.append("No rows include email or phone, so the file will add history but not create reachable marketing contacts.")
    if purchase_rows == 0:
        warnings.append("No purchase fields were detected, so this import will only update customer profiles.")
    if unmapped_columns:
        warnings.append(f"{len(unmapped_columns)} columns are not mapped yet and will be ignored.")

    return {
        "columns": columns,
        "mapped_fields": mapped_fields,
        "unmapped_columns": unmapped_columns,
        "identifiable_rows": identifiable_rows,
        "contactable_rows": contactable_rows,
        "purchase_rows": purchase_rows,
        "consent_rows": consent_rows,
        "skipped_rows": skipped_rows,
        "warnings": warnings,
        "blocking_warnings": blocking_warnings,
        "can_import": not blocking_warnings and identifiable_rows > 0,
    }


def save_upload(filename: str, payload: bytes) -> str:
    safe_name = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{Path(filename).name or 'upload.csv'}"
    target = UPLOADS_DIR / safe_name
    target.write_bytes(payload)
    return safe_name


def public_capture_page(path: str) -> dict | None:
    page = PUBLIC_CAPTURE_VARIANTS.get(path)
    if not page:
        return None
    return {"path": path, **page}


def public_capture_pages() -> list[dict]:
    return [public_capture_page(path) for path in PUBLIC_CAPTURE_VARIANTS]


def prune_pending_imports(now: float | None = None) -> None:
    cutoff = (now or time.time()) - PENDING_IMPORT_TTL_SECONDS
    expired_ids = [
        import_id
        for import_id, pending in PENDING_IMPORTS.items()
        if pending.get("created_at_ts", 0) < cutoff
    ]
    for import_id in expired_ids:
        PENDING_IMPORTS.pop(import_id, None)


def static_asset_bytes(relative_path: str) -> bytes:
    asset_path = BASE_DIR / relative_path
    stat = asset_path.stat()
    cache_key = str(asset_path)
    cached = STATIC_ASSET_CACHE.get(cache_key)
    if cached and cached[0] == stat.st_mtime:
        return cached[1]
    payload = asset_path.read_bytes()
    STATIC_ASSET_CACHE[cache_key] = (stat.st_mtime, payload)
    return payload


def asset_url(relative_path: str) -> str:
    asset_path = BASE_DIR / relative_path
    version = int(asset_path.stat().st_mtime)
    return f"/{relative_path}?v={version}"


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


def display_timestamp(value: str | None, *, include_time: bool = True) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return value or ""
    if include_time:
        return parsed.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


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
    skipped = 0
    purchases = 0
    error_message = None
    try:
        for row in rows:
            if not import_row_is_identifiable(row):
                skipped += 1
                continue
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
                source_system, filename, rows_received, customers_created, customers_updated, skipped_rows,
                purchase_events_created, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_system, filename, len(rows), created, updated, skipped, purchases, "completed", None, utc_now()),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        error_message = str(exc)
        conn.execute(
            """
            INSERT INTO import_runs (
                source_system, filename, rows_received, customers_created, customers_updated, skipped_rows,
                purchase_events_created, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_system, filename, len(rows), created, updated, skipped, purchases, "failed", error_message, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "rows_received": len(rows),
        "customers_created": created,
        "customers_updated": updated,
        "skipped_rows": skipped,
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
    source_label = fields.get("source_label", "").strip()
    capture_offer = fields.get("capture_offer", "").strip()
    source_note = f"Captured via {touchpoint_label(touchpoint_type)}."
    offer_note = f"Signup hook: {capture_offer}." if capture_offer else None
    source_label_note = f"Entry point: {source_label}." if source_label else None
    merged_notes = merge_notes(source_note, source_label_note, offer_note, summary)
    merged_tags = merge_list_text(
        interest_tags,
        "captured lead",
        touchpoint_type.replace("_", " "),
        "public signup" if public_signup else "",
    )

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
        conn.execute(
            """
            UPDATE customers
            SET last_contacted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), utc_now(), customer_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"customer_id": customer_id, "action": action, "error": None}


def task_type_label(value: str) -> str:
    return dict(TASK_TYPES).get(value, value.replace("_", " ").title())


def list_tasks_with_conn(
    conn: sqlite3.Connection, *, status: str = "open", limit: int = 20, customer_id: int | None = None
) -> list[sqlite3.Row]:
    where_clauses = ["t.status = ?"]
    params: list = [status]
    if customer_id is not None:
        where_clauses.append("t.customer_id = ?")
        params.append(customer_id)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT
            t.*,
            c.first_name,
            c.last_name,
            c.email
        FROM tasks t
        LEFT JOIN customers c ON c.id = t.customer_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY
            CASE WHEN t.due_at IS NULL OR t.due_at = '' THEN 1 ELSE 0 END,
            t.due_at ASC,
            t.created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()


def list_tasks(*, status: str = "open", limit: int = 20, customer_id: int | None = None) -> list[sqlite3.Row]:
    conn = db_connection()
    try:
        return list_tasks_with_conn(conn, status=status, limit=limit, customer_id=customer_id)
    finally:
        conn.close()


def task_counts_with_conn(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM tasks
        GROUP BY status
        """
    ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    return {
        "open": counts.get("open", 0),
        "completed": counts.get("completed", 0),
    }


def create_task(fields: dict) -> dict:
    title = fields.get("title", "").strip()
    if not title:
        return {"error": "Add a task title."}

    task_type = fields.get("task_type", "").strip() or "follow_up"
    if task_type not in {value for value, _label in TASK_TYPES}:
        task_type = "follow_up"

    customer_id_raw = fields.get("customer_id", "").strip()
    customer_id = None
    if customer_id_raw:
        try:
            customer_id = int(customer_id_raw)
        except ValueError:
            return {"error": "Customer reference is invalid."}

    due_at = fields.get("due_at", "").strip()
    due_at = parsed_timestamp(due_at) if due_at else None

    conn = db_connection()
    try:
        if customer_id is not None:
            customer = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
            if not customer:
                return {"error": "Customer not found for this task."}
        conn.execute(
            """
            INSERT INTO tasks (
                customer_id, title, details, task_type, due_at, status, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, 'open', ?, NULL)
            """,
            (
                customer_id,
                title,
                fields.get("details", "").strip() or None,
                task_type,
                due_at,
                utc_now(),
            ),
        )
        task_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return {"error": None, "task_id": task_id}
    finally:
        conn.close()


def complete_task(task_id: int) -> bool:
    conn = db_connection()
    try:
        result = conn.execute(
            """
            UPDATE tasks
            SET status = 'completed', completed_at = ?
            WHERE id = ? AND status <> 'completed'
            """,
            (utc_now(), task_id),
        )
        conn.commit()
        return result.rowcount > 0
    finally:
        conn.close()


def log_outreach_event_with_conn(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    title: str,
    channel: str | None = None,
    segment_key: str | None = None,
    audience_count: int = 0,
    details: str | None = None,
    campaign_id: int | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO outreach_history (
            campaign_id, event_type, title, channel, segment_key, audience_count, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id,
            event_type,
            title,
            channel,
            segment_key,
            audience_count,
            details,
            created_at or utc_now(),
        ),
    )


def get_campaign_with_conn(conn: sqlite3.Connection, campaign_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()


def update_customers_last_contacted_for_segment_with_conn(
    conn: sqlite3.Connection, segment_key: str, touched_at: str, segments: dict | None = None
) -> int:
    active_segments = segments or segment_definitions()
    segment = active_segments.get(segment_key)
    if not segment:
        return 0
    result = conn.execute(
        f"""
        UPDATE customers
        SET last_contacted_at = ?, updated_at = ?
        WHERE {segment['where']}
        """,
        (touched_at, touched_at, *segment["params"]),
    )
    return result.rowcount


def recent_outreach_history_with_conn(conn: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM outreach_history
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def count_segment_rows_with_conn(
    conn: sqlite3.Connection, segment_key: str, segments: dict | None = None
) -> int:
    active_segments = segments or segment_definitions()
    if segment_key not in active_segments:
        return 0
    segment = active_segments[segment_key]
    return conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {segment['where']}",
        segment["params"],
    ).fetchone()["count"]


def segment_counts_with_conn(conn: sqlite3.Connection, segments: dict | None = None) -> dict[str, int]:
    active_segments = segments or segment_definitions()
    return {
        key: count_segment_rows_with_conn(conn, key, active_segments)
        for key in active_segments
    }


def lead_capture_snapshot_with_conn(conn: sqlite3.Connection) -> dict:
    week_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    grouped_rows = conn.execute(
        """
        SELECT
            touchpoint_type,
            COUNT(*) AS count,
            SUM(CASE WHEN consent_email = 1 OR consent_sms = 1 THEN 1 ELSE 0 END) AS opted_in
        FROM touchpoints
        GROUP BY touchpoint_type
        ORDER BY count DESC, touchpoint_type
        """
    ).fetchall()
    source_map = {
        row["touchpoint_type"]: {
            "count": row["count"],
            "opted_in": row["opted_in"] or 0,
        }
        for row in grouped_rows
    }
    public_this_week = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM touchpoints
        WHERE touchpoint_type IN ({", ".join("?" for _ in PUBLIC_CAPTURE_TOUCHPOINTS)})
          AND created_at >= ?
        """,
        (*PUBLIC_CAPTURE_TOUCHPOINTS, week_cutoff),
    ).fetchone()["count"]
    website_total = sum(source_map.get(key, {}).get("count", 0) for key in ("website_homepage", "online_order_flow"))
    qr_total = sum(source_map.get(key, {}).get("count", 0) for key in ("in_store_qr", "receipt_qr", "event_booth"))
    wholesale_total = source_map.get("wholesale_inquiry", {}).get("count", 0)
    public_total = sum(source_map.get(key, {}).get("count", 0) for key in PUBLIC_CAPTURE_TOUCHPOINTS)
    opted_in_total = sum(source_map.get(key, {}).get("opted_in", 0) for key in PUBLIC_CAPTURE_TOUCHPOINTS)
    sources = [
        {
            "key": key,
            "label": touchpoint_label(key),
            "count": source_map.get(key, {}).get("count", 0),
        }
        for key in PUBLIC_CAPTURE_TOUCHPOINTS
        if source_map.get(key, {}).get("count", 0)
    ]
    sources.sort(key=lambda item: (-item["count"], item["label"]))
    return {
        "public_this_week": public_this_week,
        "public_total": public_total,
        "opted_in_total": opted_in_total,
        "website_total": website_total,
        "qr_total": qr_total,
        "wholesale_total": wholesale_total,
        "sources": sources,
        "source_map": source_map,
    }


def dashboard_insights(metrics: dict) -> list[dict]:
    segment_counts = metrics["segment_counts"]
    lead_capture = metrics["lead_capture"]
    return [
        {
            "title": "Win-back audience",
            "body": f"{segment_counts['lapsed_buyers']} customers are ready for a comeback offer.",
            "href": "/marketing",
            "label": "Open marketing",
        },
        {
            "title": "Contact capture",
            "body": f"{segment_counts['missing_contact']} records still need email or phone, while {lead_capture['public_this_week']} new public leads came in this week.",
            "href": "/capture",
            "label": "Capture lead",
        },
        {
            "title": "VIP activity",
            "body": f"{segment_counts['vip_customers']} high-value customers are available for targeted outreach.",
            "href": "/customers",
            "label": "View customers",
        },
    ]


def marketing_focus(snapshot: dict) -> list[dict]:
    segment_counts = snapshot["segment_counts"]
    return [
        {
            "title": "Recent buyers",
            "body": f"{segment_counts['recent_buyers']} customers are still warm from the last 30 days.",
        },
        {
            "title": "Lapsed buyers",
            "body": f"{segment_counts['lapsed_buyers']} customers need a win-back message.",
        },
        {
            "title": "New signups",
            "body": f"{segment_counts['new_signups']} new signups are ready for a welcome offer.",
        },
    ]


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


def fetch_segment_rows_with_conn(
    conn: sqlite3.Connection, segment_key: str, segments: dict | None = None
) -> list[sqlite3.Row]:
    active_segments = segments or segment_definitions()
    if segment_key not in active_segments:
        return []
    segment = active_segments[segment_key]
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
    segments = segment_definitions()
    segment_key = fields.get("target_segment", "").strip()
    if segment_key not in segments:
        return {"error": "Choose a valid audience segment for the campaign."}

    title = fields.get("title", "").strip()
    offer_details = fields.get("offer_details", "").strip()
    if not title or not offer_details:
        return {"error": "Campaign title and offer details are required."}

    scheduled_for = fields.get("scheduled_for", "").strip()
    scheduled_for = parsed_timestamp(scheduled_for) if scheduled_for else None
    status = fields.get("status", "").strip() or ("scheduled" if scheduled_for else "draft")
    if status not in {item[0] for item in CAMPAIGN_STATUSES}:
        status = "draft"

    conn = db_connection()
    try:
        channel = fields.get("channel", "email").strip() or "email"
        created_at = utc_now()
        audience_count = count_segment_rows_with_conn(conn, segment_key, segments)
        conn.execute(
            """
            INSERT INTO campaigns (
                title, offer_details, channel, target_segment, goal, scheduled_for, status, audience_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                offer_details,
                channel,
                segment_key,
                fields.get("goal", "").strip() or None,
                scheduled_for,
                status,
                audience_count,
                created_at,
            ),
        )
        campaign_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        log_outreach_event_with_conn(
            conn,
            campaign_id=campaign_id,
            event_type="saved",
            title=title,
            channel=channel,
            segment_key=segment_key,
            audience_count=audience_count,
            details="Campaign saved from the marketing workspace.",
            created_at=created_at,
        )
        if status == "sent":
            update_customers_last_contacted_for_segment_with_conn(conn, segment_key, created_at, segments)
            log_outreach_event_with_conn(
                conn,
                campaign_id=campaign_id,
                event_type="sent",
                title=title,
                channel=channel,
                segment_key=segment_key,
                audience_count=audience_count,
                details="Campaign was created as already sent.",
                created_at=created_at,
            )
        conn.commit()
    finally:
        conn.close()
    return {"error": None, "audience_count": audience_count, "campaign_id": campaign_id, "status": status}


def log_segment_export(segment_key: str, campaign_id: int | None = None) -> None:
    segments = segment_definitions()
    if segment_key not in segments:
        return
    conn = db_connection()
    try:
        campaign = get_campaign_with_conn(conn, campaign_id) if campaign_id else None
        if campaign and campaign["target_segment"] != segment_key:
            campaign = None
        title = campaign["title"] if campaign else segments[segment_key]["label"]
        channel = campaign["channel"] if campaign else None
        audience_count = campaign["audience_count"] if campaign else count_segment_rows_with_conn(conn, segment_key, segments)
        log_outreach_event_with_conn(
            conn,
            campaign_id=campaign["id"] if campaign else None,
            event_type="exported",
            title=title,
            channel=channel,
            segment_key=segment_key,
            audience_count=audience_count,
            details="Audience exported to CSV for outreach.",
        )
        conn.commit()
    finally:
        conn.close()


def mark_campaign_sent(campaign_id: int) -> dict:
    conn = db_connection()
    try:
        campaign = get_campaign_with_conn(conn, campaign_id)
        if not campaign:
            return {"error": "Campaign not found."}
        if campaign["status"] == "sent":
            return {
                "error": None,
                "already_sent": True,
                "title": campaign["title"],
                "audience_count": campaign["audience_count"],
                "customers_updated": 0,
            }
        sent_at = utc_now()
        conn.execute("UPDATE campaigns SET status = 'sent' WHERE id = ?", (campaign_id,))
        customers_updated = update_customers_last_contacted_for_segment_with_conn(
            conn, campaign["target_segment"], sent_at
        )
        log_outreach_event_with_conn(
            conn,
            campaign_id=campaign_id,
            event_type="sent",
            title=campaign["title"],
            channel=campaign["channel"],
            segment_key=campaign["target_segment"],
            audience_count=campaign["audience_count"],
            details="Campaign marked sent from the marketing workspace.",
            created_at=sent_at,
        )
        conn.commit()
        return {
            "error": None,
            "already_sent": False,
            "title": campaign["title"],
            "audience_count": campaign["audience_count"],
            "customers_updated": customers_updated,
        }
    finally:
        conn.close()


def dashboard_metrics_with_conn(
    conn: sqlite3.Connection,
    *,
    segment_counts: dict[str, int] | None = None,
    lead_capture: dict | None = None,
) -> dict:
    week_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    active_segment_counts = segment_counts or segment_counts_with_conn(conn)
    active_lead_capture = lead_capture or lead_capture_snapshot_with_conn(conn)
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
        "open_tasks": task_counts_with_conn(conn)["open"],
        "next_tasks": list_tasks_with_conn(conn, status="open", limit=5),
        "segment_counts": active_segment_counts,
        "lead_capture": active_lead_capture,
    }
    return metrics


def dashboard_metrics() -> dict:
    conn = db_connection()
    try:
        return dashboard_metrics_with_conn(conn)
    finally:
        conn.close()


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


def marketing_snapshot_with_conn(
    conn: sqlite3.Connection,
    *,
    segments: dict | None = None,
    segment_counts: dict[str, int] | None = None,
    lead_capture: dict | None = None,
) -> dict:
    active_segments = segments or segment_definitions()
    active_segment_counts = segment_counts or segment_counts_with_conn(conn, active_segments)
    active_lead_capture = lead_capture or lead_capture_snapshot_with_conn(conn)
    segments = []
    for key, segment in active_segments.items():
        count = active_segment_counts[key]
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
        "segment_counts": active_segment_counts,
        "playbook": weekly_playbook(active_segment_counts),
        "contactable": conn.execute(
            "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> '' OR COALESCE(phone, '') <> ''"
        ).fetchone()["count"],
        "email_ready": active_segment_counts["email_ready"],
        "sms_ready": active_segment_counts["sms_ready"],
        "missing_contact": active_segment_counts["missing_contact"],
        "new_touchpoints": conn.execute(
            "SELECT COUNT(*) AS count FROM touchpoints WHERE created_at >= ?",
            (week_cutoff,),
        ).fetchone()["count"],
        "recent_campaigns": conn.execute(
            "SELECT * FROM campaigns ORDER BY COALESCE(scheduled_for, created_at) DESC LIMIT 8"
        ).fetchall(),
        "campaign_totals": conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM campaigns
            GROUP BY status
            """
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
        "recent_outreach": recent_outreach_history_with_conn(conn),
        "lead_capture": active_lead_capture,
        "capture_pages": public_capture_pages(),
    }
    return snapshot


def marketing_snapshot() -> dict:
    conn = db_connection()
    try:
        return marketing_snapshot_with_conn(conn)
    finally:
        conn.close()


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


def get_customer_with_conn(conn: sqlite3.Connection, customer_id: int) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
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
    return customer, events, touchpoints


def get_customer(customer_id: int) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
    conn = db_connection()
    try:
        return get_customer_with_conn(conn, customer_id)
    finally:
        conn.close()


def get_customer_record(customer_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
    owns_connection = conn is None
    active_conn = conn or db_connection()
    row = active_conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if owns_connection:
        active_conn.close()
    return row


def customer_tasks(customer_id: int, *, status: str = "open", limit: int = 10) -> list[sqlite3.Row]:
    return list_tasks(status=status, limit=limit, customer_id=customer_id)


def customer_tasks_with_conn(conn: sqlite3.Connection, customer_id: int, limit: int = 12) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE customer_id = ?
        ORDER BY
            CASE WHEN status = 'open' THEN 0 ELSE 1 END,
            CASE WHEN due_at IS NULL OR due_at = '' THEN 1 ELSE 0 END,
            due_at ASC,
            COALESCE(completed_at, created_at) DESC
        LIMIT ?
        """,
        (customer_id, limit),
    ).fetchall()


def list_customer_notes_with_conn(conn: sqlite3.Connection, customer_id: int, limit: int = 8) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM customer_notes
        WHERE customer_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (customer_id, limit),
    ).fetchall()


def add_customer_note(customer_id: int, body: str) -> dict:
    note = body.strip()
    if not note:
        return {"error": "Add a note before saving it."}

    conn = db_connection()
    try:
        customer = get_customer_record(customer_id, conn=conn)
        if not customer:
            return {"error": "Customer not found."}
        created_at = utc_now()
        conn.execute(
            """
            INSERT INTO customer_notes (customer_id, body, created_at)
            VALUES (?, ?, ?)
            """,
            (customer_id, note, created_at),
        )
        conn.execute("UPDATE customers SET updated_at = ? WHERE id = ?", (created_at, customer_id))
        conn.commit()
        return {"error": None}
    finally:
        conn.close()


def customer_matches_segment_with_conn(
    conn: sqlite3.Connection, customer_id: int, segment_key: str, segments: dict | None = None
) -> bool:
    active_segments = segments or segment_definitions()
    segment = active_segments.get(segment_key)
    if not segment:
        return False
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE id = ? AND {segment['where']}
        """,
        (customer_id, *segment["params"]),
    ).fetchone()
    return bool(row["count"])


def customer_campaign_activity_with_conn(
    conn: sqlite3.Connection, customer_id: int, segments: dict | None = None, limit: int = 6
) -> list[sqlite3.Row]:
    active_segments = segments or segment_definitions()
    rows = conn.execute(
        """
        SELECT
            oh.*,
            c.title AS campaign_title,
            c.target_segment,
            c.channel AS campaign_channel
        FROM outreach_history oh
        LEFT JOIN campaigns c ON c.id = oh.campaign_id
        ORDER BY oh.created_at DESC, oh.id DESC
        LIMIT 40
        """
    ).fetchall()
    matches = []
    for row in rows:
        segment_key = row["segment_key"] or row["target_segment"]
        if not segment_key:
            continue
        if customer_matches_segment_with_conn(conn, customer_id, segment_key, active_segments):
            matches.append(row)
        if len(matches) >= limit:
            break
    return matches


def customer_related_imports_with_conn(
    conn: sqlite3.Connection, customer: sqlite3.Row, limit: int = 3
) -> list[sqlite3.Row]:
    if not customer["source_system"]:
        return []
    return conn.execute(
        """
        SELECT *
        FROM import_runs
        WHERE source_system = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (customer["source_system"], limit),
    ).fetchall()


def customer_next_actions(customer: sqlite3.Row, open_tasks_count: int) -> list[str]:
    actions = []
    if not customer["email"] and not customer["phone"]:
        actions.append("Capture an email or phone number on the next visit so this customer becomes reachable.")
    last_purchase = parse_datetime(customer["last_purchase_at"])
    if last_purchase and (datetime.now(UTC) - last_purchase).days > 45:
        actions.append("Queue a win-back offer, because this customer has gone quiet after a previous purchase.")
    elif last_purchase:
        actions.append("Follow up while the last purchase is still fresh and offer a next-visit deal.")
    elif customer["marketing_consent"]:
        actions.append("Send a welcome or education offer to move this contact from signup to first purchase.")
    else:
        actions.append("Use an in-person ask or QR flow to confirm consent before the next outreach push.")

    if "wholesale" in (customer["tags"] or "").lower():
        actions.append("Treat this as a wholesale relationship and schedule a restock or availability check-in.")
    elif (customer["total_spent"] or 0) >= 250:
        actions.append("Use premium or seasonal inventory alerts to retain this high-value customer.")
    elif open_tasks_count == 0:
        actions.append("Create one follow-up task so this record has a clear next owner and next step.")
    return actions[:3]


def customer_timeline_with_conn(
    conn: sqlite3.Connection,
    customer: sqlite3.Row,
    events: list[sqlite3.Row],
    touchpoints: list[sqlite3.Row],
    *,
    limit: int = 18,
) -> tuple[list[dict], list[sqlite3.Row], list[sqlite3.Row]]:
    customer_id = customer["id"]
    tasks = customer_tasks_with_conn(conn, customer_id, limit=12)
    notes = list_customer_notes_with_conn(conn, customer_id, limit=8)
    campaigns = customer_campaign_activity_with_conn(conn, customer_id, limit=6)
    related_imports = customer_related_imports_with_conn(conn, customer, limit=3)

    timeline: list[dict] = []

    if customer["notes"]:
        timeline.append(
            {
                "kind": "profile",
                "label": "Profile",
                "title": "Profile context saved",
                "summary": customer["notes"],
                "meta": "Saved on the core customer record",
                "occurred_at": max_timestamp(customer["updated_at"], customer["created_at"]) or customer["created_at"],
            }
        )

    for row in notes:
        timeline.append(
            {
                "kind": "note",
                "label": "Note",
                "title": "Internal note added",
                "summary": row["body"],
                "meta": "Saved from the customer profile",
                "occurred_at": row["created_at"],
            }
        )

    for row in events:
        total = f"${(row['order_total'] or 0):.2f}"
        quantity = row["quantity"] or 0
        item_name = row["item_name"] or "Purchase recorded"
        timeline.append(
            {
                "kind": "purchase",
                "label": "Purchase",
                "title": item_name,
                "summary": f"{quantity} item(s) | {total}",
                "meta": source_system_label(row["source_system"]),
                "occurred_at": row["purchased_at"] or row["created_at"],
            }
        )

    for row in touchpoints:
        preferences = []
        if row["preferred_channel"]:
            preferences.append(channel_label(row["preferred_channel"]))
        preferences.append(f"Email {yes_no(row['consent_email'])}")
        preferences.append(f"SMS {yes_no(row['consent_sms'])}")
        timeline.append(
            {
                "kind": "touchpoint",
                "label": "Capture",
                "title": touchpoint_label(row["touchpoint_type"]),
                "summary": row["summary"] or "Customer interaction captured.",
                "meta": " | ".join(preferences),
                "occurred_at": row["created_at"],
            }
        )

    for row in tasks:
        task_meta = [task_type_label(row["task_type"])]
        if row["status"] == "completed":
            task_meta.append("Completed")
        elif row["due_at"]:
            task_meta.append(f"Due {display_timestamp(row['due_at'], include_time=False)}")
        else:
            task_meta.append("No due date")
        timeline.append(
            {
                "kind": "task",
                "label": "Task",
                "title": row["title"],
                "summary": row["details"] or "Customer follow-up task",
                "meta": " | ".join(task_meta),
                "occurred_at": row["completed_at"] or row["due_at"] or row["created_at"],
            }
        )

    for row in campaigns:
        segment_key = row["segment_key"] or row["target_segment"]
        segment = segment_definitions().get(segment_key, {})
        timeline.append(
            {
                "kind": "campaign",
                "label": "Campaign",
                "title": f"{row['campaign_title'] or row['title']} · {outreach_event_label(row['event_type'])}",
                "summary": row["details"] or "Campaign activity matched this customer's current audience.",
                "meta": segment.get("label", segment_key.replace("_", " ").title()) if segment_key else "Campaign audience",
                "occurred_at": row["created_at"],
            }
        )

    for row in related_imports:
        if row["status"] != "completed":
            continue
        import_summary = f"{row['customers_created']} created, {row['customers_updated']} updated"
        if row["skipped_rows"]:
            import_summary += f", {row['skipped_rows']} skipped"
        timeline.append(
            {
                "kind": "import",
                "label": "Import",
                "title": f"{source_system_label(row['source_system'])} sync activity",
                "summary": import_summary,
                "meta": display_upload_name(row["filename"]) or "Imported file",
                "occurred_at": row["created_at"],
            }
        )

    timeline.sort(
        key=lambda item: parse_datetime(item["occurred_at"]) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return timeline[:limit], tasks, notes


def save_customer(fields: dict, customer_id: int | None = None) -> dict:
    email = fields.get("email", "").strip()
    phone = fields.get("phone", "").strip()
    if not email and not phone:
        return {"error": "Add at least an email or phone number for the customer record."}

    conn = db_connection()
    try:
        existing = get_customer_record(customer_id, conn=conn) if customer_id else None
        if customer_id and not existing:
            return {"error": "Customer not found."}

        notes = fields.get("notes", "").strip()
        tags = fields.get("tags", "").strip()
        total_spent = to_float(fields.get("total_spent", "0"))
        last_purchase = parsed_timestamp(fields.get("last_purchase_at", "").strip()) if fields.get("last_purchase_at") else None
        marketing_consent = 1 if fields.get("marketing_consent") else 0

        if existing:
            conn.execute(
                """
                UPDATE customers
                SET first_name = ?, last_name = ?, email = ?, phone = ?, city = ?, state = ?, tags = ?,
                    notes = ?, total_spent = ?, last_purchase_at = ?, preferred_channel = ?, marketing_consent = ?,
                    acquisition_source = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    fields.get("first_name", "").strip() or None,
                    fields.get("last_name", "").strip() or None,
                    email.lower() or None,
                    phone or None,
                    fields.get("city", "").strip() or None,
                    fields.get("state", "").strip() or None,
                    tags or None,
                    notes or None,
                    total_spent,
                    last_purchase,
                    fields.get("preferred_channel", "").strip() or None,
                    marketing_consent,
                    fields.get("acquisition_source", "").strip() or existing["acquisition_source"],
                    utc_now(),
                    customer_id,
                ),
            )
            conn.commit()
            return {"error": None, "customer_id": customer_id, "action": "updated"}

        new_customer_id, _ = upsert_customer_record(
            conn,
            source_system="manual_entry",
            external_id=fields.get("external_id", "").strip(),
            first_name=fields.get("first_name", "").strip(),
            last_name=fields.get("last_name", "").strip(),
            email=email,
            phone=phone,
            city=fields.get("city", "").strip(),
            state=fields.get("state", "").strip(),
            tags=tags,
            notes=notes,
            total_spent=total_spent,
            last_purchase_at=last_purchase,
            preferred_channel=fields.get("preferred_channel", "").strip(),
            marketing_consent=marketing_consent,
            acquisition_source=fields.get("acquisition_source", "").strip() or "manual_entry",
        )
        conn.commit()
        return {"error": None, "customer_id": new_customer_id, "action": "created"}
    finally:
        conn.close()


def touchpoint_label(value: str) -> str:
    labels = dict(TOUCHPOINT_TYPES)
    return labels.get(value, value.replace("_", " ").title())


def source_system_label(value: str | None) -> str:
    labels = {
        "clover": "Clover export",
        "constant_contact": "Constant Contact",
        "shopify_legacy": "Legacy Shopify",
        "website_signup": "Website signup export",
        "legacy_csv": "Other spreadsheet",
        "manual_entry": "Manual entry",
        "touchpoint_capture": "Lead capture",
        "demo_seed": "Demo seed",
    }
    return labels.get(value or "", (value or "").replace("_", " ").title())


def acquisition_label(value: str | None) -> str:
    if value in dict(TOUCHPOINT_TYPES):
        return touchpoint_label(value or "")
    return source_system_label(value)


def display_upload_name(filename: str | None) -> str:
    name = Path(filename or "").name
    if len(name) > 15 and name[:14].isdigit() and name[14] == "_":
        return name[15:]
    return name


def channel_label(value: str | None) -> str:
    labels = dict(PREFERRED_CHANNELS + CAMPAIGN_CHANNELS)
    return labels.get(value or "", (value or "").replace("_", " ").title())


def outreach_event_label(value: str | None) -> str:
    labels = dict(OUTREACH_EVENT_TYPES)
    return labels.get(value or "", (value or "").replace("_", " ").title())


def status_pill(value: str | None, *, prefix: str = "status") -> str:
    raw = (value or "").strip().lower().replace(" ", "_")
    if not raw:
        return "—"
    label_source = outreach_event_label(value) if prefix == "event" else (value or "").replace("_", " ").title()
    label = escape(label_source)
    return f"<span class='status-pill {prefix}-{escape(raw)}'>{label}</span>"


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


def base_layout(title: str, body: str, flash: str = "", active_section: str = "") -> bytes:
    flash_html = f"<div class='flash'>{escape(flash)}</div>" if flash else ""
    styles_href = asset_url("static/styles.css")
    script_src = asset_url("static/app.js")
    nav_groups = [
        (
            "Core",
            [
                ("dashboard", "Dashboard", "Overview and next actions", "/"),
                ("customers", "Customers", "Profiles, search, and history", "/customers"),
                ("tasks", "Tasks", "Follow-ups and weekly work", "/tasks"),
                ("marketing", "Marketing", "Campaigns, segments, and outreach", "/marketing"),
            ],
        ),
        (
            "Data",
            [
                ("capture", "Capture", "Add leads from staff or public flows", "/capture"),
                ("imports", "Imports", "Bring in Clover and legacy records", "/imports"),
            ],
        ),
        (
            "Quick Actions",
            [
                ("customers", "New Customer", "Create a fresh contact record", "/customers/new"),
                ("logout", "Logout", "Exit the staff workspace", "/logout"),
            ],
        ),
    ]
    nav_html = "".join(
        "<section class='nav-group'>"
        f"<div class='nav-group-label'>{escape(group_label)}</div>"
        + "".join(
            f"<a href='{escape(href)}' class='nav-card{' active' if key == active_section else ''}'>"
            "<span class='nav-card-title-row'>"
            f"<span class='nav-card-title'>{escape(label)}</span>"
            "</span>"
            f"<span class='nav-card-copy'>{escape(description)}</span>"
            "</a>"
            for key, label, description, href in group_items
        )
        + "</section>"
        for group_label, group_items in nav_groups
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="{escape(styles_href)}">
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <div class="brand-mark">SV</div>
        <div>
          <h1>Seaview CRM</h1>
          <p>Customer, capture, and outreach workspace.</p>
        </div>
      </div>
      <div class="sidebar-intro">
        <strong>Choose a workspace</strong>
        <span>Customers, capture, marketing, and follow-up in one place.</span>
      </div>
      <nav>
        {nav_html}
      </nav>
      <div class="sidebar-footer">
        <span class="pill muted-pill">Staff workspace</span>
        <span class="muted">Presentation build</span>
      </div>
    </aside>
    <main class="content">
      {flash_html}
      {body}
    </main>
  </div>
  <script src="{escape(script_src)}" defer></script>
</body>
</html>"""
    return html.encode("utf-8")


def render_login(message: str = "") -> bytes:
    body = f"""
    <section class="hero public-hero auth-hero">
      <div>
        <h2>Seaview Staff Login</h2>
        <p>Staff access only.</p>
      </div>
    </section>

    <div class="panel signup-panel auth-panel">
      <h3>Sign in</h3>
      <form method="post" action="/login" class="stack">
        <label>Username
          <input type="text" name="username" autocomplete="username">
        </label>
        <label>Password
          <input type="password" name="password" autocomplete="current-password">
        </label>
        <button type="submit">Sign in</button>
      </form>
      <p class="muted">Use the configured staff credentials for this workspace.</p>
    </div>
    """
    return public_layout("Seaview Staff Login", body, flash=message)


def public_layout(title: str, body: str, flash: str = "") -> bytes:
    flash_html = f"<div class='flash'>{escape(flash)}</div>" if flash else ""
    styles_href = asset_url("static/styles.css")
    script_src = asset_url("static/app.js")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="{escape(styles_href)}">
</head>
<body class="public-view">
  <main class="public-shell">
    {flash_html}
    {body}
  </main>
  <script src="{escape(script_src)}" defer></script>
</body>
</html>"""
    return html.encode("utf-8")


def render_public_capture(path: str, message: str = "") -> bytes:
    page = public_capture_page(path)
    if not page:
        return public_layout("Not Found", "<div class='panel'><h2>Page not found</h2></div>", flash=message)

    benefits_html = "".join(
        f"<li><strong>{escape(item)}</strong></li>" for item in page["benefits"]
    )
    body = f"""
    <section class="hero public-hero capture-hero">
      <div>
        <h2>{escape(page['headline'])}</h2>
        <p>{escape(page['description'])}</p>
      </div>
      <div class="capture-hero-meta">
        <span class="pill">{escape(touchpoint_label(page['touchpoint_type']))}</span>
        <span class="pill">Preferred {escape(channel_label(page['preferred_channel']))}</span>
      </div>
    </section>

    <section class="grid wide-grid">
      <div class="panel signup-panel">
        <h3>Stay in the loop</h3>
        <form method="post" action="{escape(path)}" class="stack">
          <input type="hidden" name="touchpoint_type" value="{escape(page['touchpoint_type'])}">
          <input type="hidden" name="source_label" value="{escape(page['label'])}">
          <input type="hidden" name="capture_offer" value="{escape(page['headline'])}">
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
              <input type="email" name="email" placeholder="name@example.com">
            </label>
            <label>Phone
              <input type="text" name="phone" placeholder="(555) 555-5555">
            </label>
          </div>
          <label>Preferred updates
            <select name="preferred_channel">{option_list(PREFERRED_CHANNELS, page['preferred_channel'])}</select>
          </label>
          <label>What are you interested in?
            <input type="text" name="interest_tags" placeholder="{escape(page['interest_placeholder'])}">
          </label>
          <label>Optional notes
            <textarea name="notes" rows="3" placeholder="Anything Seaview should know before following up?"></textarea>
          </label>
          <div class="checkbox-row">
            <label><input type="checkbox" name="consent_email" value="1"> Email me specials and updates</label>
            <label><input type="checkbox" name="consent_sms" value="1"> Text me timely offers</label>
          </div>
          <button type="submit">{escape(page['cta'])}</button>
        </form>
        <p class="muted">Only one contact method is required. Seaview can enrich the profile later through purchases and staff interactions.</p>
      </div>
      <div class="panel">
        <h3>Included</h3>
        <ul class="stacked-list">{benefits_html}</ul>
        <p class="muted">{escape(page['capture_note'])}</p>
      </div>
    </section>
    """
    return public_layout("Join Seaview Updates", body, flash=message)


def render_dashboard(message: str = "") -> bytes:
    conn = db_connection()
    try:
        segment_counts = segment_counts_with_conn(conn)
        lead_capture = lead_capture_snapshot_with_conn(conn)
        metrics = dashboard_metrics_with_conn(conn, segment_counts=segment_counts, lead_capture=lead_capture)
    finally:
        conn.close()
    imports_html = "".join(
        f"<tr><td>{escape(display_timestamp(row['created_at']))}</td><td>{escape(source_system_label(row['source_system']))}</td><td>{escape(display_upload_name(row['filename']))}</td><td>{row['rows_received']}</td><td>{escape(row['status'])}</td></tr>"
        for row in metrics["recent_imports"]
    ) or "<tr><td colspan='5'>No imports yet.</td></tr>"
    touchpoints_html = "".join(
        f"<li><strong>{escape(display_name(row))}</strong><span>{escape(touchpoint_label(row['touchpoint_type']))}</span><span>{escape(display_timestamp(row['created_at']))}</span></li>"
        for row in metrics["recent_touchpoints"]
    ) or "<li>No touchpoints yet.</li>"
    next_tasks_html = "".join(
        f"""
        <li>
          <strong>{escape(row['title'])}</strong>
          <span>{escape(task_type_label(row['task_type']))}{' | ' + escape(display_timestamp(row['due_at'], include_time=False)) if row['due_at'] else ''}</span>
        </li>
        """
        for row in metrics["next_tasks"]
    ) or "<li><strong>No open tasks</strong><span>Create the next follow-up from the tasks page or a customer profile.</span></li>"
    insight_cards_html = "".join(
        f"""
        <article class="card action-card">
          <h4>{escape(card['title'])}</h4>
          <p>{escape(card['body'])}</p>
          <a class="button" href="{escape(card['href'])}">{escape(card['label'])}</a>
        </article>
        """
        for card in dashboard_insights(metrics)
    )
    capture_summary = "".join(
        [
            f"<li><strong>Website</strong><span>{metrics['lead_capture']['website_total']} captured</span></li>",
            f"<li><strong>QR</strong><span>{metrics['lead_capture']['qr_total']} captured</span></li>",
            f"<li><strong>Wholesale</strong><span>{metrics['lead_capture']['wholesale_total']} captured</span></li>",
        ]
    )
    body = f"""
    <section class="hero">
      <div>
        <h2>Seaview CRM</h2>
        <p>Customers, capture, campaigns, and imports in one place.</p>
      </div>
      <div class="button-row">
        <a class="button" href="/customers">Find customer</a>
        <a class="button secondary" href="/capture">Capture lead</a>
        <a class="button secondary" href="/marketing">New campaign</a>
        <a class="button secondary" href="/imports">Import data</a>
      </div>
    </section>

    <section class="cards action-grid">
      {insight_cards_html}
    </section>

    <section class="stats">
      <article><span>Total customers</span><strong>{metrics['customers']}</strong></article>
      <article><span>Reachable contacts</span><strong>{metrics['contactable']}</strong></article>
      <article><span>Open tasks</span><strong>{metrics['open_tasks']}</strong></article>
      <article><span>Open campaigns</span><strong>{metrics['scheduled_campaigns']}</strong></article>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Next actions</h3>
        <ul class="stacked-list">{next_tasks_html}</ul>
      </div>
      <div class="panel">
        <h3>Recent activity</h3>
        <ul class="stacked-list">{touchpoints_html}</ul>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Recent imports</h3>
        <table>
          <thead><tr><th>Date</th><th>Source</th><th>File</th><th>Rows</th><th>Status</th></tr></thead>
          <tbody>{imports_html}</tbody>
        </table>
      </div>
      <div class="panel">
        <h3>Capture links</h3>
        <div class="button-row">
          <a class="button secondary" href="/join" target="_blank">Website</a>
          <a class="button secondary" href="/join/qr" target="_blank">QR</a>
          <a class="button secondary" href="/join/wholesale" target="_blank">Wholesale</a>
        </div>
        <ul class="stacked-list">{capture_summary}</ul>
      </div>
    </section>
    """
    return base_layout("Seaview CRM Dashboard", body, flash=message, active_section="dashboard")


def render_tasks(message: str = "") -> bytes:
    conn = db_connection()
    try:
        counts = task_counts_with_conn(conn)
        open_rows = list_tasks_with_conn(conn, status="open", limit=20)
        completed_rows = list_tasks_with_conn(conn, status="completed", limit=8)
    finally:
        conn.close()

    open_tasks_html = "".join(
        f"""
        <tr>
          <td><strong>{escape(row['title'])}</strong><div class="muted">{escape(row['details'] or '')}</div></td>
          <td>{escape(task_type_label(row['task_type']))}</td>
          <td>{"<a href='/customers/%s'>%s</a>" % (row['customer_id'], escape(display_name(row))) if row['customer_id'] else 'General'}</td>
          <td>{escape(display_timestamp(row['due_at'], include_time=False) if row['due_at'] else 'No due date')}</td>
          <td>
            <form method="post" action="/tasks/complete">
              <input type="hidden" name="task_id" value="{row['id']}">
              <button type="submit" class="button secondary small">Complete</button>
            </form>
          </td>
        </tr>
        """
        for row in open_rows
    ) or "<tr><td colspan='5'>No open tasks.</td></tr>"

    completed_tasks_html = "".join(
        f"<li><strong>{escape(row['title'])}</strong><span>{escape(display_timestamp(row['completed_at']))}</span></li>"
        for row in completed_rows
    ) or "<li>No completed tasks yet.</li>"

    body = f"""
    <section class="page-head">
      <div>
        <h2>Tasks</h2>
        <p>Track follow-ups and weekly work.</p>
      </div>
    </section>

    <section class="stats">
      <article><span>Open tasks</span><strong>{counts['open']}</strong></article>
      <article><span>Completed</span><strong>{counts['completed']}</strong></article>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>New task</h3>
        <form method="post" action="/tasks" class="stack">
          <label>Title
            <input type="text" name="title" placeholder="Follow up on weekend special">
          </label>
          <label>Details
            <textarea name="details" rows="3" placeholder="Add the next step or context."></textarea>
          </label>
          <div class="field-grid">
            <label>Type
              <select name="task_type">{option_list(TASK_TYPES, 'follow_up')}</select>
            </label>
            <label>Due date
              <input type="date" name="due_at">
            </label>
          </div>
          <button type="submit">Create task</button>
        </form>
      </div>
      <div class="panel">
        <h3>Recently completed</h3>
        <ul class="stacked-list">{completed_tasks_html}</ul>
      </div>
    </section>

    <div class="panel">
      <h3>Open tasks</h3>
      <table>
        <thead><tr><th>Task</th><th>Type</th><th>Customer</th><th>Due</th><th></th></tr></thead>
        <tbody>{open_tasks_html}</tbody>
      </table>
    </div>
    """
    return base_layout("Tasks", body, flash=message, active_section="tasks")


def render_customers(search: str = "") -> bytes:
    rows = list_customers(search)
    results_label = f"{len(rows)} record{'s' if len(rows) != 1 else ''}"
    customer_rows = "".join(
        f"<tr><td><a href='/customers/{row['id']}'>{escape(display_name(row))}</a></td><td>{escape(row['email'] or '')}</td><td>{escape(row['phone'] or '')}</td><td>{escape(channel_label(row['preferred_channel']) or 'Unknown')}</td><td>{escape(acquisition_label(row['acquisition_source'] or row['source_system']))}</td><td>{escape(row['tags'] or '')}</td><td>${(row['total_spent'] or 0):.2f}</td></tr>"
        for row in rows
    ) or "<tr><td colspan='7'>No customers found.</td></tr>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Customers</h2>
        <p>Search and manage customer profiles.</p>
        <div class="pill-row"><span class="pill muted-pill">{results_label}</span></div>
      </div>
      <div class="button-row">
        <form method="get" class="search inline-search">
          <input type="text" name="q" value="{escape(search)}" placeholder="Search by name, email, source, or tag">
          <button type="submit">Search</button>
        </form>
        <a class="button" href="/customers/new">Add customer</a>
      </div>
    </section>
    <div class="panel">
      <table>
        <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Preferred</th><th>Source</th><th>Tags</th><th>Total spend</th></tr></thead>
        <tbody>{customer_rows}</tbody>
      </table>
    </div>
    """
    return base_layout("Customers", body, active_section="customers")


def render_customer_form(message: str = "", customer: sqlite3.Row | None = None) -> bytes:
    is_edit = customer is not None
    title = "Edit Customer" if is_edit else "Add Customer"
    action = f"/customers/{customer['id']}/edit" if is_edit else "/customers/new"
    values = {
        "first_name": customer["first_name"] if customer else "",
        "last_name": customer["last_name"] if customer else "",
        "email": customer["email"] if customer else "",
        "phone": customer["phone"] if customer else "",
        "city": customer["city"] if customer else "",
        "state": customer["state"] if customer else "",
        "tags": customer["tags"] if customer else "",
        "notes": customer["notes"] if customer else "",
        "preferred_channel": customer["preferred_channel"] if customer else "email",
        "marketing_consent": "checked" if customer and customer["marketing_consent"] else "",
        "acquisition_source": customer["acquisition_source"] if customer else "manual_entry",
        "total_spent": f"{(customer['total_spent'] or 0):.2f}" if customer else "",
        "last_purchase_at": (customer["last_purchase_at"] or "")[:10] if customer and customer["last_purchase_at"] else "",
    }
    body = f"""
    <section class="page-head">
      <div>
        <h2>{title}</h2>
        <p>Create or update a customer record.</p>
      </div>
      <a class="button secondary" href="/customers">Back to customers</a>
    </section>
    <div class="panel">
      <form method="post" action="{action}" class="stack">
        <div class="field-grid">
          <label>First name
            <input type="text" name="first_name" value="{escape(values['first_name'] or '')}">
          </label>
          <label>Last name
            <input type="text" name="last_name" value="{escape(values['last_name'] or '')}">
          </label>
        </div>
        <div class="field-grid">
          <label>Email
            <input type="email" name="email" value="{escape(values['email'] or '')}">
          </label>
          <label>Phone
            <input type="text" name="phone" value="{escape(values['phone'] or '')}">
          </label>
        </div>
        <div class="field-grid">
          <label>City
            <input type="text" name="city" value="{escape(values['city'] or '')}">
          </label>
          <label>State
            <input type="text" name="state" value="{escape(values['state'] or '')}">
          </label>
        </div>
        <div class="field-grid">
          <label>Preferred channel
            <select name="preferred_channel">{option_list(PREFERRED_CHANNELS, values['preferred_channel'] or 'email')}</select>
          </label>
          <label>Acquisition source
            <input type="text" name="acquisition_source" value="{escape(values['acquisition_source'] or '')}">
          </label>
        </div>
        <div class="field-grid">
          <label>Total spend
            <input type="text" name="total_spent" value="{escape(values['total_spent'] or '')}">
          </label>
          <label>Last purchase date
            <input type="date" name="last_purchase_at" value="{escape(values['last_purchase_at'] or '')}">
          </label>
        </div>
        <label>Tags
          <input type="text" name="tags" value="{escape(values['tags'] or '')}" placeholder="vip, wholesale, newsletter">
        </label>
        <label>Notes
          <textarea name="notes" rows="4" placeholder="Helpful context for staff">{escape(values['notes'] or '')}</textarea>
        </label>
        <label class="checkbox-inline"><input type="checkbox" name="marketing_consent" value="1" {values['marketing_consent']}> Customer approved marketing outreach</label>
        <button type="submit">{'Save changes' if is_edit else 'Create customer'}</button>
      </form>
    </div>
    """
    return base_layout(title, body, flash=message, active_section="customers")


def render_customer_detail(customer_id: int, message: str = "") -> bytes:
    conn = db_connection()
    try:
        customer, events, touchpoints = get_customer_with_conn(conn, customer_id)
        if not customer:
            return base_layout("Customer Not Found", "<div class='panel'><h2>Customer not found</h2></div>", flash=message, active_section="customers")
        timeline, tasks, notes = customer_timeline_with_conn(conn, customer, events, touchpoints)
    finally:
        conn.close()

    open_tasks = [row for row in tasks if row["status"] == "open"]
    task_rows = "".join(
        f"""
        <li>
          <strong>{escape(row['title'])}</strong>
          <span>{escape(display_timestamp(row['due_at'], include_time=False) if row['due_at'] else 'No due date')}</span>
          <form method="post" action="/tasks/complete">
            <input type="hidden" name="task_id" value="{row['id']}">
            <input type="hidden" name="customer_id" value="{customer_id}">
            <button type="submit" class="button secondary small">Complete</button>
          </form>
        </li>
        """
        for row in open_tasks[:8]
    ) or "<li>No open follow-ups for this customer.</li>"

    timeline_rows = "".join(
        f"""
        <li class="timeline-item timeline-{escape(item['kind'])}">
          <div class="timeline-topline">
            <span class="pill">{escape(item['label'])}</span>
            <time class="muted">{escape(display_timestamp(item['occurred_at']))}</time>
          </div>
          <strong>{escape(item['title'])}</strong>
          <span>{escape(item['summary'])}</span>
          <small class="muted">{escape(item['meta'])}</small>
        </li>
        """
        for item in timeline
    ) or "<li class='timeline-empty'>No activity has been recorded for this customer yet.</li>"

    next_actions_html = "".join(
        f"<li>{escape(action)}</li>" for action in customer_next_actions(customer, len(open_tasks))
    )
    note_rows = "".join(
        f"<li><strong>{escape(display_timestamp(row['created_at']))}</strong><span>{escape(row['body'])}</span></li>"
        for row in notes[:4]
    ) or "<li>No internal notes yet.</li>"

    context_pills = "".join(f"<span class='pill'>{escape(label)}</span>" for label in customer_context_labels(customer))
    body = f"""
    <section class="page-head">
      <div>
        <h2>{escape(display_name(customer))}</h2>
        <p>{escape(customer['email'] or 'No email on file')} | {escape(customer['phone'] or 'No phone on file')}</p>
        <div class="pill-row">{context_pills}</div>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/customers">Back to customers</a>
        <a class="button" href="/customers/{customer['id']}/edit">Edit customer</a>
      </div>
    </section>
    <section class="stats">
      <article><span>Total spend</span><strong>${(customer['total_spent'] or 0):.2f}</strong></article>
      <article><span>Last purchase</span><strong>{escape(display_timestamp(customer['last_purchase_at'], include_time=False) if customer['last_purchase_at'] else 'No purchases yet')}</strong></article>
      <article><span>Open tasks</span><strong>{len(open_tasks)}</strong></article>
      <article><span>Timeline activity</span><strong>{len(timeline)}</strong></article>
    </section>
    <section class="grid customer-grid">
      <div class="panel timeline-panel">
        <div class="panel-head">
          <div>
            <h3>Customer timeline</h3>
            <p class="muted">Purchases, captures, campaigns, imports, tasks, and notes in one stream.</p>
          </div>
        </div>
        <ul class="timeline-list">{timeline_rows}</ul>
      </div>
      <div class="panel">
        <h3>Profile snapshot</h3>
        <dl class="details">
          <dt>Primary source</dt><dd>{escape(source_system_label(customer['source_system']))}</dd>
          <dt>Acquisition source</dt><dd>{escape(acquisition_label(customer['acquisition_source']))}</dd>
          <dt>Preferred channel</dt><dd>{escape(channel_label(customer['preferred_channel']) or 'Unknown')}</dd>
          <dt>Marketing consent</dt><dd>{yes_no(customer['marketing_consent'])}</dd>
          <dt>Location</dt><dd>{escape(' '.join(filter(None, [customer['city'], customer['state']])) or 'Unknown')}</dd>
          <dt>Tags</dt><dd>{escape(customer['tags'] or 'None yet')}</dd>
          <dt>Last contacted</dt><dd>{escape(display_timestamp(customer['last_contacted_at']) if customer['last_contacted_at'] else 'Not tracked yet')}</dd>
        </dl>
      </div>
      <div class="panel">
        <h3>Recommended next step</h3>
        <ul class="stacked-list compact-list">{next_actions_html}</ul>
      </div>
      <div class="panel">
        <h3>Open tasks</h3>
        <ul class="stacked-list">{task_rows}</ul>
      </div>
      <div class="panel">
        <h3>New follow-up</h3>
        <form method="post" action="/tasks" class="stack">
          <input type="hidden" name="customer_id" value="{customer['id']}">
          <input type="hidden" name="task_type" value="follow_up">
          <label>Title
            <input type="text" name="title" placeholder="Follow up after last visit">
          </label>
          <label>Details
            <textarea name="details" rows="3" placeholder="Add the next step."></textarea>
          </label>
          <label>Due date
            <input type="date" name="due_at">
          </label>
          <button type="submit">Create follow-up</button>
        </form>
      </div>
      <div class="panel">
        <h3>Internal notes</h3>
        <ul class="stacked-list compact-list">{note_rows}</ul>
        <form method="post" action="/customers/{customer['id']}/notes" class="stack">
          <label>Add note
            <textarea name="body" rows="3" placeholder="Add context for the next person who opens this record."></textarea>
          </label>
          <button type="submit">Save note</button>
        </form>
      </div>
    </section>
    """
    return base_layout(display_name(customer), body, flash=message, active_section="customers")


def render_marketing(message: str = "") -> bytes:
    conn = db_connection()
    try:
        segments = segment_definitions()
        segment_counts = segment_counts_with_conn(conn, segments)
        lead_capture = lead_capture_snapshot_with_conn(conn)
        snapshot = marketing_snapshot_with_conn(
            conn,
            segments=segments,
            segment_counts=segment_counts,
            lead_capture=lead_capture,
        )
    finally:
        conn.close()
    campaign_status_counts = {row["status"]: row["count"] for row in snapshot["campaign_totals"]}
    segment_rows = "".join(
        f"<tr><td><strong>{escape(segment['label'])}</strong><div class='muted'>{escape(segment['description'])}</div></td><td>{segment['count']}</td><td>{escape(segment['recommended_channel'])}</td><td><a class='button secondary small' href='/marketing/export?segment={escape(segment['key'])}'>Export CSV</a></td></tr>"
        for segment in snapshot["segments"]
    )
    campaign_rows_list = []
    for row in snapshot["recent_campaigns"]:
        export_query = urlencode({"segment": row["target_segment"], "campaign_id": str(row["id"])})
        actions = [
            f"<a class='button secondary small' href='/marketing/export?{escape(export_query)}'>Export</a>"
        ]
        if row["status"] != "sent":
            actions.append(
                "<form method='post' action='/marketing/campaigns/send' class='inline-form'>"
                f"<input type='hidden' name='campaign_id' value='{row['id']}'>"
                "<button type='submit' class='secondary small'>Mark sent</button>"
                "</form>"
            )
        campaign_rows_list.append(
            "<tr>"
            f"<td><strong>{escape(row['title'])}</strong><div class='muted'>{escape(row['goal'] or 'No goal added.')}</div></td>"
            f"<td>{escape(segments.get(row['target_segment'], {}).get('label', row['target_segment']))}</td>"
            f"<td>{escape(channel_label(row['channel']))}</td>"
            f"<td>{row['audience_count']}</td>"
            f"<td>{status_pill(row['status'])}</td>"
            f"<td>{escape(display_timestamp(row['scheduled_for'], include_time=False) if row['scheduled_for'] else '—')}</td>"
            f"<td><div class='table-actions'>{''.join(actions)}</div></td>"
            "</tr>"
        )
    campaign_rows = "".join(campaign_rows_list) or "<tr><td colspan='7'>No campaigns yet.</td></tr>"
    outreach_rows = "".join(
        "<tr>"
        f"<td>{status_pill(row['event_type'], prefix='event')}</td>"
        f"<td><strong>{escape(row['title'])}</strong><div class='muted'>{escape(row['details'] or '')}</div></td>"
        f"<td>{escape(segments.get(row['segment_key'], {}).get('label', row['segment_key'] or '—'))}</td>"
        f"<td>{escape(channel_label(row['channel'])) if row['channel'] else '—'}</td>"
        f"<td>{row['audience_count']}</td>"
        f"<td>{escape(display_timestamp(row['created_at']))}</td>"
        "</tr>"
        for row in snapshot["recent_outreach"]
    ) or "<tr><td colspan='6'>No outreach history yet.</td></tr>"
    focus_rows = "".join(
        f"<li><strong>{escape(item['title'])}</strong><span>{escape(item['body'])}</span></li>"
        for item in marketing_focus(snapshot)
    )
    capture_rows = "".join(
        f"<li><strong>{escape(source['label'])}</strong><span>{source['count']} captured</span></li>"
        for source in snapshot["lead_capture"]["sources"][:4]
    ) or (
        "<li><strong>Website</strong><span>Start capturing on the public join pages to build first-party contacts.</span></li>"
    )
    body = f"""
    <section class="page-head">
      <div>
        <h2>Marketing</h2>
        <p>Campaigns and audience segments.</p>
      </div>
      <div class="button-row">
        <a class="button" href="/capture">Capture lead</a>
        <a class="button secondary" href="/tasks">Tasks</a>
      </div>
    </section>

    <section class="stats">
      <article><span>Email-ready</span><strong>{snapshot['email_ready']}</strong></article>
      <article><span>SMS-ready</span><strong>{snapshot['sms_ready']}</strong></article>
      <article><span>Missing contact info</span><strong>{snapshot['missing_contact']}</strong></article>
      <article><span>Sent campaigns</span><strong>{campaign_status_counts.get('sent', 0)}</strong></article>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>New campaign</h3>
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
              <select name="target_segment">{option_list([(key, segment['label']) for key, segment in segments.items()], 'recent_buyers')}</select>
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
          <label>Status
            <select name="status">{option_list(CAMPAIGN_STATUSES, 'draft')}</select>
          </label>
          <button type="submit">Save campaign</button>
        </form>
      </div>
      <div class="panel">
        <h3>Audience segments</h3>
        <table>
          <thead><tr><th>Segment</th><th>Count</th><th>Best channel</th><th>Action</th></tr></thead>
          <tbody>{segment_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Recent campaigns</h3>
        <table>
          <thead><tr><th>Campaign</th><th>Segment</th><th>Channel</th><th>Audience</th><th>Status</th><th>Scheduled</th><th>Actions</th></tr></thead>
          <tbody>{campaign_rows}</tbody>
        </table>
      </div>
      <div class="panel">
        <h3>Recent outreach</h3>
        <table>
          <thead><tr><th>Activity</th><th>Campaign</th><th>Segment</th><th>Channel</th><th>Audience</th><th>When</th></tr></thead>
          <tbody>{outreach_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Focus</h3>
        <ul class="stacked-list">{focus_rows}</ul>
        <div class="button-row">
          <a class="button secondary" href="/join" target="_blank">Website</a>
          <a class="button secondary" href="/join/qr" target="_blank">QR</a>
          <a class="button secondary" href="/join/wholesale" target="_blank">Wholesale</a>
        </div>
      </div>
      <div class="panel">
        <h3>Capture sources</h3>
        <ul class="stacked-list">{capture_rows}</ul>
        <div class="pill-row">
          <span class="pill">Website {snapshot['lead_capture']['website_total']}</span>
          <span class="pill">QR {snapshot['lead_capture']['qr_total']}</span>
          <span class="pill">Wholesale {snapshot['lead_capture']['wholesale_total']}</span>
        </div>
      </div>
    </section>
    """
    return base_layout("Marketing", body, flash=message, active_section="marketing")


def render_imports(message: str = "") -> bytes:
    conn = db_connection()
    recent = conn.execute("SELECT * FROM import_runs ORDER BY created_at DESC LIMIT 10").fetchall()
    conn.close()
    history_rows = "".join(
        f"<tr><td>{escape(display_timestamp(row['created_at']))}</td><td>{escape(source_system_label(row['source_system']))}</td><td>{escape(display_upload_name(row['filename']))}</td><td>{row['rows_received']}</td><td>{row['customers_created']}</td><td>{row['customers_updated']}</td><td>{row['skipped_rows'] or 0}</td><td>{row['purchase_events_created']}</td><td>{escape(row['status'])}</td></tr>"
        for row in recent
    ) or "<tr><td colspan='9'>No imports yet.</td></tr>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Imports</h2>
        <p>Upload customer data from current systems.</p>
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
        <thead><tr><th>Date</th><th>Source</th><th>File</th><th>Rows</th><th>Created</th><th>Updated</th><th>Skipped</th><th>Purchases</th><th>Status</th></tr></thead>
        <tbody>{history_rows}</tbody>
      </table>
    </div>
    """
    return base_layout("Imports", body, flash=message, active_section="imports")


def render_import_preview(import_id: str, message: str = "") -> bytes:
    pending = PENDING_IMPORTS.get(import_id)
    if not pending:
        return base_layout("Import Preview", "<div class='panel'><h2>Import preview expired</h2><p>Upload the file again to review it before importing.</p></div>", flash=message, active_section="imports")
    analysis = pending.get("analysis") or analyze_import_rows(pending["rows"])
    columns = pending.get("columns") or analysis["columns"]
    sample_rows = pending.get("sample_rows") or preview_rows(pending["rows"])
    column_headers = "".join(f"<th>{escape(col)}</th>" for col in columns) or "<th>No columns detected</th>"
    sample_html = "".join(
        "<tr>" + "".join(f"<td>{escape((row.get(col) or '')[:80])}</td>" for col in columns) + "</tr>"
        for row in sample_rows
    ) or "<tr><td>No preview rows available.</td></tr>"
    mapped_groups = "".join(
        f"<li><strong>{escape(group['group'])}</strong><span>"
        + (
            " | ".join(
                f"{escape(field['label'])}: " + ", ".join(f"<code>{escape(column)}</code>" for column in field["columns"])
                for field in group["fields"]
            )
            if group["fields"]
            else "No supported fields detected."
        )
        + "</span></li>"
        for group in analysis["mapped_fields"]
    )
    validation_items = analysis["blocking_warnings"] + analysis["warnings"]
    validation_rows = "".join(f"<li>{escape(item)}</li>" for item in validation_items)
    unmapped_pills = "".join(f"<span class='pill muted-pill'>{escape(column)}</span>" for column in analysis["unmapped_columns"])
    disabled_attr = "" if analysis["can_import"] else " disabled"
    confirm_label = "Confirm import" if analysis["can_import"] else "Import unavailable"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Preview import</h2>
        <p>Review the uploaded file before it changes the CRM. This makes the import workflow feel deliberate and worker-safe.</p>
      </div>
      <a class="button secondary" href="/imports">Back to imports</a>
    </section>
    <section class="stats">
      <article><span>Rows detected</span><strong>{len(pending['rows'])}</strong></article>
      <article><span>Ready to match</span><strong>{analysis['identifiable_rows']}</strong></article>
      <article><span>Reachable contacts</span><strong>{analysis['contactable_rows']}</strong></article>
      <article><span>Rows skipped</span><strong>{analysis['skipped_rows']}</strong></article>
    </section>
    <section class="grid wide-grid">
      <div class="panel">
        <h3>Validation</h3>
        <dl class="details">
          <dt>Source system</dt><dd>{escape(source_system_label(pending['source_system']))}</dd>
          <dt>Filename</dt><dd>{escape(display_upload_name(pending['filename']))}</dd>
          <dt>Purchase rows</dt><dd>{analysis['purchase_rows']}</dd>
          <dt>Consent rows</dt><dd>{analysis['consent_rows']}</dd>
        </dl>
        <ul class="stacked-list compact-list validation-list">
          {validation_rows or "<li><strong>Ready to import</strong><span>The file has enough identity data to safely merge into the CRM.</span></li>"}
        </ul>
      </div>
      <div class="panel">
        <h3>Mapped fields</h3>
        <ul class="stacked-list compact-list">
          {mapped_groups}
        </ul>
      </div>
    </section>
    <section class="grid wide-grid">
      <div class="panel">
        <h3>Unmapped columns</h3>
        <div class="pill-row">{unmapped_pills or "<span class='pill'>All detected columns are mapped.</span>"}</div>
      </div>
      <div class="panel">
        <h3>What happens on import</h3>
        <ul class="stacked-list compact-list">
          <li><strong>Identity merge</strong><span>Rows with email, phone, external ID, or a customer name are matched into customer profiles.</span></li>
          <li><strong>Purchase enrichment</strong><span>Order totals, items, and dates create purchase events when those columns are present.</span></li>
          <li><strong>Worker-safe import</strong><span>Rows without enough identity data are skipped instead of creating empty customer records.</span></li>
        </ul>
      </div>
    </section>
    <div class="panel">
      <h3>Sample rows</h3>
      <table>
        <thead><tr>{column_headers}</tr></thead>
        <tbody>{sample_html}</tbody>
      </table>
    </div>
    <div class="button-row">
      <form method="post" action="/imports/confirm">
        <input type="hidden" name="import_id" value="{escape(import_id)}">
        <button type="submit"{disabled_attr}>{confirm_label}</button>
      </form>
      <form method="post" action="/imports/cancel">
        <input type="hidden" name="import_id" value="{escape(import_id)}">
        <button type="submit" class="button secondary">Cancel</button>
      </form>
    </div>
    """
    return base_layout("Import Preview", body, flash=message, active_section="imports")


def render_signup(message: str = "") -> bytes:
    public_page_rows = "".join(
        f"""
        <li>
          <strong>{escape(page['label'])}</strong>
          <span>{escape(page['capture_note'])}</span>
          <div class="copy-row">
            <a class="button secondary small" href="{escape(page['path'])}" target="_blank">Open page</a>
            <button type="button" class="button secondary small" data-copy-url="{escape(page['path'])}">Copy link</button>
          </div>
        </li>
        """
        for page in public_capture_pages()
    )
    body = f"""
    <section class="page-head">
      <div>
        <h2>Staff lead capture</h2>
        <p>Capture a new contact or interaction.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/join" target="_blank">Open website signup</a>
        <a class="button secondary" href="/join/qr" target="_blank">Open QR signup</a>
      </div>
    </section>

    <section class="grid wide-grid">
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
      <div class="panel">
        <h3>Public capture pages</h3>
        <ul class="stacked-list">{public_page_rows}</ul>
        <p class="muted">Public links feed new contacts into the CRM.</p>
      </div>
    </section>
    """
    return base_layout("Capture", body, flash=message, active_section="capture")


class SeaviewCRMHandler(BaseHTTPRequestHandler):
    server_version = "SeaviewCRM/0.2"

    def is_authenticated(self) -> bool:
        return authenticated_username(self.headers.get("Cookie")) is not None

    def requires_auth(self, path: str) -> bool:
        if path == "/login":
            return False
        if path.startswith("/static/"):
            return False
        if path in PUBLIC_CAPTURE_VARIANTS:
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        message = params.get("message", [""])[0]
        prune_pending_imports()

        if self.requires_auth(parsed.path) and not self.is_authenticated():
            self.respond_redirect("/login?" + message_query("Staff login required."))
            return

        if parsed.path == "/login":
            if self.is_authenticated():
                self.respond_redirect("/")
                return
            self.respond_html(render_login(message))
            return
        if parsed.path in PUBLIC_CAPTURE_VARIANTS:
            self.respond_html(render_public_capture(parsed.path, message))
            return
        if parsed.path == "/":
            self.respond_html(render_dashboard(message))
            return
        if parsed.path == "/customers":
            self.respond_html(render_customers(params.get("q", [""])[0]))
            return
        if parsed.path == "/tasks":
            self.respond_html(render_tasks(message))
            return
        if parsed.path == "/customers/new":
            self.respond_html(render_customer_form(message))
            return
        if parsed.path.startswith("/customers/") and parsed.path.endswith("/edit"):
            try:
                customer_id = int(parsed.path.split("/")[-2])
            except ValueError:
                self.respond_not_found()
                return
            customer = get_customer_record(customer_id)
            if not customer:
                self.respond_not_found()
                return
            self.respond_html(render_customer_form(message, customer=customer))
            return
        if parsed.path.startswith("/customers/"):
            try:
                customer_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.respond_not_found()
                return
            self.respond_html(render_customer_detail(customer_id, message))
            return
        if parsed.path == "/marketing/export":
            segment_key = params.get("segment", [""])[0]
            if segment_key not in segment_definitions():
                self.respond_not_found()
                return
            campaign_id = None
            campaign_id_value = params.get("campaign_id", [""])[0]
            if campaign_id_value:
                try:
                    campaign_id = int(campaign_id_value)
                except ValueError:
                    campaign_id = None
            payload = export_segment_csv(segment_key)
            log_segment_export(segment_key, campaign_id)
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
        if parsed.path.startswith("/imports/preview/"):
            import_id = parsed.path.rsplit("/", 1)[-1]
            self.respond_html(render_import_preview(import_id, message))
            return
        if parsed.path in {"/signup", "/capture"}:
            self.respond_html(render_signup(message))
            return
        if parsed.path == "/logout":
            self.respond_redirect("/login?" + message_query("You have been signed out."), headers={"Set-Cookie": clear_session_cookie_value()})
            return
        if parsed.path == "/static/styles.css":
            css = static_asset_bytes("static/styles.css")
            self.respond_bytes(css, "text/css; charset=utf-8", headers={"Cache-Control": "public, max-age=300"})
            return
        if parsed.path == "/static/app.js":
            js = static_asset_bytes("static/app.js")
            self.respond_bytes(js, "text/javascript; charset=utf-8", headers={"Cache-Control": "public, max-age=300"})
            return
        self.respond_not_found()

    def do_POST(self) -> None:
        prune_pending_imports()
        if self.path == "/login":
            fields = self.parse_urlencoded()
            username = fields.get("username", "").strip()
            password = fields.get("password", "")
            if not valid_staff_credentials(username, password):
                self.respond_redirect("/login?" + message_query("Invalid staff credentials."))
                return
            self.respond_redirect("/", headers={"Set-Cookie": auth_cookie_header(username)})
            return
        if self.path in PUBLIC_CAPTURE_VARIANTS:
            fields = self.parse_urlencoded()
            page = public_capture_page(self.path)
            if page:
                fields["touchpoint_type"] = page["touchpoint_type"]
                if not fields.get("preferred_channel"):
                    fields["preferred_channel"] = page["preferred_channel"]
            result = create_touchpoint_capture(fields, public_signup=True)
            if result["error"]:
                self.respond_redirect(f"{self.path}?{message_query(result['error'])}")
                return
            self.respond_redirect(f"{self.path}?{message_query('Thanks. Your contact details were saved for Seaview updates.')}")
            return

        if self.requires_auth(self.path) and not self.is_authenticated():
            self.respond_redirect("/login?" + message_query("Staff login required."))
            return

        if self.path == "/imports":
            self.handle_import_upload()
            return
        if self.path == "/tasks":
            fields = self.parse_urlencoded()
            result = create_task(fields)
            customer_id = fields.get("customer_id", "").strip()
            if result["error"]:
                redirect_path = f"/customers/{customer_id}" if customer_id else "/tasks"
                self.respond_redirect(f"{redirect_path}?{message_query(result['error'])}")
                return
            redirect_path = f"/customers/{customer_id}" if customer_id else "/tasks"
            self.respond_redirect(f"{redirect_path}?{message_query('Task created.')}")
            return
        if self.path == "/tasks/complete":
            fields = self.parse_urlencoded()
            try:
                task_id = int(fields.get("task_id", "0"))
            except ValueError:
                self.respond_redirect("/tasks?" + message_query("Task not found."))
                return
            customer_id = fields.get("customer_id", "").strip()
            updated = complete_task(task_id)
            redirect_path = f"/customers/{customer_id}" if customer_id else "/tasks"
            message_text = "Task completed." if updated else "Task not found."
            self.respond_redirect(f"{redirect_path}?{message_query(message_text)}")
            return
        if self.path == "/imports/confirm":
            fields = self.parse_urlencoded()
            import_id = fields.get("import_id", "")
            pending = PENDING_IMPORTS.get(import_id)
            if not pending:
                self.respond_redirect("/imports?" + message_query("Import preview expired. Upload the file again."))
                return
            analysis = pending.get("analysis") or analyze_import_rows(pending["rows"])
            if not analysis["can_import"]:
                self.respond_redirect(f"/imports/preview/{import_id}?" + message_query("This file needs usable identity columns before it can be imported."))
                return
            pending = PENDING_IMPORTS.pop(import_id, None)
            if not pending:
                self.respond_redirect("/imports?" + message_query("Import preview expired. Upload the file again."))
                return
            result = import_rows(pending["source_system"], pending["filename"], pending["rows"])
            if result["error_message"]:
                self.respond_redirect("/imports?" + message_query("Import failed."))
                return
            message = (
                f"Imported {result['rows_received']} rows. Created {result['customers_created']} customers, "
                f"updated {result['customers_updated']}, skipped {result['skipped_rows']}, and added {result['purchase_events_created']} purchase records."
            )
            self.respond_redirect("/imports?" + message_query(message))
            return
        if self.path == "/imports/cancel":
            fields = self.parse_urlencoded()
            PENDING_IMPORTS.pop(fields.get("import_id", ""), None)
            self.respond_redirect("/imports?" + message_query("Import canceled."))
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
        if self.path == "/marketing/campaigns/send":
            fields = self.parse_urlencoded()
            try:
                campaign_id = int(fields.get("campaign_id", "0"))
            except ValueError:
                self.respond_redirect("/marketing?" + message_query("Campaign not found."))
                return
            result = mark_campaign_sent(campaign_id)
            if result["error"]:
                self.respond_redirect("/marketing?" + message_query(result["error"]))
                return
            if result["already_sent"]:
                self.respond_redirect("/marketing?" + message_query(f"{result['title']} was already marked sent."))
                return
            self.respond_redirect(
                "/marketing?"
                + message_query(
                    f"{result['title']} marked sent for {result['audience_count']} customers."
                )
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
        if self.path == "/customers/new":
            fields = self.parse_urlencoded()
            result = save_customer(fields)
            if result["error"]:
                self.respond_redirect("/customers/new?" + message_query(result["error"]))
                return
            self.respond_redirect(f"/customers/{result['customer_id']}?" + message_query("Customer created."))
            return
        if self.path.startswith("/customers/") and self.path.endswith("/edit"):
            try:
                customer_id = int(self.path.split("/")[-2])
            except ValueError:
                self.respond_not_found()
                return
            fields = self.parse_urlencoded()
            result = save_customer(fields, customer_id=customer_id)
            if result["error"]:
                self.respond_redirect(f"/customers/{customer_id}/edit?" + message_query(result["error"]))
                return
            self.respond_redirect(f"/customers/{customer_id}?" + message_query("Customer updated."))
            return
        if self.path.startswith("/customers/") and self.path.endswith("/notes"):
            try:
                customer_id = int(self.path.split("/")[-2])
            except ValueError:
                self.respond_not_found()
                return
            fields = self.parse_urlencoded()
            result = add_customer_note(customer_id, fields.get("body", ""))
            if result["error"]:
                self.respond_redirect(f"/customers/{customer_id}?" + message_query(result["error"]))
                return
            self.respond_redirect(f"/customers/{customer_id}?" + message_query("Note saved."))
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

        try:
            rows = parse_csv_bytes(file_part["content"])
        except Exception:
            self.respond_redirect("/imports?" + message_query("Upload failed: file could not be parsed as CSV."))
            return
        analysis = analyze_import_rows(rows)
        filename = save_upload(file_part["filename"], file_part["content"])
        import_id = uuid.uuid4().hex
        PENDING_IMPORTS[import_id] = {
            "source_system": source_system,
            "filename": filename,
            "rows": rows,
            "columns": analysis["columns"],
            "sample_rows": preview_rows(rows),
            "analysis": analysis,
            "created_at_ts": time.time(),
        }
        self.respond_redirect(f"/imports/preview/{import_id}")

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

    def respond_bytes(
        self,
        payload: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for header, value in (headers or {}).items():
            self.send_header(header, value)
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
