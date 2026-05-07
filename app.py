import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import socket
import sqlite3
import threading
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


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "seaview_crm.db"
UPLOADS_DIR = DATA_DIR / "uploads"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_COOKIE_NAME = "seaview_session"
SESSION_MAX_AGE = 60 * 60 * 12
PENDING_IMPORT_TTL_SECONDS = 60 * 30
DEFAULT_STAFF_USERNAME = os.environ.get("SEAVIEW_CRM_USERNAME", "seaview")
DEFAULT_STAFF_PASSWORD = os.environ.get("SEAVIEW_CRM_PASSWORD", "crabshack-demo")
SESSION_SECRET = os.environ.get("SEAVIEW_SESSION_SECRET", "seaview-internal-demo-secret")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000)
    return f"pbkdf2:sha256:310000:{salt}:{key.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        _, algo, iterations, salt, key_hex = stored_hash.split(":")
        key = hashlib.pbkdf2_hmac(algo, password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False

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

IMPORT_SOURCE_GUIDES = {
    "clover": {
        "label": "Clover weekly pull",
        "summary": "Best for customer purchases, repeat visits, and recent transaction history.",
        "steps": [
            "Export the latest customer or transaction CSV from Clover.",
            "Keep customer ID, name, email, phone, item, total, and transaction date columns when possible.",
            "Upload here first, confirm the preview, then review skipped rows before sending campaigns.",
        ],
        "sample_file": "/samples/clover_weekly_mock.csv",
    },
    "constant_contact": {
        "label": "Constant Contact list refresh",
        "summary": "Best for newsletter contacts, consent status, and segmented mailing lists.",
        "steps": [
            "Export the latest subscriber file with email, tags, and any opt-in columns.",
            "Use this import to enrich customer reachability and consent context.",
            "Review unmapped fields in preview so newsletter lists stay clean.",
        ],
        "sample_file": "/samples/website_signups.csv",
    },
    "legacy_csv": {
        "label": "Legacy spreadsheet cleanup",
        "summary": "Best for older lists, event sheets, and one-off customer exports.",
        "steps": [
            "Normalize names, email, phone, and notes if possible before uploading.",
            "Use preview to catch weak identity rows and duplicate risks before importing.",
            "After import, review duplicates and missing-contact customers from the dashboard.",
        ],
        "sample_file": "/samples/legacy_customers.csv",
    },
}

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
    "/join/receipt": {
        "label": "Receipt QR page",
        "headline": "Get your next Seaview offer by phone or email",
        "description": "A quick signup flow built for receipts, bag inserts, and takeout packaging where Seaview wants one more reachable touchpoint after checkout.",
        "touchpoint_type": "receipt_qr",
        "preferred_channel": "sms",
        "cta": "Get the next offer",
        "interest_placeholder": "weekend pickup deals, low country boil, shrimp trays",
        "capture_note": "Best for receipt QR codes, package stickers, and post-purchase inserts that turn a one-time visit into a reachable contact.",
        "benefits": [
            "Extends the interaction after checkout without slowing the line",
            "Works well for receipt and bag QR code campaigns",
            "Creates a clear post-purchase capture source inside the CRM",
        ],
    },
    "/join/events": {
        "label": "Event booth page",
        "headline": "Join Seaview updates at the event",
        "description": "A quick event and festival capture page for foot traffic, sampling days, and community booths where Seaview needs fast, lightweight signup.",
        "touchpoint_type": "event_booth",
        "preferred_channel": "either",
        "cta": "Join event updates",
        "interest_placeholder": "festival deals, seasonal oysters, crab specials, catering",
        "capture_note": "Use this page on event signage and festival booth QR cards so leads from one-day events do not disappear after the interaction.",
        "benefits": [
            "Turns event traffic into follow-up leads",
            "Keeps event and in-store capture separate for reporting",
            "Makes community outreach measurable inside the CRM",
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
    "/join/qr/counter": {
        "label": "Front Counter QR",
        "headline": "Scan for fresh deals from Seaview Crab",
        "description": "Counter QR signup for weekly specials and fresh catch deals.",
        "touchpoint_type": "in_store_qr",
        "preferred_channel": "sms",
        "cta": "Get the deal",
        "interest_placeholder": "weekend specials, crab boil, fresh catch",
        "capture_note": "Front counter placement.",
        "location_tag": "qr_counter",
        "benefits": [
            "Captures contact info without slowing down checkout",
            "Tracks signups by location inside the CRM",
        ],
    },
    "/join/qr/receipt": {
        "label": "Receipt QR",
        "headline": "Scan for fresh deals from Seaview Crab",
        "description": "Receipt QR signup — catch customers right after checkout.",
        "touchpoint_type": "receipt_qr",
        "preferred_channel": "sms",
        "cta": "Get the deal",
        "interest_placeholder": "next order deals, shrimp trays, weekend pickup",
        "capture_note": "Receipt or bag insert placement.",
        "location_tag": "qr_receipt",
        "benefits": [
            "Extends the interaction after checkout",
            "Creates a clear post-purchase capture source",
        ],
    },
    "/join/qr/table": {
        "label": "Table Tent QR",
        "headline": "Scan for fresh deals from Seaview Crab",
        "description": "Table tent QR for dine-in and wait area signups.",
        "touchpoint_type": "in_store_qr",
        "preferred_channel": "sms",
        "cta": "Get the deal",
        "interest_placeholder": "dine-in specials, crab boil, family packs",
        "capture_note": "Table tent placement.",
        "location_tag": "qr_table",
        "benefits": [
            "Captures signups while customers wait",
            "Tracks table placements separately from counter",
        ],
    },
    "/join/qr/event": {
        "label": "Event Booth QR",
        "headline": "Scan for fresh deals from Seaview Crab",
        "description": "Event and festival booth QR capture.",
        "touchpoint_type": "event_booth",
        "preferred_channel": "either",
        "cta": "Get the deal",
        "interest_placeholder": "festival deals, seasonal oysters, crab specials",
        "capture_note": "Event booth placement.",
        "location_tag": "qr_event",
        "benefits": [
            "Turns event traffic into follow-up leads",
            "Keeps event capture separate from in-store for reporting",
        ],
    },
}

QR_LOCATIONS = {
    "counter": {"label": "Front Counter", "touchpoint_type": "in_store_qr", "tag": "qr_counter", "path": "/join/qr/counter"},
    "receipt": {"label": "Receipt / Bag", "touchpoint_type": "receipt_qr", "tag": "qr_receipt", "path": "/join/qr/receipt"},
    "table": {"label": "Table Tent", "touchpoint_type": "in_store_qr", "tag": "qr_table", "path": "/join/qr/table"},
    "event": {"label": "Event Booth", "touchpoint_type": "event_booth", "tag": "qr_event", "path": "/join/qr/event"},
    "wholesale": {"label": "Wholesale", "touchpoint_type": "wholesale_inquiry", "tag": "qr_wholesale", "path": "/join/qr/wholesale"},
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
            review_needed_rows INTEGER NOT NULL DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS duplicate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_low_id INTEGER NOT NULL,
            customer_high_id INTEGER NOT NULL,
            primary_customer_id INTEGER,
            secondary_customer_id INTEGER,
            decision TEXT NOT NULL,
            reason TEXT,
            match_value TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(customer_low_id, customer_high_id)
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            business_name TEXT NOT NULL,
            primary_location TEXT,
            weekly_import_owner TEXT,
            weekly_outreach_day TEXT,
            primary_offer_hook TEXT,
            capture_prompt TEXT,
            preferred_primary_data_source TEXT,
            default_capture_cta TEXT,
            duplicate_review_required_before_campaign_export INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
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
        CREATE INDEX IF NOT EXISTS idx_duplicate_reviews_pair ON duplicate_reviews(customer_low_id, customer_high_id);
        """
    )

    ensure_column(conn, "customers", "preferred_channel", "TEXT")
    ensure_column(conn, "customers", "marketing_consent", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "customers", "acquisition_source", "TEXT")
    ensure_column(conn, "customers", "last_contacted_at", "TEXT")
    ensure_column(conn, "import_runs", "review_needed_rows", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "import_runs", "skipped_rows", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "app_settings", "preferred_primary_data_source", "TEXT")
    ensure_column(conn, "app_settings", "default_capture_cta", "TEXT")
    ensure_column(
        conn,
        "app_settings",
        "duplicate_review_required_before_campaign_export",
        "INTEGER NOT NULL DEFAULT 1",
    )
    ensure_column(conn, "touchpoints", "scan_location", "TEXT")

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
    settings_row = conn.execute("SELECT id FROM app_settings WHERE id = 1").fetchone()
    if not settings_row:
        conn.execute(
            """
            INSERT INTO app_settings (
                id, business_name, primary_location, weekly_import_owner, weekly_outreach_day,
                primary_offer_hook, capture_prompt, preferred_primary_data_source, default_capture_cta,
                duplicate_review_required_before_campaign_export, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Seaview Crab Company",
                "Wilmington, NC",
                "Owner or floor lead",
                "Thursday",
                "Weekly seafood specials and fresh catch alerts",
                "Ask every reachable guest for email or phone before checkout ends.",
                "clover",
                "Get Seaview updates",
                1,
                utc_now(),
            ),
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


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


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


def import_identity_details(row: dict) -> dict:
    first_name = value_for(row, "first_name")
    last_name = value_for(row, "last_name")
    if not first_name and not last_name:
        first_name, last_name = split_name(value_for(row, "full_name"))
    return {
        "external_id": value_for(row, "external_id"),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "email": value_for(row, "email").strip().lower(),
        "phone": normalize_phone(value_for(row, "phone").strip()),
        "city": value_for(row, "city").strip(),
        "state": value_for(row, "state").strip(),
    }


def import_row_decision_with_conn(conn: sqlite3.Connection, source_system: str, row: dict) -> dict:
    if not import_row_is_identifiable(row):
        return {"outcome": "skip", "reason": "Missing identity", "match_value": ""}

    identity = import_identity_details(row)
    if identity["email"]:
        existing = conn.execute(
            "SELECT * FROM customers WHERE lower(email) = ?",
            (identity["email"],),
        ).fetchone()
        if existing:
            return {"outcome": "merge", "reason": "Exact email", "match_value": identity["email"], "customer": existing}

    if identity["phone"]:
        existing = conn.execute(
            "SELECT * FROM customers WHERE phone = ?",
            (identity["phone"],),
        ).fetchone()
        if existing:
            return {"outcome": "merge", "reason": "Normalized phone", "match_value": identity["phone"], "customer": existing}

    if identity["external_id"]:
        existing = conn.execute(
            """
            SELECT *
            FROM customers
            WHERE external_id = ? AND source_system = ?
            """,
            (identity["external_id"], source_system),
        ).fetchone()
        if existing:
            return {
                "outcome": "merge",
                "reason": "External ID and source",
                "match_value": identity["external_id"],
                "customer": existing,
            }

    normalized_name = " ".join(part for part in [identity["first_name"].lower(), identity["last_name"].lower()] if part).strip()
    if normalized_name and (identity["city"] or identity["state"]):
        existing = conn.execute(
            """
            SELECT *
            FROM customers
            WHERE lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) = ?
              AND lower(COALESCE(city, '')) = ?
              AND lower(COALESCE(state, '')) = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_name, identity["city"].lower(), identity["state"].lower()),
        ).fetchone()
        if existing:
            return {
                "outcome": "review",
                "reason": "Same name and location",
                "match_value": build_name_location_match_value(
                    identity["first_name"],
                    identity["last_name"],
                    identity["city"],
                    identity["state"],
                ),
                "customer": existing,
            }

    return {"outcome": "create", "reason": "New record", "match_value": ""}


def analyze_import_rows(source_system: str, rows: list[dict]) -> dict:
    columns = preview_columns(rows)
    mapped_fields = []
    for group_label, field_keys in IMPORT_FIELD_GROUPS:
        fields = []
        for field_key in field_keys:
            matched_columns = matched_preview_columns(columns, field_key)
            if not matched_columns:
                continue
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
    outcome_counts = {"create": 0, "merge": 0, "review": 0, "skip": 0}
    conn = db_connection()
    try:
        for row in rows:
            outcome = import_row_decision_with_conn(conn, source_system, row)["outcome"]
            outcome_counts[outcome] += 1
    finally:
        conn.close()
    identifiable_rows = outcome_counts["create"] + outcome_counts["merge"] + outcome_counts["review"]
    contactable_rows = sum(
        1 for row in rows if normalize_phone(value_for(row, "phone")) or value_for(row, "email")
    )
    purchase_rows = sum(
        1
        for row in rows
        if value_for(row, "item_name") or value_for(row, "order_total") or value_for(row, "purchased_at")
    )
    consent_rows = sum(1 for row in rows if to_bool(value_for(row, "marketing_consent")))
    skipped_rows = outcome_counts["skip"]

    warnings = []
    blocking_warnings = []
    identity_columns_present = any(
        matched_preview_columns(columns, key)
        for key in ("external_id", "email", "phone", "full_name", "first_name", "last_name")
    )
    if not rows:
        blocking_warnings.append("No data rows were detected in this file. Upload a CSV or Excel file with a header row and customer records.")
    if not identity_columns_present:
        blocking_warnings.append("No supported identity columns were detected. Add email, phone, external ID, or customer name fields before importing.")
    elif identifiable_rows == 0:
        blocking_warnings.append("None of the rows contain enough identity data to create or update a customer record.")
    elif skipped_rows:
        warnings.append(f"{skipped_rows} rows are missing usable identity data and will be skipped during import.")
    if outcome_counts["review"]:
        warnings.append(
            f"{outcome_counts['review']} rows look similar to an existing customer by name and location and will be routed to duplicate review."
        )
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
        "create_rows": outcome_counts["create"],
        "merge_rows": outcome_counts["merge"],
        "review_rows": outcome_counts["review"],
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


def import_source_guides() -> list[dict]:
    ordered_keys = ["clover", "constant_contact", "legacy_csv"]
    return [{"key": key, **IMPORT_SOURCE_GUIDES[key]} for key in ordered_keys]


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


def pair_bounds(customer_a_id: int, customer_b_id: int) -> tuple[int, int]:
    return tuple(sorted((customer_a_id, customer_b_id)))


def duplicate_review_row_with_conn(
    conn: sqlite3.Connection, customer_a_id: int, customer_b_id: int
) -> sqlite3.Row | None:
    low_id, high_id = pair_bounds(customer_a_id, customer_b_id)
    return conn.execute(
        """
        SELECT *
        FROM duplicate_reviews
        WHERE customer_low_id = ? AND customer_high_id = ?
        """,
        (low_id, high_id),
    ).fetchone()


def save_duplicate_review_with_conn(
    conn: sqlite3.Connection,
    *,
    customer_a_id: int,
    customer_b_id: int,
    decision: str,
    primary_customer_id: int | None = None,
    secondary_customer_id: int | None = None,
    reason: str = "",
    match_value: str = "",
) -> None:
    low_id, high_id = pair_bounds(customer_a_id, customer_b_id)
    conn.execute(
        """
        INSERT INTO duplicate_reviews (
            customer_low_id, customer_high_id, primary_customer_id, secondary_customer_id,
            decision, reason, match_value, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_low_id, customer_high_id) DO UPDATE SET
            primary_customer_id = excluded.primary_customer_id,
            secondary_customer_id = excluded.secondary_customer_id,
            decision = excluded.decision,
            reason = excluded.reason,
            match_value = excluded.match_value,
            updated_at = excluded.updated_at
        """,
        (
            low_id,
            high_id,
            primary_customer_id,
            secondary_customer_id,
            decision,
            reason or None,
            match_value or None,
            utc_now(),
        ),
    )


def customer_priority_key(row: sqlite3.Row | dict) -> tuple:
    email = row["email"] if isinstance(row, sqlite3.Row) else row.get("email")
    phone = row["phone"] if isinstance(row, sqlite3.Row) else row.get("phone")
    total_spent = row["total_spent"] if isinstance(row, sqlite3.Row) else row.get("total_spent")
    updated_at = row["updated_at"] if isinstance(row, sqlite3.Row) else row.get("updated_at")
    created_at = row["created_at"] if isinstance(row, sqlite3.Row) else row.get("created_at")
    return (
        1 if email else 0,
        1 if phone else 0,
        float(total_spent or 0),
        parse_datetime(updated_at) or datetime.min.replace(tzinfo=UTC),
        parse_datetime(created_at) or datetime.min.replace(tzinfo=UTC),
    )


def select_primary_customer(rows: list[sqlite3.Row]) -> sqlite3.Row:
    return sorted(rows, key=customer_priority_key, reverse=True)[0]


def build_name_location_match_value(first_name: str, last_name: str, city: str, state: str) -> str:
    display = " ".join(part for part in [first_name.strip(), last_name.strip()] if part).strip() or "Unnamed customer"
    location = ", ".join(part for part in [city.strip(), state.strip()] if part)
    return f"{display} · {location}" if location else display


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
    phone = normalize_phone(phone.strip())
    now = utc_now()
    normalized_name = f"{first_name.strip().lower()} {last_name.strip().lower()}".strip()

    existing = None
    if email:
        existing = conn.execute("SELECT * FROM customers WHERE lower(email) = ?", (email,)).fetchone()
    if not existing and phone:
        existing = conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
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
            SET external_id = ?, first_name = ?, last_name = ?, email = ?, phone = ?, city = ?, state = ?, tags = ?, notes = ?,
                total_spent = ?, last_purchase_at = ?, preferred_channel = ?, marketing_consent = ?,
                acquisition_source = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                existing["external_id"] or external_id or None,
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
    review_needed = 0
    skipped = 0
    purchases = 0
    error_message = None
    try:
        for row in rows:
            decision = import_row_decision_with_conn(conn, source_system, row)
            if decision["outcome"] == "skip":
                skipped += 1
                continue
            customer_id, action = upsert_customer(conn, source_system, row)
            if action == "created":
                created += 1
            else:
                updated += 1
            if decision["outcome"] == "review":
                review_needed += 1
                matched_customer = decision["customer"]
                save_duplicate_review_with_conn(
                    conn,
                    customer_a_id=matched_customer["id"],
                    customer_b_id=customer_id,
                    decision="pending",
                    primary_customer_id=matched_customer["id"],
                    secondary_customer_id=customer_id,
                    reason=decision["reason"],
                    match_value=decision["match_value"],
                )

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
                source_system, filename, rows_received, customers_created, customers_updated, review_needed_rows,
                skipped_rows, purchase_events_created, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_system,
                filename,
                len(rows),
                created,
                updated,
                review_needed,
                skipped,
                purchases,
                "completed",
                None,
                utc_now(),
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        error_message = str(exc)
        conn.execute(
            """
            INSERT INTO import_runs (
                source_system, filename, rows_received, customers_created, customers_updated, review_needed_rows,
                skipped_rows, purchase_events_created, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_system,
                filename,
                len(rows),
                created,
                updated,
                review_needed,
                skipped,
                purchases,
                "failed",
                error_message,
                utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "rows_received": len(rows),
        "customers_created": created,
        "customers_updated": updated,
        "review_needed_rows": review_needed,
        "skipped_rows": skipped,
        "purchase_events_created": purchases,
        "error_message": error_message,
    }


def merge_customer_records_with_conn(
    conn: sqlite3.Connection, primary_customer_id: int, secondary_customer_id: int
) -> dict:
    if primary_customer_id == secondary_customer_id:
        return {"error": "Choose two different customer records to merge."}

    primary = get_customer_record(primary_customer_id, conn=conn)
    secondary = get_customer_record(secondary_customer_id, conn=conn)
    if not primary or not secondary:
        return {"error": "One of the duplicate records could not be found."}

    now = utc_now()
    merged_tags = merge_list_text(primary["tags"] or "", secondary["tags"] or "")
    merged_notes = merge_notes(
        primary["notes"],
        secondary["notes"],
        f"Merged duplicate record from {display_name(secondary)}.",
    )

    conn.execute("UPDATE purchase_events SET customer_id = ? WHERE customer_id = ?", (primary_customer_id, secondary_customer_id))
    conn.execute("UPDATE touchpoints SET customer_id = ? WHERE customer_id = ?", (primary_customer_id, secondary_customer_id))
    conn.execute("UPDATE tasks SET customer_id = ? WHERE customer_id = ?", (primary_customer_id, secondary_customer_id))
    conn.execute("UPDATE customer_notes SET customer_id = ? WHERE customer_id = ?", (primary_customer_id, secondary_customer_id))
    conn.execute(
        """
        UPDATE customers
        SET external_id = ?, source_system = ?, first_name = ?, last_name = ?, email = ?, phone = ?,
            city = ?, state = ?, tags = ?, notes = ?, total_spent = ?, last_purchase_at = ?,
            preferred_channel = ?, marketing_consent = ?, acquisition_source = ?, last_contacted_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            primary["external_id"] or secondary["external_id"],
            primary["source_system"] or secondary["source_system"],
            primary["first_name"] or secondary["first_name"],
            primary["last_name"] or secondary["last_name"],
            primary["email"] or secondary["email"],
            primary["phone"] or secondary["phone"],
            primary["city"] or secondary["city"],
            primary["state"] or secondary["state"],
            merged_tags or None,
            merged_notes,
            (primary["total_spent"] or 0) + (secondary["total_spent"] or 0),
            max_timestamp(primary["last_purchase_at"], secondary["last_purchase_at"]),
            primary["preferred_channel"] or secondary["preferred_channel"],
            1 if (primary["marketing_consent"] or secondary["marketing_consent"]) else 0,
            primary["acquisition_source"] or secondary["acquisition_source"],
            max_timestamp(primary["last_contacted_at"], secondary["last_contacted_at"]),
            now,
            primary_customer_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO customer_notes (customer_id, body, created_at)
        VALUES (?, ?, ?)
        """,
        (
            primary_customer_id,
            f"Merged duplicate record from {display_name(secondary)}.",
            now,
        ),
    )
    low_id, high_id = pair_bounds(primary_customer_id, secondary_customer_id)
    conn.execute(
        """
        DELETE FROM duplicate_reviews
        WHERE (customer_low_id = ? OR customer_high_id = ?)
          AND NOT (customer_low_id = ? AND customer_high_id = ?)
        """,
        (secondary_customer_id, secondary_customer_id, low_id, high_id),
    )
    save_duplicate_review_with_conn(
        conn,
        customer_a_id=primary_customer_id,
        customer_b_id=secondary_customer_id,
        decision="merged",
        primary_customer_id=primary_customer_id,
        secondary_customer_id=secondary_customer_id,
        reason="Manual duplicate merge",
        match_value=display_name(secondary),
    )
    conn.execute("DELETE FROM customers WHERE id = ?", (secondary_customer_id,))
    return {"error": None, "primary_customer_id": primary_customer_id, "secondary_customer_id": secondary_customer_id}


def merge_customer_records(primary_customer_id: int, secondary_customer_id: int) -> dict:
    conn = db_connection()
    try:
        result = merge_customer_records_with_conn(conn, primary_customer_id, secondary_customer_id)
        if not result["error"]:
            conn.commit()
        else:
            conn.rollback()
        return result
    finally:
        conn.close()


def dismiss_duplicate_candidate(
    primary_customer_id: int, secondary_customer_id: int, reason: str = "", match_value: str = ""
) -> dict:
    conn = db_connection()
    try:
        primary = get_customer_record(primary_customer_id, conn=conn)
        secondary = get_customer_record(secondary_customer_id, conn=conn)
        if not primary or not secondary:
            return {"error": "One of the duplicate records could not be found."}
        save_duplicate_review_with_conn(
            conn,
            customer_a_id=primary_customer_id,
            customer_b_id=secondary_customer_id,
            decision="keep_separate",
            primary_customer_id=primary_customer_id,
            secondary_customer_id=secondary_customer_id,
            reason=reason,
            match_value=match_value,
        )
        conn.commit()
        return {"error": None}
    finally:
        conn.close()


def create_touchpoint_capture(fields: dict, *, public_signup: bool = False) -> dict:
    email = fields.get("email", "").strip()
    phone = fields.get("phone", "").strip()
    first_name = fields.get("first_name", "").strip()
    last_name = fields.get("last_name", "").strip()
    if public_signup and not email and not phone:
        return {"error": "Capture at least an email or phone number so Seaview can follow up."}
    if not public_signup and not any([email, phone, first_name, last_name]):
        return {"error": "Add at least a name, email, or phone number before saving the capture."}

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
    location_tag = fields.get("location_tag", "").strip()
    scan_location = fields.get("scan_location", "").strip() or (location_tag.replace("qr_", "") if location_tag.startswith("qr_") else "")
    if location_tag:
        merged_tags = merge_list_text(
            interest_tags,
            "captured lead",
            touchpoint_type.replace("_", " "),
            location_tag,
            "public signup" if public_signup else "",
        )
    else:
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
            first_name=first_name,
            last_name=last_name,
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
                customer_id, touchpoint_type, summary, preferred_channel, consent_email, consent_sms, scan_location, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                touchpoint_type,
                summary or source_note,
                infer_preferred_channel(email, phone, preferred_channel),
                consent_email,
                consent_sms,
                scan_location or None,
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
        customer = get_customer_record(customer_id, conn=conn)
    finally:
        conn.close()

    is_reachable = bool((customer["email"] or "").strip() or (customer["phone"] or "").strip()) if customer else False
    result_state = "contact_incomplete" if not is_reachable else ("new_customer" if action == "created" else "existing_customer")
    return {
        "customer_id": customer_id,
        "action": action,
        "error": None,
        "reachable": is_reachable,
        "result_state": result_state,
    }


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
            SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS count_this_week,
            SUM(CASE WHEN consent_email = 1 OR consent_sms = 1 THEN 1 ELSE 0 END) AS opted_in
        FROM touchpoints
        GROUP BY touchpoint_type
        ORDER BY count DESC, touchpoint_type
        """
        ,
        (week_cutoff,),
    ).fetchall()
    source_map = {
        row["touchpoint_type"]: {
            "count": row["count"],
            "count_this_week": row["count_this_week"] or 0,
            "opted_in": row["opted_in"] or 0,
        }
        for row in grouped_rows
    }
    public_this_week = conn.execute(
        f"""
        SELECT COUNT(DISTINCT customer_id) AS count
        FROM touchpoints
        WHERE touchpoint_type IN ({", ".join("?" for _ in PUBLIC_CAPTURE_TOUCHPOINTS)})
          AND created_at >= ?
        """,
        (*PUBLIC_CAPTURE_TOUCHPOINTS, week_cutoff),
    ).fetchone()["count"]
    reachable_this_week = conn.execute(
        """
        SELECT COUNT(DISTINCT t.customer_id) AS count
        FROM touchpoints t
        JOIN customers c ON c.id = t.customer_id
        WHERE t.created_at >= ?
          AND (COALESCE(c.email, '') <> '' OR COALESCE(c.phone, '') <> '')
        """,
        (week_cutoff,),
    ).fetchone()["count"]
    follow_up_needed_this_week = conn.execute(
        """
        SELECT COUNT(DISTINCT t.customer_id) AS count
        FROM touchpoints t
        JOIN customers c ON c.id = t.customer_id
        LEFT JOIN tasks open_tasks
          ON open_tasks.customer_id = t.customer_id
         AND open_tasks.status = 'open'
        WHERE t.created_at >= ?
          AND open_tasks.id IS NULL
          AND (COALESCE(c.email, '') <> '' OR COALESCE(c.phone, '') <> '')
        """,
        (week_cutoff,),
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
    sources.sort(
        key=lambda item: (
            -source_map.get(item["key"], {}).get("count_this_week", 0),
            -item["count"],
            item["label"],
        )
    )
    top_source = sources[0] if sources else None
    qr_by_location = {}
    for loc_key in QR_LOCATIONS:
        qr_by_location[loc_key] = conn.execute(
            "SELECT COUNT(*) AS count FROM touchpoints WHERE scan_location = ?",
            (loc_key,),
        ).fetchone()["count"]
    return {
        "public_this_week": public_this_week,
        "public_total": public_total,
        "opted_in_total": opted_in_total,
        "website_total": website_total,
        "qr_total": qr_total,
        "wholesale_total": wholesale_total,
        "reachable_this_week": reachable_this_week,
        "unreachable_this_week": max(public_this_week - reachable_this_week, 0),
        "follow_up_needed_this_week": follow_up_needed_this_week,
        "top_source": top_source,
        "sources": sources,
        "source_map": source_map,
        "qr_by_location": qr_by_location,
    }


def probable_duplicate_snapshot_with_conn(conn: sqlite3.Connection) -> dict:
    candidates = duplicate_candidate_rows_with_conn(conn, limit=200)
    email_groups = sum(1 for row in candidates if row["reason"] == "Shared email")
    phone_groups = sum(1 for row in candidates if row["reason"] == "Shared phone")
    name_groups = sum(1 for row in candidates if row["reason"] == "Same name and location")
    return {
        "email_groups": email_groups,
        "phone_groups": phone_groups,
        "name_groups": name_groups,
        "candidate_groups": len(candidates),
    }


def duplicate_candidate_rows_with_conn(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    candidates: dict[tuple[int, int], dict] = {}
    reason_priority = {"Shared email": 0, "Shared phone": 1, "Same name and location": 2}

    def add_candidates(customers: list[sqlite3.Row], reason: str, match_value: str) -> None:
        if len(customers) < 2:
            return
        ordered = sorted(customers, key=customer_priority_key, reverse=True)
        primary = ordered[0]
        for secondary in ordered[1:]:
            review = duplicate_review_row_with_conn(conn, primary["id"], secondary["id"])
            if review and review["decision"] in {"keep_separate", "merged"}:
                continue
            low_id, high_id = pair_bounds(primary["id"], secondary["id"])
            if (low_id, high_id) in candidates:
                continue
            candidates[(low_id, high_id)] = {
                "reason": reason,
                "match_value": match_value,
                "primary": primary,
                "secondary": secondary,
                "customers": [primary, secondary],
                "sort_reason": reason_priority[reason],
            }

    email_rows = conn.execute(
        """
        SELECT lower(email) AS match_value
        FROM customers
        WHERE COALESCE(email, '') <> ''
        GROUP BY lower(email)
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, lower(email)
        LIMIT 20
        """
    ).fetchall()
    for row in email_rows:
        customers = conn.execute(
            """
            SELECT *
            FROM customers
            WHERE lower(email) = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (row["match_value"],),
        ).fetchall()
        add_candidates(customers, "Shared email", row["match_value"])

    phone_rows = conn.execute(
        """
        SELECT phone AS match_value
        FROM customers
        WHERE COALESCE(phone, '') <> ''
        GROUP BY phone
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, phone
        LIMIT 20
        """
    ).fetchall()
    for row in phone_rows:
        customers = conn.execute(
            """
            SELECT *
            FROM customers
            WHERE phone = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (row["match_value"],),
        ).fetchall()
        add_candidates(customers, "Shared phone", row["match_value"])

    name_rows = conn.execute(
        """
        SELECT
            lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) AS match_value,
            lower(COALESCE(city, '')) AS city,
            lower(COALESCE(state, '')) AS state
        FROM customers
        WHERE trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) <> ''
          AND (COALESCE(city, '') <> '' OR COALESCE(state, '') <> '')
        GROUP BY lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))), lower(COALESCE(city, '')), lower(COALESCE(state, ''))
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, match_value
        LIMIT 20
        """
    ).fetchall()
    for row in name_rows:
        customers = conn.execute(
            """
            SELECT *
            FROM customers
            WHERE lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) = ?
              AND lower(COALESCE(city, '')) = ?
              AND lower(COALESCE(state, '')) = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (row["match_value"], row["city"], row["state"]),
        ).fetchall()
        location = ", ".join(part.title() for part in [row["city"], row["state"]] if part)
        add_candidates(customers, "Same name and location", f"{row['match_value']} · {location}")

    ordered_candidates = sorted(
        candidates.values(),
        key=lambda item: (
            item["sort_reason"],
            -(item["primary"]["total_spent"] or 0),
            -(item["secondary"]["total_spent"] or 0),
            item["match_value"],
        ),
    )
    return ordered_candidates[:limit]


def dashboard_action_queue_with_conn(conn: sqlite3.Connection, metrics: dict, limit: int = 6) -> list[dict]:
    actions: list[dict] = []
    segment_counts = metrics["segment_counts"]
    duplicate_snapshot = metrics["duplicate_snapshot"]

    lapsed_rows = conn.execute(
        """
        SELECT id, first_name, last_name, last_purchase_at, total_spent
        FROM customers
        WHERE last_purchase_at IS NOT NULL
          AND last_purchase_at < ?
          AND total_spent > 0
          AND (COALESCE(email, '') <> '' OR COALESCE(phone, '') <> '')
        ORDER BY total_spent DESC, last_purchase_at ASC
        LIMIT 2
        """,
        ((datetime.now(UTC) - timedelta(days=45)).replace(microsecond=0).isoformat(),),
    ).fetchall()
    for row in lapsed_rows:
        actions.append(
            {
                "title": f"Win back {display_name(row)}",
                "body": f"No purchase since {display_timestamp(row['last_purchase_at'], include_time=False)}. Prior spend is ${(row['total_spent'] or 0):.2f}.",
                "href": f"/customers/{row['id']}",
                "label": "Open profile",
                "tone": "retention",
            }
        )

    missing_contact_rows = conn.execute(
        """
        SELECT id, first_name, last_name, tags, acquisition_source
        FROM customers
        WHERE COALESCE(email, '') = '' AND COALESCE(phone, '') = ''
        ORDER BY updated_at DESC
        LIMIT 2
        """
    ).fetchall()
    for row in missing_contact_rows:
        actions.append(
            {
                "title": f"Capture contact for {display_name(row)}",
                "body": f"Currently unreachable. Source: {acquisition_label(row['acquisition_source'])}.",
                "href": f"/customers/{row['id']}",
                "label": "Add contact",
                "tone": "capture",
            }
        )

    if duplicate_snapshot["candidate_groups"]:
        actions.append(
            {
                "title": "Review duplicate customer candidates",
                "body": f"{duplicate_snapshot['candidate_groups']} likely duplicate groups need cleanup before the next campaign export.",
                "href": "/duplicates",
                "label": "Open duplicate review",
                "tone": "data",
            }
        )

    recent_failed_import = conn.execute(
        """
        SELECT *
        FROM import_runs
        WHERE status <> 'completed'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if recent_failed_import:
        actions.append(
            {
                "title": "Resolve the latest import issue",
                "body": f"{source_system_label(recent_failed_import['source_system'])} import on {display_timestamp(recent_failed_import['created_at'])} needs review.",
                "href": "/imports",
                "label": "Open imports",
                "tone": "data",
            }
        )

    if segment_counts["recent_buyers"]:
        actions.append(
            {
                "title": "Send a recent-buyer follow-up",
                "body": f"{segment_counts['recent_buyers']} customers are still warm from the last 30 days.",
                "href": "/marketing",
                "label": "Build campaign",
                "tone": "retention",
            }
        )

    if metrics["open_tasks"]:
        actions.append(
            {
                "title": "Clear the open follow-up queue",
                "body": f"{metrics['open_tasks']} tasks are still open. Finish the nearest due follow-ups first.",
                "href": "/tasks",
                "label": "Open tasks",
                "tone": "ops",
            }
        )

    if not actions:
        actions.append(
            {
                "title": "CRM is up to date",
                "body": "No urgent issues were detected. Use this week to capture more leads and plan the next offer.",
                "href": "/capture",
                "label": "Capture lead",
                "tone": "ops",
            }
        )

    return actions[:limit]


def dashboard_insights(metrics: dict) -> list[dict]:
    segment_counts = metrics["segment_counts"]
    lead_capture = metrics["lead_capture"]
    duplicate_snapshot = metrics["duplicate_snapshot"]
    return [
        {
            "title": "Win-back audience",
            "body": f"{segment_counts['lapsed_buyers']} customers are ready for a comeback offer.",
            "href": "/marketing",
            "label": "Open marketing",
            "tone": "retention",
        },
        {
            "title": "Contact capture",
            "body": f"{segment_counts['missing_contact']} records still need email or phone, while {lead_capture['public_this_week']} new public leads came in this week.",
            "href": "/capture",
            "label": "Capture lead",
            "tone": "capture",
        },
        {
            "title": "Data health",
            "body": f"{duplicate_snapshot['candidate_groups']} likely duplicate groups and {metrics['imports']} imports are shaping the customer base.",
            "href": "/duplicates",
            "label": "Review data",
            "tone": "data",
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


def campaign_export_block_message_with_conn(conn: sqlite3.Connection) -> str | None:
    settings = conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
    if not settings or not settings["duplicate_review_required_before_campaign_export"]:
        return None
    pending_duplicates = duplicate_candidate_rows_with_conn(conn, limit=1)
    if pending_duplicates:
        return "Review duplicate candidates before exporting an audience list."
    return None


def dashboard_metrics_with_conn(
    conn: sqlite3.Connection,
    *,
    segment_counts: dict[str, int] | None = None,
    lead_capture: dict | None = None,
) -> dict:
    week_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    active_segment_counts = segment_counts or segment_counts_with_conn(conn)
    active_lead_capture = lead_capture or lead_capture_snapshot_with_conn(conn)
    duplicate_snapshot = probable_duplicate_snapshot_with_conn(conn)
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
        "duplicate_snapshot": duplicate_snapshot,
    }
    metrics["action_queue"] = dashboard_action_queue_with_conn(conn, metrics)
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


def list_customers(search: str = "", review_mode: str = "", filter_key: str = "") -> list[sqlite3.Row]:
    conn = db_connection()
    try:
        if review_mode == "duplicates":
            rows = []
            seen_ids = set()
            for group in duplicate_candidate_rows_with_conn(conn):
                for customer in group["customers"]:
                    if customer["id"] in seen_ids:
                        continue
                    seen_ids.add(customer["id"])
                    rows.append(customer)
            return rows
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
        cutoff_30 = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
        cutoff_45 = (datetime.now(UTC) - timedelta(days=45)).replace(microsecond=0).isoformat()
        filter_where = {
            "email_ready": ("COALESCE(email, '') <> ''", []),
            "needs_attention": ("COALESCE(email, '') = '' AND COALESCE(phone, '') = ''", []),
            "vip": ("total_spent >= 250", []),
            "lapsed": (f"last_purchase_at < '{cutoff_45}' AND total_spent > 0", []),
            "recent_buyers": (f"last_purchase_at >= '{cutoff_30}'", []),
        }
        where_clause, params = filter_where.get(filter_key, ("1=1", []))
        if search:
            like = f"%{search.lower()}%"
            rows = conn.execute(
                f"""
                SELECT *
                FROM customers
                WHERE ({where_clause})
                  AND (lower(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) LIKE ?
                   OR lower(COALESCE(email, '')) LIKE ?
                   OR lower(COALESCE(tags, '')) LIKE ?
                   OR lower(COALESCE(acquisition_source, '')) LIKE ?)
                ORDER BY updated_at DESC
                LIMIT 200
                """,
                (*params, like, like, like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM customers WHERE {where_clause} ORDER BY updated_at DESC LIMIT 200",
                params,
            ).fetchall()
        return rows
    finally:
        conn.close()


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


def customer_account_type(customer: sqlite3.Row) -> str:
    tags = (customer["tags"] or "").lower()
    acquisition_source = (customer["acquisition_source"] or "").lower()
    if "wholesale" in tags or acquisition_source == "wholesale_inquiry":
        return "Wholesale"
    return "Retail / consumer"


def customer_duplicate_candidates_with_conn(
    conn: sqlite3.Connection, customer_id: int, limit: int = 4
) -> list[dict]:
    rows = []
    for candidate in duplicate_candidate_rows_with_conn(conn, limit=80):
        if candidate["primary"]["id"] == customer_id or candidate["secondary"]["id"] == customer_id:
            rows.append(candidate)
        if len(rows) >= limit:
            break
    return rows


def customer_record_health_with_conn(
    conn: sqlite3.Connection,
    customer: sqlite3.Row,
    touchpoints: list[sqlite3.Row],
    open_tasks_count: int,
) -> list[dict]:
    duplicate_candidates = customer_duplicate_candidates_with_conn(conn, customer["id"], limit=4)
    last_purchase = parse_datetime(customer["last_purchase_at"])
    if last_purchase:
        days_since_purchase = (datetime.now(UTC) - last_purchase).days
        recency_label = f"{days_since_purchase} day{'s' if days_since_purchase != 1 else ''} since purchase"
    else:
        recency_label = "No purchase history yet"
    return [
        {
            "label": "Account type",
            "value": customer_account_type(customer),
            "tone": "neutral",
        },
        {
            "label": "Reachability",
            "value": "Reachable" if customer["email"] or customer["phone"] else "Missing contact details",
            "tone": "good" if customer["email"] or customer["phone"] else "warning",
        },
        {
            "label": "Consent status",
            "value": "Confirmed for outreach" if customer["marketing_consent"] else "Consent still needed",
            "tone": "good" if customer["marketing_consent"] else "warning",
        },
        {
            "label": "Duplicate risk",
            "value": f"{len(duplicate_candidates)} review candidate{'s' if len(duplicate_candidates) != 1 else ''}" if duplicate_candidates else "No open duplicate review",
            "tone": "warning" if duplicate_candidates else "good",
        },
        {
            "label": "Purchase recency",
            "value": recency_label,
            "tone": "warning" if last_purchase and (datetime.now(UTC) - last_purchase).days > 45 else "neutral",
        },
        {
            "label": "Follow-up coverage",
            "value": "Open follow-up queued" if open_tasks_count else "No open follow-up",
            "tone": "good" if open_tasks_count else "warning",
        },
        {
            "label": "Recent interaction",
            "value": "Touchpoint logged recently" if touchpoints else "No touchpoints logged yet",
            "tone": "neutral",
        },
    ]


def customer_next_actions(
    customer: sqlite3.Row, open_tasks_count: int, touchpoints: list[sqlite3.Row]
) -> list[str]:
    actions = []
    if not customer["email"] and not customer["phone"]:
        actions.append("Capture an email or phone number on the next visit so this customer becomes reachable.")
    elif not customer["marketing_consent"]:
        actions.append("Confirm outreach consent before the next campaign export so this contact can be used safely.")

    last_purchase = parse_datetime(customer["last_purchase_at"])
    if last_purchase and (datetime.now(UTC) - last_purchase).days > 45:
        actions.append("Queue a win-back offer now, because this customer has gone quiet after a previous purchase.")
    elif last_purchase:
        actions.append("Follow up while the last purchase is still fresh and offer a next-visit deal.")
    elif touchpoints and customer["marketing_consent"]:
        actions.append("Send a welcome offer to convert this new signup into a first purchase.")

    if customer_account_type(customer) == "Wholesale":
        actions.append("Treat this as a wholesale relationship and schedule a restock or availability check-in.")
    elif (customer["total_spent"] or 0) >= 250:
        actions.append("Use premium or seasonal inventory alerts to retain this high-value customer.")

    if open_tasks_count == 0:
        actions.append("Create one follow-up task so this record has a clear next owner and next step.")
    return actions[:3]


def customer_relationship_summary(
    customer: sqlite3.Row,
    events: list[sqlite3.Row],
    touchpoints: list[sqlite3.Row],
    notes: list[sqlite3.Row],
    tasks: list[sqlite3.Row],
) -> list[str]:
    summary = []
    summary.append(f"This record is currently treated as a {customer_account_type(customer).lower()} relationship.")
    if events:
        summary.append(f"{len(events)} purchase event{'s' if len(events) != 1 else ''} are already attached to this record.")
    else:
        summary.append("No purchases are attached yet, so this record is still mostly relationship and capture context.")

    if touchpoints:
        summary.append(f"{len(touchpoints)} capture or interaction touchpoint{'s' if len(touchpoints) != 1 else ''} show how this customer has engaged.")
    else:
        summary.append("No touchpoints have been logged yet, so the relationship history is still thin.")

    if notes:
        summary.append(f"{len(notes)} internal note{'s' if len(notes) != 1 else ''} are helping preserve staff context.")

    completed_tasks = sum(1 for row in tasks if row["status"] == "completed")
    if completed_tasks:
        summary.append(f"{completed_tasks} follow-up task{'s' if completed_tasks != 1 else ''} have already been completed for this customer.")
    return summary[:4]


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
    phone = normalize_phone(fields.get("phone", "").strip())
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


def qr_page_items() -> list[dict]:
    placement_copy = {
        "/join": "Use on the Seaview website, footer, social bio, and email signatures.",
        "/join/qr": "Use on counter signs, table tents, window decals, and in-store collateral.",
        "/join/receipt": "Use on receipts, takeout bags, pickup inserts, and package stickers.",
        "/join/events": "Use on event signage, booth cards, and festival handouts.",
        "/join/wholesale": "Use in wholesale outreach, reorder decks, and account follow-up emails.",
    }
    pages = []
    for page in public_capture_pages():
        pages.append(
            {
                **page,
                "placement": placement_copy.get(page["path"], page["capture_note"]),
                "download_name": f"seaview-{page['touchpoint_type']}.png",
            }
        )
    return pages


def primary_record_reason(primary: sqlite3.Row, secondary: sqlite3.Row) -> str:
    reasons = []
    if primary["email"] and not secondary["email"]:
        reasons.append("has an email address")
    if primary["phone"] and not secondary["phone"]:
        reasons.append("has a phone number")
    if (primary["total_spent"] or 0) > (secondary["total_spent"] or 0):
        reasons.append("has higher recorded spend")
    primary_updated = parse_datetime(primary["updated_at"])
    secondary_updated = parse_datetime(secondary["updated_at"])
    if primary_updated and secondary_updated and primary_updated > secondary_updated:
        reasons.append("was updated more recently")
    if not reasons:
        return "Chosen as the primary record because it is the strongest current customer profile."
    return "Chosen as the primary record because it " + ", ".join(reasons[:3]) + "."


def duplicate_comparison_rows(primary: sqlite3.Row, secondary: sqlite3.Row) -> list[tuple[str, str, str]]:
    return [
        ("Name", display_name(primary), display_name(secondary)),
        ("Email", primary["email"] or "—", secondary["email"] or "—"),
        ("Phone", primary["phone"] or "—", secondary["phone"] or "—"),
        ("Source", source_system_label(primary["source_system"]), source_system_label(secondary["source_system"])),
        ("Acquisition", acquisition_label(primary["acquisition_source"]), acquisition_label(secondary["acquisition_source"])),
        ("Tags", primary["tags"] or "—", secondary["tags"] or "—"),
        ("Total spend", f"${(primary['total_spent'] or 0):.2f}", f"${(secondary['total_spent'] or 0):.2f}"),
        (
            "Last purchase",
            display_timestamp(primary["last_purchase_at"], include_time=False) if primary["last_purchase_at"] else "—",
            display_timestamp(secondary["last_purchase_at"], include_time=False) if secondary["last_purchase_at"] else "—",
        ),
        (
            "Last updated",
            display_timestamp(primary["updated_at"], include_time=False),
            display_timestamp(secondary["updated_at"], include_time=False),
        ),
    ]


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


def session_cookie_value(user_id: int, username: str, role: str) -> str:
    expires_at = int(time.time()) + SESSION_MAX_AGE
    payload = f"{user_id}|{username}|{role}|{expires_at}"
    sig = hmac.new(
        SESSION_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}|{sig}"


def session_cookie_attributes(*, secure: bool = False, max_age: int) -> str:
    parts = ["Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie_value(*, secure: bool = False) -> str:
    return f"{SESSION_COOKIE_NAME}=; {session_cookie_attributes(secure=secure, max_age=0)}"


def auth_cookie_header_v2(user_id: int, username: str, role: str, *, secure: bool = False) -> str:
    return (
        f"{SESSION_COOKIE_NAME}={session_cookie_value(user_id, username, role)}; "
        f"{session_cookie_attributes(secure=secure, max_age=SESSION_MAX_AGE)}"
    )


def get_current_user(cookie_header: str | None) -> dict | None:
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
        user_id_s, username, role, expires_at, sig = session.value.split("|", 4)
        payload = f"{user_id_s}|{username}|{role}|{expires_at}"
        expected = hmac.new(
            SESSION_SECRET.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(expires_at) < int(time.time()):
            return None
    except Exception:
        return None

    conn = db_connection()
    try:
        user = conn.execute(
            """
            SELECT id, username, display_name, role, is_active
            FROM staff_users
            WHERE id = ?
              AND username = ?
            """,
            (int(user_id_s), username),
        ).fetchone()
        if not user or not user["is_active"]:
            return None
        return {
            "id": int(user["id"]),
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
        }
    finally:
        conn.close()


def authenticated_username(cookie_header: str | None) -> str | None:
    user = get_current_user(cookie_header)
    return user["username"] if user else None


def valid_staff_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, DEFAULT_STAFF_USERNAME) and hmac.compare_digest(password, DEFAULT_STAFF_PASSWORD)


def get_setting(key: str, default: str = "") -> str:
    conn = db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?",
            (key,),
        ).fetchone()
        return (row["value"] or default) if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = db_connection()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO system_settings
            (key, value, updated_at) VALUES (?, ?, ?)
            """,
            (key, value, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def log_audit(
    conn: sqlite3.Connection,
    user_id: int | None,
    username: str,
    action: str,
    detail: str = "",
    ip: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log
        (user_id, username, action, detail, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, action, detail, ip, utc_now()),
    )


def get_app_settings() -> sqlite3.Row:
    conn = db_connection()
    try:
        row = conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
        return row
    finally:
        conn.close()


def save_app_settings(fields: dict) -> None:
    conn = db_connection()
    try:
        conn.execute(
            """
            UPDATE app_settings
            SET business_name = ?, primary_location = ?, weekly_import_owner = ?, weekly_outreach_day = ?,
                primary_offer_hook = ?, capture_prompt = ?, preferred_primary_data_source = ?,
                default_capture_cta = ?, duplicate_review_required_before_campaign_export = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                fields.get("business_name", "").strip() or "Seaview Crab Company",
                fields.get("primary_location", "").strip() or None,
                fields.get("weekly_import_owner", "").strip() or None,
                fields.get("weekly_outreach_day", "").strip() or None,
                fields.get("primary_offer_hook", "").strip() or None,
                fields.get("capture_prompt", "").strip() or None,
                fields.get("preferred_primary_data_source", "").strip() or "clover",
                fields.get("default_capture_cta", "").strip() or "Get Seaview updates",
                1 if fields.get("duplicate_review_required_before_campaign_export") else 0,
                utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def base_layout(
    title: str,
    body: str,
    flash: str = "",
    active_section: str = "",
    user: dict | None = None,
) -> bytes:
    flash_html = f"<div class='flash'>{escape(flash)}</div>" if flash else ""
    styles_href = asset_url("static/styles.css")
    script_src = asset_url("static/app.js")
    nav_items = [
        ("dashboard", "Dashboard", "/"),
        ("customers", "Customers", "/customers"),
        ("marketing", "Marketing", "/marketing"),
        ("capture", "Capture", "/capture"),
        ("imports", "Imports", "/imports"),
    ]
    if user and user.get("role") == "admin":
        nav_items.append(("admin", "Settings", "/admin"))
    nav_html = "".join(
        f"<a href='{escape(href)}' class='nav-link{' active' if key == active_section else ''}'>"
        f"<span class='nav-link-label'>{escape(label)}</span>"
        "</a>"
        for key, label, href in nav_items
    )
    user_html = ""
    if user:
        admin_link = (
            "<a href='/admin' class='sidebar-admin-link'>Settings</a>"
            if user.get("role") == "admin"
            else ""
        )
        user_html = f"""
        <div class='sidebar-user'>
          <span class='sidebar-user-name'>
            {escape(user.get('display_name', user['username']))}
          </span>
          <span class='sidebar-user-role'>
            {escape(user.get('role', 'staff'))}
          </span>
        </div>
        {admin_link}
        """
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
          <p>Customer operating system.</p>
        </div>
      </div>
      <nav>
        {nav_html}
      </nav>
      <div class="sidebar-footer">
        {user_html}
        <a href="/logout" class="sidebar-logout">Sign out</a>
        <span class="sidebar-version">Seaview CRM &middot; v1.0</span>
      </div>
    </aside>
    <main class="content">
      {flash_html}
      {body}
    </main>
  </div>
  <div id="toast" class="toast hidden" role="alert" aria-live="polite"></div>
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


def local_network_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def local_capture_origin() -> str:
    configured = os.environ.get("LOCAL_CAPTURE_HOST", "").strip()
    if configured:
        return configured.rstrip("/")
    host_name = socket.gethostname().strip()
    if host_name and not host_name.endswith(".local"):
        host_name = f"{host_name.split('.', 1)[0]}.local"
    if host_name:
        return f"http://{host_name}:{PORT}"
    return f"http://{local_network_ip()}:{PORT}"


def public_origin_for_request(handler: BaseHTTPRequestHandler) -> str:
    configured = (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("HOST_URL")
        or get_setting("public_base_url", "")
    ).strip()
    if configured:
        return configured.rstrip("/")

    forwarded_host = handler.headers.get("X-Forwarded-Host", "").strip()
    host = forwarded_host or handler.headers.get("Host", "").strip()
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").strip()
    proto = forwarded_proto or ("https" if forwarded_host else "http")
    host_name = host.split(":", 1)[0].lower()
    if host and host_name not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        return f"{proto}://{host}".rstrip("/")
    return local_capture_origin().rstrip("/")


def public_origin_for_display() -> str:
    configured = (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("HOST_URL")
        or get_setting("public_base_url", "")
    ).strip()
    if configured:
        return configured.rstrip("/")
    return local_capture_origin().rstrip("/")


def qr_capture_url(handler: BaseHTTPRequestHandler, loc_key: str) -> str:
    return f"{public_origin_for_request(handler)}{QR_LOCATIONS[loc_key]['path']}"


def public_capture_url(handler: BaseHTTPRequestHandler, path: str) -> str:
    return f"{public_origin_for_request(handler)}{path}"


def qr_filename_slug(path_or_key: str) -> str:
    cleaned = path_or_key.strip().strip("/") or "join"
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in cleaned)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "join"


def qr_target_from_params(
    handler: BaseHTTPRequestHandler,
    params: dict[str, list[str]],
) -> dict | None:
    loc_key = params.get("location", [""])[0]
    if loc_key in QR_LOCATIONS:
        loc = QR_LOCATIONS[loc_key]
        return {
            "label": loc["label"],
            "path": loc["path"],
            "url": qr_capture_url(handler, loc_key),
            "slug": loc_key,
        }

    page_path = params.get("page", [""])[0]
    page = PUBLIC_CAPTURE_VARIANTS.get(page_path)
    if page:
        return {
            "label": page["label"],
            "path": page_path,
            "url": public_capture_url(handler, page_path),
            "slug": qr_filename_slug(page_path),
        }
    return None


def render_public_capture(path: str, message: str = "") -> bytes:
    page = public_capture_page(path)
    if not page:
        return public_layout("Not Found", "<div class='panel'><h2>Page not found</h2></div>", flash=message)
    settings = get_app_settings()
    offer_hook = settings["primary_offer_hook"] or "Seaview customer signup"
    cta_label = settings["default_capture_cta"] or page["cta"]
    location_tag = page.get("location_tag", "")

    body = f"""
    <section class="panel signup-panel public-capture-card">
      <div class="public-capture-head">
        <h2>Join Seaview updates</h2>
        <p>Leave your email or phone number so Seaview can send specials and fresh-catch updates.</p>
      </div>
      <form method="post" action="{escape(path)}" class="stack public-capture-form" data-qr-form>
          <input type="hidden" name="touchpoint_type" value="{escape(page['touchpoint_type'])}">
          <input type="hidden" name="source_label" value="{escape(page['label'])}">
          <input type="hidden" name="capture_offer" value="{escape(offer_hook)}">
          <input type="hidden" name="location_tag" value="{escape(location_tag)}">
          <input type="hidden" name="preferred_channel" value="{escape(page['preferred_channel'])}">
          <input type="hidden" name="consent_email" value="1">
          <input type="hidden" name="consent_sms" value="1">
          <p class="inline-error" data-form-error hidden></p>
          <label>First name
            <input type="text" name="first_name" autocomplete="given-name" inputmode="text">
          </label>
          <label>Last name
            <input type="text" name="last_name" autocomplete="family-name" inputmode="text">
          </label>
          <label>Email
            <input type="email" name="email" placeholder="name@example.com" autocomplete="email" inputmode="email">
          </label>
          <label>Phone
            <input type="tel" name="phone" placeholder="(555) 555-5555" autocomplete="tel" inputmode="tel">
          </label>
          <p class="muted public-capture-note">Email or phone is required. Seaview will use this only for customer updates and offers.</p>
          <button type="submit">{escape(cta_label)}</button>
      </form>
      <div class="qr-confirm" data-qr-confirm>
        <div class="confirm-check">&#10003;</div>
        <h3>You're in!</h3>
        <p>Thanks. Seaview saved your signup.</p>
      </div>
    </section>
    """
    return public_layout("Join Seaview Updates", body, flash=message)


def render_qr_tools(message: str = "", user: dict | None = None) -> bytes:
    settings = get_app_settings()
    qr_cards = "".join(
        f"""
        <article class="panel qr-card">
          <div class="qr-card-copy">
            <span class="eyebrow">{escape(page['label'])}</span>
            <h3>{escape(page['headline'])}</h3>
            <p>{escape(page['placement'])}</p>
            <div class="copy-row">
              <a class="button secondary small" href="{escape(page['path'])}" target="_blank">Open page</a>
              <button type="button" class="button secondary small" data-copy-url="{escape(page['path'])}">Copy link</button>
              <button
                type="button"
                class="button small"
                data-qr-download
                data-qr-path="{escape(page['path'])}"
                data-qr-name="{escape(page['download_name'])}"
              >Download QR</button>
            </div>
          </div>
          <div class="qr-preview" data-qr-card data-qr-path="{escape(page['path'])}" data-qr-name="{escape(page['download_name'])}">
            <img alt="{escape(page['label'])} QR preview" class="qr-image" data-qr-image>
            <code data-qr-target></code>
          </div>
        </article>
        """
        for page in qr_page_items()
    )
    body = f"""
    <section class="page-head">
      <div>
        <h2>QR Kits</h2>
        <p>Download QR codes for each customer capture point.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/capture">Open capture</a>
      </div>
    </section>

    <section class="qr-grid">
      {qr_cards}
    </section>
    """
    return base_layout("QR Kits", body, flash=message, active_section="qr", user=user)


def render_duplicate_review(message: str = "", user: dict | None = None) -> bytes:
    conn = db_connection()
    try:
        groups = duplicate_candidate_rows_with_conn(conn, limit=14)
        total_count = probable_duplicate_snapshot_with_conn(conn)["candidate_groups"]
    finally:
        conn.close()

    candidate_cards = ""
    for group in groups:
        comparison_rows = "".join(
            f"<tr><th>{escape(label)}</th><td>{escape(primary_value)}</td><td>{escape(secondary_value)}</td></tr>"
            for label, primary_value, secondary_value in duplicate_comparison_rows(group["primary"], group["secondary"])
        )
        candidate_cards += f"""
        <article class="panel duplicate-panel">
          <div class="duplicate-panel-head">
            <div>
              <span class="eyebrow">{escape(group['reason'])}</span>
              <h3>{escape(group['match_value'])}</h3>
              <p>{escape(primary_record_reason(group['primary'], group['secondary']))}</p>
            </div>
            <div class="table-actions">
              <form method="post" action="/customers/duplicates/merge" class="inline-form">
                <input type="hidden" name="primary_customer_id" value="{group['primary']['id']}">
                <input type="hidden" name="secondary_customer_id" value="{group['secondary']['id']}">
                <button type="submit">Merge into primary</button>
              </form>
              <form method="post" action="/customers/duplicates/dismiss" class="inline-form">
                <input type="hidden" name="primary_customer_id" value="{group['primary']['id']}">
                <input type="hidden" name="secondary_customer_id" value="{group['secondary']['id']}">
                <input type="hidden" name="reason" value="{escape(group['reason'])}">
                <input type="hidden" name="match_value" value="{escape(group['match_value'])}">
                <button type="submit" class="button secondary">Keep separate</button>
              </form>
            </div>
          </div>
          <div class="duplicate-compare">
            <div class="duplicate-profile primary-choice">
              <div class="duplicate-profile-head">
                <span class="pill">Primary record</span>
                <strong>{escape(display_name(group['primary']))}</strong>
              </div>
              <a href="/customers/{group['primary']['id']}">Open profile</a>
            </div>
            <div class="duplicate-profile">
              <div class="duplicate-profile-head">
                <span class="pill muted-pill">Review record</span>
                <strong>{escape(display_name(group['secondary']))}</strong>
              </div>
              <a href="/customers/{group['secondary']['id']}">Open profile</a>
            </div>
          </div>
          <table class="compare-table">
            <thead><tr><th>Field</th><th>Primary</th><th>Review</th></tr></thead>
            <tbody>{comparison_rows}</tbody>
          </table>
        </article>
        """
    if not candidate_cards:
        candidate_cards = "<div class='panel'><h3>Duplicate review is clear</h3><p>No customer pairs need review right now.</p></div>"

    body = f"""
    <section class="page-head">
      <div>
        <h2>Duplicate Review</h2>
        <p>{total_count} customer pair{'s' if total_count != 1 else ''} need a decision.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/customers">Back to customers</a>
        <a class="button secondary" href="/marketing">Open marketing</a>
      </div>
    </section>

    <section class="duplicate-grid">
      {candidate_cards}
    </section>
    """
    return base_layout("Duplicate Review", body, flash=message, active_section="duplicates", user=user)


def reporting_snapshot() -> dict:
    conn = db_connection()
    try:
        segment_counts = segment_counts_with_conn(conn)
        lead_capture = lead_capture_snapshot_with_conn(conn)
        total_customers = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
        reachable_customers = conn.execute(
            "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> '' OR COALESCE(phone, '') <> ''"
        ).fetchone()["count"]
        campaign_counts = conn.execute(
            "SELECT status, COUNT(*) AS count FROM campaigns GROUP BY status ORDER BY count DESC"
        ).fetchall()
        lead_sources = conn.execute(
            """
            SELECT touchpoint_type, COUNT(*) AS count
            FROM touchpoints
            GROUP BY touchpoint_type
            ORDER BY count DESC
            LIMIT 8
            """
        ).fetchall()
        lead_rows = conn.execute(
            """
            SELECT created_at
            FROM touchpoints
            ORDER BY created_at DESC
            LIMIT 80
            """
        ).fetchall()
        import_rows = conn.execute(
            """
            SELECT created_at, rows_received, customers_created, customers_updated, review_needed_rows, skipped_rows
            FROM import_runs
            ORDER BY created_at DESC
            LIMIT 8
            """
        ).fetchall()
        outreach_rows = conn.execute(
            """
            SELECT created_at, event_type, audience_count
            FROM outreach_history
            ORDER BY created_at DESC
            LIMIT 12
            """
        ).fetchall()
    finally:
        conn.close()

    def bucket_week(rows: list[sqlite3.Row], value_key: str) -> list[dict]:
        buckets: dict[str, int] = {}
        for row in rows:
            parsed = parse_datetime(row["created_at"])
            if not parsed:
                continue
            week_start = (parsed - timedelta(days=parsed.weekday())).date().isoformat()
            buckets[week_start] = buckets.get(week_start, 0) + int(row[value_key] or 0)
        return [
            {"label": key, "value": value}
            for key, value in sorted(buckets.items(), reverse=True)[:6]
        ][::-1]

    return {
        "total_customers": total_customers,
        "reachable_customers": reachable_customers,
        "lead_capture": lead_capture,
        "segment_counts": segment_counts,
        "campaign_counts": campaign_counts,
        "lead_sources": lead_sources,
        "import_rows": import_rows,
        "lead_trend": bucket_week(
            [{"created_at": row["created_at"], "count": 1} for row in lead_rows], "count"
        ),
        "import_trend": bucket_week(import_rows, "rows_received"),
        "outreach_trend": bucket_week(outreach_rows, "audience_count"),
    }


def render_reports(message: str = "", user: dict | None = None) -> bytes:
    snapshot = reporting_snapshot()
    data_quality = snapshot["data_quality"]
    lead_source_rows = "".join(
        f"<li><strong>{escape(touchpoint_label(row['touchpoint_type']))}</strong><span>{row['count']} captures</span></li>"
        for row in snapshot["lead_sources"]
    ) or "<li><strong>No source data yet</strong><span>Capture activity will appear here once staff starts using the product.</span></li>"
    import_rows = "".join(
        f"<tr><td>{escape(display_timestamp(row['created_at'], include_time=False))}</td><td>{row['rows_received']}</td><td>{row['customers_created']}</td><td>{row['customers_updated']}</td><td>{row['review_needed_rows']}</td><td>{row['skipped_rows']}</td></tr>"
        for row in snapshot["import_rows"]
    ) or "<tr><td colspan='6'>No import activity yet.</td></tr>"
    campaign_rows = "".join(
        f"<li><strong>{escape((row['status'] or '').title())}</strong><span>{row['count']} campaigns</span></li>"
        for row in snapshot["campaign_counts"]
    ) or "<li><strong>No campaigns yet</strong><span>Campaign activity will appear here once outreach starts.</span></li>"
    trend_rows = "".join(
        f"<li><strong>{escape(item['label'])}</strong><span>{item['value']} imported rows</span></li>"
        for item in snapshot["import_trend"]
    ) or "<li><strong>No import trend yet</strong><span>Weekly import history will appear here.</span></li>"
    lead_trend_rows = "".join(
        f"<li><strong>{escape(item['label'])}</strong><span>{item['value']} leads captured</span></li>"
        for item in snapshot["lead_trend"]
    ) or "<li><strong>No lead trend yet</strong><span>Weekly capture history will appear here.</span></li>"
    outreach_rows = "".join(
        f"<li><strong>{escape(item['label'])}</strong><span>{item['value']} audience touches</span></li>"
        for item in snapshot["outreach_trend"]
    ) or "<li><strong>No outreach trend yet</strong><span>Sent and exported audience activity will appear here.</span></li>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Reports</h2>
        <p>Track reachability, capture, imports, and outreach progress.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/">Dashboard</a>
        <a class="button secondary" href="/marketing">Marketing</a>
      </div>
    </section>

    <section class="stats">
      <article><span>Total customers</span><strong>{snapshot['total_customers']}</strong></article>
      <article><span>Reachability rate</span><strong>{data_quality['reachable_rate']}%</strong></article>
      <article><span>Consent rate</span><strong>{data_quality['consent_rate']}%</strong></article>
      <article><span>Campaign-ready</span><strong>{data_quality['campaign_ready']}</strong></article>
    </section>

    <section class="grid balanced-grid">
      <div class="panel">
        <h3>Lead sources</h3>
        <ul class="stacked-list compact-list">{lead_source_rows}</ul>
      </div>
      <div class="panel">
        <h3>Customer readiness</h3>
        <ul class="stacked-list compact-list">
          <li><strong>Unreachable</strong><span>{data_quality['unreachable']} records need contact capture.</span></li>
          <li><strong>Named no contact</strong><span>{data_quality['named_unreachable']} should be prioritized at checkout.</span></li>
          <li><strong>Reachable, no consent</strong><span>{data_quality['reachable_needs_consent']} need permission before export.</span></li>
          <li><strong>Duplicate contact values</strong><span>{data_quality['duplicate_contact_values']} groups need review.</span></li>
        </ul>
      </div>
    </section>

    <section class="grid balanced-grid">
      <div class="panel">
        <h3>Lead trend</h3>
        <ul class="stacked-list compact-list">{lead_trend_rows}</ul>
      </div>
      <div class="panel">
        <h3>Import trend</h3>
        <ul class="stacked-list compact-list">{trend_rows}</ul>
      </div>
    </section>

    <section class="grid balanced-grid">
      <div class="panel">
        <h3>Campaign activity</h3>
        <ul class="stacked-list compact-list">{campaign_rows}</ul>
        <ul class="stacked-list compact-list">{outreach_rows}</ul>
      </div>
      <div class="panel">
        <h3>Owner value</h3>
        <ul class="stacked-list compact-list">
          <li><strong>Reachability</strong><span>{data_quality['reachable']} customers can receive a direct message.</span></li>
          <li><strong>Consent</strong><span>{data_quality['marketing_allowed']} customers are marked marketing-allowed.</span></li>
          <li><strong>Capture progress</strong><span>{snapshot['lead_capture']['public_this_week']} public leads captured this week.</span></li>
          <li><strong>Audience size</strong><span>{data_quality['campaign_ready']} contacts are currently campaign-ready.</span></li>
        </ul>
      </div>
    </section>

    <details class="panel secondary-details">
      <summary>Recent imports</summary>
      <h3>Recent import detail</h3>
      <table>
        <thead><tr><th>Date</th><th>Rows</th><th>Created</th><th>Merged</th><th>Review</th><th>Skipped</th></tr></thead>
        <tbody>{import_rows}</tbody>
      </table>
    </details>
    """
    return base_layout("Reports", body, flash=message, active_section="reports", user=user)


def render_guide(message: str = "", user: dict | None = None) -> bytes:
    settings = get_app_settings()
    body = f"""
    <section class="page-head">
      <div>
        <h2>Guide</h2>
        <p>A lightweight weekly operating guide for Seaview staff. Start here if the product is new or if someone needs to understand the rhythm quickly.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/">Open dashboard</a>
        <a class="button secondary" href="/qr-tools">Open QR kits</a>
      </div>
    </section>

    <section class="guide-grid">
      <article class="panel guide-step">
        <span class="eyebrow">1. Start at the dashboard</span>
        <h3>Check what matters this week</h3>
        <p>Use the dashboard first. It shows the follow-ups, incomplete leads, capture performance, and data issues that need attention.</p>
        <a class="button secondary small" href="/">Open dashboard</a>
      </article>
      <article class="panel guide-step">
        <span class="eyebrow">2. Capture every reachable guest</span>
        <h3>Use staff capture and QR pages together</h3>
        <p>{escape(settings['capture_prompt'] or 'Ask every reachable guest for the best contact method before they leave.')}</p>
        <a class="button secondary small" href="/capture">Open capture</a>
      </article>
      <article class="panel guide-step">
        <span class="eyebrow">3. Run the weekly import</span>
        <h3>Refresh Clover and list data</h3>
        <p>Upload the weekly file, review the preview, and resolve any review-needed rows before exporting a segment.</p>
        <a class="button secondary small" href="/imports">Open imports</a>
      </article>
      <article class="panel guide-step">
        <span class="eyebrow">4. Clean duplicates before outreach</span>
        <h3>Keep the audience clean</h3>
        <p>Review duplicate pairs so Seaview does not send the wrong outreach or lose relationship history during a merge.</p>
        <a class="button secondary small" href="/duplicates">Open duplicate review</a>
      </article>
      <article class="panel guide-step">
        <span class="eyebrow">5. Send one focused campaign</span>
        <h3>Choose the next audience</h3>
        <p>Use recent buyers, lapsed buyers, or new signups first. Keep the weekly offer clear and time-bound.</p>
        <a class="button secondary small" href="/marketing">Open marketing</a>
      </article>
      <article class="panel guide-step">
        <span class="eyebrow">6. Review the pilot value</span>
        <h3>Show what the system is improving</h3>
        <p>Use reports to explain how many customers are reachable now, which sources are driving leads, and how data quality is improving.</p>
        <a class="button secondary small" href="/reports">Open reports</a>
      </article>
    </section>
    """
    return base_layout("Guide", body, flash=message, active_section="guide", user=user)


def render_dashboard(message: str = "", user: dict | None = None) -> bytes:
    settings = get_app_settings()
    conn = db_connection()
    try:
        segment_counts = segment_counts_with_conn(conn)
        lead_capture = lead_capture_snapshot_with_conn(conn)
        metrics = dashboard_metrics_with_conn(conn, segment_counts=segment_counts, lead_capture=lead_capture)
        capture_gap = capture_gap_with_conn(conn)
        freshline_cleanup = freshline_cleanup_with_conn(conn, limit=25)
        business_segments = business_audience_segments_with_conn(conn)
        last_task_refresh = latest_task_refresh_run_with_conn(conn)
    finally:
        conn.close()

    data_quality = metrics["data_quality"]
    duplicate_groups = metrics["duplicate_snapshot"]["candidate_groups"]
    latest_import = metrics["recent_imports"][0] if metrics["recent_imports"] else None
    records_need_cleanup = data_quality["cleanup_records"] + duplicate_groups
    total_customers = max(data_quality["total"], 0)
    unreachable = data_quality["unreachable"]

    def percent(value: int | float) -> str:
        return f"{value:.1f}%"

    blocker_options = [
        {
            "title": "No useful contact method",
            "count": unreachable,
            "body": "Add email or phone before these customers can be reached.",
            "href": "/customers?view=missing_contact",
        },
        {
            "title": "Missing consent",
            "count": data_quality["reachable_needs_consent"],
            "body": "Reachable customers still need permission before export.",
            "href": "/customers?view=needs_consent",
        },
        {
            "title": "Duplicate risk",
            "count": duplicate_groups,
            "body": "Review likely pairs before sending a campaign.",
            "href": "/duplicates",
        },
        {
            "title": "Missing names",
            "count": data_quality["missing_name"],
            "body": "Name cleanup helps staff recognize and merge records.",
            "href": "/customers?view=missing_name",
        },
    ]
    biggest_blocker = max(blocker_options, key=lambda item: item["count"]) if total_customers else {
        "title": "Import customer data",
        "count": 0,
        "body": "Upload a customer file to see what is usable.",
        "href": "/imports",
    }
    if biggest_blocker["count"] == 0 and data_quality["campaign_ready"]:
        biggest_blocker = {
            "title": "Ready to export",
            "count": data_quality["campaign_ready"],
            "body": "Build this week's list from customers with usable contact data.",
            "href": "/marketing",
        }
    if capture_gap["clover_total"]:
        biggest_blocker = {
            "title": "Clover capture gap",
            "count": capture_gap["dark_customers"],
            "body": "In-store buyers with no email or phone are the highest-value fix.",
            "href": "#capture-gap",
        }

    email_ready_count = segment_counts.get("email_ready", capture_gap.get("freshline_reachable", 0))
    text_fix_count = freshline_cleanup.get("invalid_campaign_phone_total", freshline_cleanup.get("invalid_phone_total", 0))
    invalid_phone_total = freshline_cleanup.get("invalid_phone_total", text_fix_count)
    duplicate_total = freshline_cleanup.get("duplicate_total", duplicate_groups)
    dashboard_actions = [
        {
            "title": f"Email your {email_ready_count:,} ready customers",
            "body": "This is the Freshline audience that can go straight into Constant Contact today.",
            "href": "/marketing/export?segment=email_ready",
            "label": "Export List",
        },
        {
            "title": f"Fix {text_fix_count:,} phone numbers to unlock text campaigns",
            "body": f"{invalid_phone_total:,} Freshline phone numbers are malformed; {text_fix_count:,} are customer records after internal exclusions.",
            "href": "/imports#cleanup",
            "label": "View List",
        },
        {
            "title": f"Resolve {duplicate_total:,} duplicate accounts",
            "body": "Same-name Freshline records with different emails need an owner decision before list cleanup is done.",
            "href": "/imports#cleanup",
            "label": "View Duplicates",
        },
        {
            "title": "At checkout: ask every in-store customer for email",
            "body": f"Goal: capture 5% of in-store traffic, or about {capture_gap['five_percent_goal']:,} new reachable customers.",
            "href": "/capture/qr/preview?location=counter",
            "label": "Print QR",
        },
        {
            "title": f"{capture_gap['clover_total']:,} in-store customers are unreachable",
            "body": f"Clover has only {capture_gap['reachable_email']:,} email-reachable customers. The opportunity is closing the checkout capture gap.",
            "href": "/marketing#audiences",
            "label": "Open Audiences",
        },
    ]

    # Build numbered action plan HTML
    action_items_html = "".join(
        f"""
        <li class="action-item">
          <span class="action-number">{str(i + 1).zfill(2)}</span>
          <div class="action-body">
            <strong>{escape(item['title'])}</strong>
            <p>{escape(item['body'])}</p>
            <a class="button small secondary" href="{escape(item['href'])}">{escape(item['label'])}</a>
          </div>
        </li>
        """
        for i, item in enumerate(dashboard_actions)
    ) or "<li class='action-item'><span class='action-number'>—</span><div class='action-body'><strong>Import customer data to see action plan</strong></div></li>"

    # Fetch recent activity (last 8 touchpoints with customer names)
    conn2 = db_connection()
    try:
        recent_touchpoints = conn2.execute(
            """
            SELECT t.touchpoint_type, t.created_at, c.first_name, c.last_name, c.email,
                   t.customer_id
            FROM touchpoints t
            JOIN customers c ON c.id = t.customer_id
            ORDER BY t.created_at DESC
            LIMIT 8
            """
        ).fetchall()
        open_task_rows = conn2.execute(
            """
            SELECT
                t.id, t.title, t.details, t.due_at, t.customer_id,
                t.priority, t.priority_score, t.source, t.related_metric,
                c.first_name, c.last_name
            FROM tasks t
            LEFT JOIN customers c ON c.id = t.customer_id
            WHERE t.status = 'open'
            ORDER BY
                COALESCE(t.priority_score, 50) DESC,
                CASE WHEN t.due_at IS NULL OR t.due_at = '' THEN 1 ELSE 0 END,
                t.due_at ASC,
                t.created_at DESC
            LIMIT 5
            """
        ).fetchall()
    finally:
        conn2.close()

    def time_ago(ts: str) -> str:
        if not ts:
            return ""
        try:
            parsed = parse_datetime(ts)
            if not parsed:
                return ts[:10]
            delta = datetime.now(UTC) - parsed
            if delta.days == 0:
                hours = delta.seconds // 3600
                if hours == 0:
                    mins = delta.seconds // 60
                    return f"{mins}m ago" if mins else "just now"
                return f"{hours}h ago"
            if delta.days == 1:
                return "yesterday"
            if delta.days < 7:
                return f"{delta.days}d ago"
            return ts[:10]
        except Exception:
            return ts[:10]

    activity_feed_html = "".join(
        f"""
        <li>
          <strong>{escape(display_name(row))}</strong>
          &middot; <span>{escape(touchpoint_label(row['touchpoint_type']))}</span>
          &middot; <span class="muted">{escape(time_ago(row['created_at']))}</span>
        </li>
        """
        for row in recent_touchpoints
    ) or "<li class='muted'>No touchpoints recorded yet.</li>"

    open_tasks_html = "".join(
        f"""
        <li class="action-item">
          <span class="priority-badge priority-{escape(task_row_value(row, 'priority', 'medium') or 'medium')}">{escape((task_row_value(row, 'priority', 'medium') or 'medium').title())}</span>
          <div class="action-body">
            <strong>{escape(row['title'])}</strong>
            <p>{escape(row['details'] or '')}</p>
            <p class="muted">
              Score {int(task_row_value(row, 'priority_score', 50) or 50)}
              &middot; {escape(task_source_label(task_row_value(row, 'source', 'manual')))}
              {('&middot; ' + escape(task_row_value(row, 'related_metric', ''))) if task_row_value(row, 'related_metric', '') else ''}
            </p>
            {"<p>" + escape(display_name(row)) + "</p>" if row['first_name'] or row['last_name'] else ""}
            {"<p class='muted'>" + escape(display_timestamp(row['due_at'], include_time=False)) + "</p>" if row['due_at'] else ""}
          </div>
        </li>
        """
        for row in open_task_rows
    ) or "<li class='action-item'><span class='action-number'>&#10003;</span><div class='action-body'><strong>No open tasks</strong></div></li>"

    capture_gap_count = capture_gap["dark_customers"] if capture_gap["clover_total"] else unreachable
    capture_gap_note = (
        f"{capture_gap['capture_rate']:.1f}% email captured"
        if capture_gap["clover_total"]
        else "import customer data to measure"
    )
    freshline_reachable = capture_gap.get("freshline_campaign_ready", data_quality.get("campaign_ready", 0))
    what_changed = (
        f"Latest import: {latest_import['customers_created']} created, {latest_import['customers_updated']} updated, "
        f"{latest_import['review_needed_rows'] or 0} sent to duplicate review."
        if latest_import
        else "No import has been confirmed yet. Start with a customer export so the dashboard has real context."
    )
    what_matters = (
        f"{freshline_reachable:,} customers are campaign-ready, {unreachable:,} need contact info, "
        f"and {duplicate_groups:,} duplicate group{'s' if duplicate_groups != 1 else ''} need review."
    )
    what_next = (
        open_task_rows[0]["title"]
        if open_task_rows
        else "Import fresh customer data or capture new leads to generate the next operating task."
    )
    refresh_line = (
        f"Task brain refreshed {display_timestamp(last_task_refresh['created_at'])} from {last_task_refresh['trigger_event']}."
        if last_task_refresh
        else "Task recommendations have not been refreshed yet."
    )
    if ai_is_configured(get_setting):
        ai_dashboard_html = """
        <section class="panel ai-panel">
          <div class="panel-head">
            <div>
              <h3>AI Weekly Brief</h3>
              <p class="muted">Turn today&rsquo;s CRM numbers into a manager-style task brief.</p>
            </div>
            <form method="post" action="/ai/weekly-brief" class="inline-form">
              <button type="submit">Generate brief</button>
            </form>
          </div>
        </section>
        """
    elif user and user.get("role") == "admin":
        ai_dashboard_html = """
        <section class="panel ai-panel">
          <h3>AI Weekly Brief</h3>
          <p class="muted">Add your OpenAI API key in Admin Settings to generate a live weekly brief from CRM metrics.</p>
          <a class="button secondary small" href="/admin">Configure AI</a>
        </section>
        """
    else:
        ai_dashboard_html = ""

    body = f"""
    <section class="page-head">
      <div>
        <h2>Dashboard</h2>
        <p>{escape(settings['business_name'] or 'Seaview CRM')} &mdash; {escape(settings['primary_location'] or 'Wilmington, NC')}</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/imports">Import data</a>
        <a class="button" href="/marketing">Build campaign</a>
      </div>
    </section>

    <!-- ZONE 1: 4 stat tiles -->
    <section class="stats">
      <article>
        <span>Total Customers</span>
        <strong>{metrics['customers']:,}</strong>
      </article>
      <article>
        <span>Reachable (Freshline)</span>
        <strong>{freshline_reachable:,}</strong>
      </article>
      <article class="highlight-tile">
        <span>Capture Gap</span>
        <strong>{capture_gap_count:,}</strong>
        <small class="stat-sub">{escape(capture_gap_note)}</small>
      </article>
      <article>
        <span>Open Tasks</span>
        <strong>{metrics.get('open_tasks', 0)}</strong>
      </article>
    </section>

    <section class="panel operating-brief">
      <span class="eyebrow">CRM Operating Brain</span>
      <div class="operating-brief-grid">
        <div class="operating-brief-card">
          <strong>What changed</strong>
          <p>{escape(what_changed)}</p>
        </div>
        <div class="operating-brief-card">
          <strong>What matters</strong>
          <p>{escape(what_matters)}</p>
        </div>
        <div class="operating-brief-card">
          <strong>What to do next</strong>
          <p>{escape(what_next)}</p>
        </div>
      </div>
      <p class="muted task-refresh-details">{escape(refresh_line)}</p>
    </section>

    <!-- ZONE 2 + 3: Action plan + Activity feed -->
    <section class="dash-grid">
      <div class="panel">
        <h3>This Week&rsquo;s Action Plan</h3>
        <ol class="action-plan-list">{action_items_html}</ol>
      </div>
      <div class="panel">
        <h3>Recent Activity</h3>
        <ul class="activity-feed">{activity_feed_html}</ul>
      </div>
    </section>

    <!-- Open tasks compact list -->
    <section class="panel">
      <div class="panel-head">
        <h3>Open Tasks</h3>
        <div class="button-row">
          <form method="post" action="/tasks/refresh" class="inline-form">
            <button type="submit" class="button secondary small">Refresh Task Recommendations</button>
          </form>
          <a class="button secondary small" href="/tasks">All tasks</a>
        </div>
      </div>
      <ol class="action-plan-list">{open_tasks_html}</ol>
    </section>
    {ai_dashboard_html}
    """
    return base_layout("Seaview CRM Dashboard", body, flash=message, active_section="dashboard", user=user)


def task_source_label(value: str | None) -> str:
    return {
        "ai": "AI",
        "rule": "Rule-based",
        "manual": "Manual",
    }.get((value or "manual").lower(), "Manual")


def task_row_value(row: sqlite3.Row, key: str, default=None):
    return row[key] if key in row.keys() else default


def task_refresh_message(summary: dict) -> str:
    if summary.get("used_ai"):
        return "Task recommendations refreshed using latest CRM context."
    if summary.get("error_message"):
        return "AI recommendations were unavailable, so rule-based task recommendations were used."
    return "Task recommendations refreshed using rule-based logic because AI is not configured."


def render_tasks(message: str = "", user: dict | None = None, filter_key: str = "all") -> bytes:
    conn = db_connection()
    try:
        counts = task_counts_with_conn(conn)
        source_filter = filter_key if filter_key in {"manual", "ai", "rule"} else None
        status_filter = "completed" if filter_key == "completed" else "all"
        open_rows = list_tasks_with_conn(conn, status=status_filter, limit=80, source=source_filter)
        if filter_key == "high":
            open_rows = [row for row in open_rows if task_row_value(row, "priority", "medium") == "high"]
        completed_rows = list_tasks_with_conn(conn, status="completed", limit=8)
        last_refresh = latest_task_refresh_run_with_conn(conn)
    finally:
        conn.close()

    open_tasks_html = "".join(
        f"""
        <tr class="task-row task-priority-{escape(task_row_value(row, 'priority', 'medium') or 'medium')}">
          <td>
            <span class="priority-badge priority-{escape(task_row_value(row, 'priority', 'medium') or 'medium')}">{escape((task_row_value(row, 'priority', 'medium') or 'medium').title())}</span>
            <span class="score-badge">{int(task_row_value(row, 'priority_score', 50) or 50)}</span>
          </td>
          <td>
            <strong>{escape(row['title'])}</strong>
            <div class="muted">{escape(row['details'] or '')}</div>
            {f"<div class='task-reason'>Why: {escape(task_row_value(row, 'ai_reason', ''))}</div>" if task_row_value(row, 'ai_reason', '') else ""}
            {f"<div class='task-metric'>Metric: {escape(task_row_value(row, 'related_metric', ''))}</div>" if task_row_value(row, 'related_metric', '') else ""}
          </td>
          <td>{escape(task_type_label(row['task_type']))}</td>
          <td>{"<a href='/customers/%s'>%s</a>" % (row['customer_id'], escape(display_name(row))) if row['customer_id'] else 'General'}</td>
          <td><span class="source-pill source-{escape(task_row_value(row, 'source', 'manual') or 'manual')}">{escape(task_source_label(task_row_value(row, 'source', 'manual')))}</span></td>
          <td>{escape((row['status'] or 'open').title())}</td>
          <td>{escape(display_timestamp(row['due_at'], include_time=False) if row['due_at'] else 'No due date')}</td>
          <td>
            {""
            if row["status"] == "completed"
            else f'''
            <form method="post" action="/tasks/complete">
              <input type="hidden" name="task_id" value="{row['id']}">
              <button type="submit" class="button secondary small">Complete</button>
            </form>
            '''}
          </td>
        </tr>
        """
        for row in open_rows
    ) or "<tr><td colspan='8'>No tasks match this view.</td></tr>"

    completed_tasks_html = "".join(
        f"<li><strong>{escape(row['title'])}</strong><span>{escape(display_timestamp(row['completed_at']))}</span></li>"
        for row in completed_rows
    ) or "<li>No completed tasks yet.</li>"
    filter_options = [
        ("all", "All"),
        ("high", "High priority"),
        ("ai", "AI recommended"),
        ("rule", "Rule-based"),
        ("manual", "Manual"),
        ("completed", "Completed"),
    ]
    filter_links = "".join(
        f"<a class='filter-pill {'active' if filter_key == key else ''}' href='/tasks?filter={key}'>{label}</a>"
        for key, label in filter_options
    )
    last_refresh_html = ""
    if last_refresh:
        mode = "AI" if last_refresh["used_ai"] else "Rule fallback"
        error = f"<dd>{escape(last_refresh['error_message'])}</dd>" if last_refresh["error_message"] else "<dd>None</dd>"
        last_refresh_html = f"""
        <dl class="details task-refresh-details">
          <dt>Last refresh</dt><dd>{escape(display_timestamp(last_refresh['created_at']))}</dd>
          <dt>Trigger</dt><dd>{escape(last_refresh['trigger_event'])}</dd>
          <dt>Mode</dt><dd>{escape(mode)}</dd>
          <dt>Created / updated</dt><dd>{last_refresh['tasks_created']} / {last_refresh['tasks_updated']}</dd>
          <dt>Error</dt>{error}
        </dl>
        """

    body = f"""
    <section class="page-head">
      <div>
        <h2>Tasks</h2>
        <p>Prioritized CRM operations work based on the latest customer, capture, import, and campaign context.</p>
      </div>
      <form method="post" action="/tasks/refresh">
        <button type="submit">Refresh Task Recommendations</button>
      </form>
    </section>

    <section class="stats">
      <article><span>Open tasks</span><strong>{counts['open']}</strong></article>
      <article><span>Completed</span><strong>{counts['completed']}</strong></article>
    </section>
    <section class="panel task-refresh-panel">
      <div class="panel-head">
        <div>
          <h3>Recommendation refresh</h3>
          <p class="muted">Uses compact CRM metrics. Manual tasks are never overwritten.</p>
        </div>
      </div>
      {last_refresh_html or "<p class='muted'>No task recommendation refresh has run yet.</p>"}
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
      <div class="panel-head">
        <h3>Task list</h3>
        <div class="filter-row">{filter_links}</div>
      </div>
      <div class="scrollable-table task-table-wrap">
        <table class="task-table">
          <thead><tr><th>Priority</th><th>Task</th><th>Type</th><th>Customer</th><th>Source</th><th>Status</th><th>Due</th><th></th></tr></thead>
          <tbody>{open_tasks_html}</tbody>
        </table>
      </div>
    </div>
    """
    return base_layout("Tasks", body, flash=message, active_section="tasks", user=user)


def _source_label_short(row: sqlite3.Row) -> str:
    src = (row["acquisition_source"] or row["source_system"] or "").lower()
    if "clover" in src:
        return "Clover"
    if "freshline" in src:
        return "Freshline"
    if "qr" in src or "in_store_qr" in src or "receipt_qr" in src:
        return "QR Capture"
    if "touchpoint" in src or "website_homepage" in src or "counter" in src or "event_booth" in src:
        return "Capture"
    if "manual" in src:
        return "Manual"
    if "constant_contact" in src or "newsletter" in src:
        return "Newsletter"
    if "shopify" in src or "legacy" in src:
        return "Import"
    return (row["acquisition_source"] or row["source_system"] or "—")[:20]


def render_customers(
    search: str = "",
    review_mode: str = "",
    filter_key: str = "",
    user: dict | None = None,
) -> bytes:
    if review_mode == "duplicates":
        return render_duplicate_review(user=user)
    rows = list_customers(search, review_mode=review_mode, filter_key=filter_key)
    results_label = f"{len(rows)} record{'s' if len(rows) != 1 else ''}"
    quick_filters = [
        ("", "All"),
        ("email_ready", "Email-Ready"),
        ("needs_attention", "Needs Attention"),
        ("vip", "VIP"),
        ("lapsed", "Lapsed"),
        ("recent_buyers", "Recent Buyers"),
    ]
    filter_links = "".join(
        f"<a class='filter-link' href='/customers{('?filter=' + escape(key)) if key else ''}'>{escape(label)}</a>"
        for key, label in quick_filters
    )
    customer_rows_list = []
    for row in rows:
        contact = row["email"] or row["phone"] or "Needs contact"
        contact_class = "muted" if not row["email"] and not row["phone"] else ""
        tags_short = (row["tags"] or "")[:40]
        customer_rows_list.append(
            "<tr>"
            f"<td><a href='/customers/{row['id']}'><strong>{escape(display_name(row))}</strong></a></td>"
            f"<td class='{contact_class}'>{escape(contact)}</td>"
            f"<td><span class='muted'>{escape(row['phone'] or '')}</span></td>"
            f"<td><span class='pill muted-pill' style='font-size:0.78rem'>{escape(_source_label_short(row))}</span></td>"
            f"<td class='muted' style='max-width:140px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis'>{escape(tags_short)}</td>"
            f"<td><a class='button secondary small' href='/customers/{row['id']}'>Open</a></td>"
            "</tr>"
        )
    customer_rows = "".join(customer_rows_list) or "<tr><td colspan='6'>No customers found.</td></tr>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Customers</h2>
        <div class="pill-row"><span class="pill muted-pill">{results_label}</span></div>
      </div>
      <div class="button-row">
        <form method="get" class="search inline-search">
          <input type="text" name="q" value="{escape(search)}" placeholder="Search customers">
          <button type="submit">Search</button>
        </form>
        <a class="button" href="/customers/new">Add customer</a>
      </div>
    </section>
    <div class="filter-bar-row">{filter_links}</div>
    <div class="panel">
      <div class="scrollable-table">
        <table>
          <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Source</th><th>Tags</th><th></th></tr></thead>
          <tbody>{customer_rows}</tbody>
        </table>
      </div>
    </div>
    """
    return base_layout("Customers", body, active_section="customers", user=user)


def render_customer_form(
    message: str = "",
    customer: sqlite3.Row | None = None,
    user: dict | None = None,
) -> bytes:
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
    return base_layout(title, body, flash=message, active_section="customers", user=user)


def render_customer_detail(
    customer_id: int,
    message: str = "",
    user: dict | None = None,
) -> bytes:
    conn = db_connection()
    try:
        customer, events, touchpoints = get_customer_with_conn(conn, customer_id)
        if not customer:
            return base_layout(
                "Customer Not Found",
                "<div class='panel'><h2>Customer not found</h2></div>",
                flash=message,
                active_section="customers",
                user=user,
            )
        timeline, tasks, notes = customer_timeline_with_conn(conn, customer, events, touchpoints)
        open_tasks = [row for row in tasks if row["status"] == "open"]
        record_health = customer_record_health_with_conn(conn, customer, touchpoints, len(open_tasks))
    finally:
        conn.close()

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

    next_actions = customer_next_actions(customer, len(open_tasks), touchpoints)
    primary_next_action = next_actions[0] if next_actions else "No urgent next step."
    record_health_rows = "".join(
        f"<li><strong>{escape(item['label'])}</strong><span>{escape(item['value'])}</span></li>"
        for item in record_health[:5]
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
        <p>{escape(customer['email'] or 'No email')} · {escape(customer['phone'] or 'No phone')}</p>
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
      <article><span>Reachability</span><strong>{'Reachable' if customer['email'] or customer['phone'] else 'Missing'}</strong></article>
      <article><span>Open tasks</span><strong>{len(open_tasks)}</strong></article>
    </section>
    <section class="grid customer-grid profile-workspace">
      <div class="panel timeline-panel">
        <div class="panel-head">
          <div>
            <h3>Timeline</h3>
          </div>
        </div>
        <ul class="timeline-list">{timeline_rows}</ul>
      </div>
      <div class="panel">
        <h3>Next step</h3>
        <p>{escape(primary_next_action)}</p>
        <form method="post" action="/tasks" class="stack compact-form">
          <input type="hidden" name="customer_id" value="{customer['id']}">
          <input type="hidden" name="task_type" value="follow_up">
          <label>Follow-up task
            <input type="text" name="title" placeholder="Next follow-up">
          </label>
          <button type="submit">Create task</button>
        </form>
      </div>
      <div class="panel">
        <h3>Profile</h3>
        <dl class="details">
          <dt>Type</dt><dd>{escape(customer_account_type(customer))}</dd>
          <dt>Source</dt><dd>{escape(acquisition_label(customer['acquisition_source']))}</dd>
          <dt>Customer since</dt><dd>{escape(display_timestamp(customer['customer_since'], include_time=False) if customer['customer_since'] else 'Unknown')}</dd>
          <dt>Channel</dt><dd>{escape(channel_label(customer['preferred_channel']) or 'Unknown')}</dd>
          <dt>Consent</dt><dd>{yes_no(customer['marketing_consent'])}</dd>
          <dt>Location</dt><dd>{escape(' '.join(filter(None, [customer['city'], customer['state']])) or 'Unknown')}</dd>
          <dt>Tags</dt><dd>{escape(customer['tags'] or 'None yet')}</dd>
        </dl>
      </div>
      <div class="panel">
        <h3>Tasks</h3>
        <ul class="stacked-list">{task_rows}</ul>
      </div>
    </section>
    <details class="panel secondary-details">
      <summary>Notes and record health</summary>
      <section class="grid balanced-grid nested-grid">
        <div>
          <h3>Notes</h3>
          <ul class="stacked-list compact-list">{note_rows}</ul>
          <form method="post" action="/customers/{customer['id']}/notes" class="stack compact-form">
            <label>Add note
              <textarea name="body" rows="3" placeholder="Add staff context"></textarea>
            </label>
            <button type="submit">Save note</button>
          </form>
        </div>
        <div>
          <h3>Record health</h3>
          <ul class="stacked-list compact-list">{record_health_rows}</ul>
        </div>
      </section>
    </details>
    """
    return base_layout(display_name(customer), body, flash=message, active_section="customers", user=user)


def render_marketing(message: str = "", user: dict | None = None) -> bytes:
    conn = db_connection()
    try:
        segments = segment_definitions()
        segment_counts = segment_counts_with_conn(conn, segments)
        lead_capture = lead_capture_snapshot_with_conn(conn)
        export_block_message = None
        snapshot = marketing_snapshot_with_conn(
            conn,
            segments=segments,
            segment_counts=segment_counts,
            lead_capture=lead_capture,
        )
        capture_gap = capture_gap_with_conn(conn)
        results = results_snapshot_with_conn(conn)
    finally:
        conn.close()

    def results_capture_label(raw_value: str | None) -> str:
        mapping = {
            "counter": "Front Counter",
            "receipt": "Receipt / Bag",
            "table": "Table Tent",
            "event": "Event Booth",
            "in_store_qr": "In-Store QR",
            "website_homepage": "Website Signup",
            "wholesale_inquiry": "Wholesale",
            "receipt_qr": "Receipt QR",
        }
        cleaned = (raw_value or "").strip()
        return mapping.get(cleaned, cleaned.replace("_", " ").title() or "Unknown")

    campaign_status_counts = {row["status"]: row["count"] for row in snapshot["campaign_totals"]}
    business_segment_rows = [segment for segment in snapshot["segments"] if segment["key"] in BUSINESS_SEGMENT_KEYS]
    segment_rows = "".join(
        f"<tr><td><strong>{escape(segment['label'])}</strong></td><td>{segment['count']}</td><td>{escape(segment['recommended_channel'])}</td><td><a class='button secondary small' href='/marketing/export/preview?segment={escape(segment['key'])}'>Preview export</a></td></tr>"
        for segment in business_segment_rows
    )
    named_segment_keys = [
        "email_ready",
        "sms_ready",
        "clean_campaign_ready",
        "recent_buyers",
        "lapsed_buyers",
        "vip_customers",
        "newsletter_prospects",
        "wholesale_accounts",
        "new_signups",
    ]
    named_segment_rows = "".join(
        "<tr>"
        f"<td><strong>{escape(segment['label'])}</strong><div class='muted'>{escape(segment['description'])}</div></td>"
        f"<td>{segment['count']:,}</td>"
        f"<td>{escape(segment['recommended_channel'])}</td>"
        f"<td><a class='button secondary small' href='/marketing/export/preview?segment={escape(segment['key'])}'>Preview CSV</a></td>"
        "</tr>"
        for segment in snapshot["segments"]
        if segment["key"] in named_segment_keys
    )
    capture_rows = "".join(
        f"<li><strong>{escape(source['label'])}</strong><span>{source['count']:,} captured</span></li>"
        for source in snapshot["lead_capture"]["sources"][:6]
    ) or "<li><strong>Capture links are ready</strong><span>Use QR and public signup pages to start measuring new contacts.</span></li>"
    qr_location_rows = "".join(
        f"<span>{escape(QR_LOCATIONS[key]['label'])}: {snapshot['lead_capture'].get('qr_by_location', {}).get(key, 0):,}</span>"
        for key in ("counter", "receipt", "table", "event")
        if key in QR_LOCATIONS
    )
    file_growth_svg = render_file_growth_chart(results["weekly_growth"])
    qr_leaderboard_svg = render_qr_leaderboard_chart(results["qr_leaderboard"])
    source_donut_svg = render_source_donut_chart(results["source_breakdown"])
    top_capture_source = results["top_capture_source"]
    top_capture_label = results_capture_label(top_capture_source["location"]) if top_capture_source else "No capture source yet"
    top_capture_total = int(top_capture_source["total_captures"] or 0) if top_capture_source else 0
    if results["wow_change"] > 0:
        wow_tone = "positive"
        wow_label = f"+{results['wow_change']:,} this week vs last week"
    elif results["wow_change"] < 0:
        wow_tone = "negative"
        wow_label = f"{results['wow_change']:,} this week vs last week"
    else:
        wow_tone = "neutral"
        wow_label = "\u2192 this week vs last week"
    attribution_rows = "".join(
        f"""
        <div class="attribution-row">
          <div class="attribution-meta">
            <strong>{escape(row['title'])}</strong>
            <span class="muted">{escape(channel_label(row['channel']))} &middot; {escape(display_timestamp(row['scheduled_for'], include_time=False) if row['scheduled_for'] else 'No date')}</span>
          </div>
          <div class="attribution-stats">
            <span class="attribution-number">{row['attributed_returns']:,}</span>
            <span class="muted">returned</span>
            <span class="attribution-rate">{row['return_rate_pct']:.1f}%</span>
          </div>
          <div class="attribution-bar">
            <div class="attribution-fill" style="width: {max(0.0, min(float(row['return_rate_pct'] or 0.0), 100.0)):.1f}%"></div>
          </div>
        </div>
        """
        for row in results["attribution_results"]
    ) or (
        "<p class='muted'>No sent campaigns yet. Attribution data will appear here after campaigns are marked sent and new imports are confirmed.</p>"
    )
    campaign_rows_list = []
    for row in snapshot["recent_campaigns"]:
        export_query = urlencode({"segment": row["target_segment"], "campaign_id": str(row["id"])})
        actions = [
            f"<a class='button secondary small' href='/marketing/export/preview?{escape(export_query)}'>Preview export</a>"
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
            f"<td><strong>{escape(row['title'])}</strong></td>"
            f"<td>{escape(segments.get(row['target_segment'], {}).get('label', row['target_segment']))}</td>"
            f"<td>{row['audience_count']}</td>"
            f"<td>{status_pill(row['status'])}</td>"
            f"<td><div class='table-actions'>{''.join(actions)}</div></td>"
            "</tr>"
        )
    campaign_rows = "".join(campaign_rows_list) or "<tr><td colspan='5'>No campaigns yet.</td></tr>"
    outreach_rows = "".join(
        "<tr>"
        f"<td>{status_pill(row['event_type'], prefix='event')}</td>"
        f"<td><strong>{escape(row['title'])}</strong></td>"
        f"<td>{escape(segments.get(row['segment_key'], {}).get('label', row['segment_key'] or '—'))}</td>"
        f"<td>{escape(display_timestamp(row['created_at']))}</td>"
        "</tr>"
        for row in snapshot["recent_outreach"]
    ) or "<tr><td colspan='4'>No outreach history yet.</td></tr>"
    if ai_is_configured(get_setting):
        ai_campaign_panel = f"""
        <div class="panel ai-panel" style="margin-bottom:1rem">
          <div class="panel-head">
            <div>
              <h3>AI campaign draft</h3>
              <p class="muted">Generate a draft using the selected audience count and current CRM context.</p>
            </div>
          </div>
          <form method="post" action="/marketing/ai-campaign" class="stack">
            <div class="field-grid">
              <label>Audience
                <select name="target_segment">{option_list([(key, segments[key]['label']) for key in BUSINESS_SEGMENT_KEYS], 'clean_campaign_ready')}</select>
              </label>
              <label>Channel
                <select name="channel">{option_list(CAMPAIGN_CHANNELS, 'email')}</select>
              </label>
            </div>
            <label>This week&rsquo;s special or goal
              <input type="text" name="campaign_special" placeholder="Weekend crab special, newsletter signup, repeat visits">
            </label>
            <button type="submit" class="secondary">Generate draft campaign</button>
          </form>
        </div>
        """
    elif user and user.get("role") == "admin":
        ai_campaign_panel = """
        <div class="panel ai-panel" style="margin-bottom:1rem">
          <h3>AI campaign draft</h3>
          <p class="muted">Configure your OpenAI API key to generate campaign drafts from CRM audiences.</p>
          <a class="button secondary small" href="/admin">Configure AI</a>
        </div>
        """
    else:
        ai_campaign_panel = ""
    body = f"""
    <section class="page-head">
      <div>
        <h2>Marketing</h2>
        <p>Use Freshline for outreach. Clover is the capture gap to close at checkout.</p>
      </div>
      <div class="button-row">
        <a class="button" href="/capture">Capture lead</a>
      </div>
    </section>
    {"<div class='panel warning-panel'><strong>Export gate active</strong><p>" + escape(export_block_message) + "</p><a class='button secondary small' href='/duplicates'>Review duplicates</a></div>" if export_block_message else ""}

    <div class="tab-bar">
      <button class="tab-pill" data-tab-trigger="campaigns">Campaigns</button>
      <button class="tab-pill" data-tab-trigger="audiences">Audiences</button>
      <button class="tab-pill" data-tab-trigger="results">Results</button>
    </div>

    <!-- CAMPAIGNS TAB -->
    <div data-tab-panel="campaigns">
      {ai_campaign_panel}
      <section class="grid balanced-grid" style="margin-bottom:1rem">
        <div class="panel">
          <h3>New campaign</h3>
          <form method="post" action="/marketing/campaigns" class="stack">
            <label>Title
              <input type="text" name="title" placeholder="Weekend special">
            </label>
            <label>Offer
              <textarea name="offer_details" rows="3" placeholder="Deal, hook, and CTA"></textarea>
            </label>
            <div class="field-grid">
              <label>Channel
                <select name="channel">{option_list(CAMPAIGN_CHANNELS, 'email')}</select>
              </label>
              <label>Audience
                <select name="target_segment">{option_list([(key, segments[key]['label']) for key in BUSINESS_SEGMENT_KEYS], 'clean_campaign_ready')}</select>
              </label>
            </div>
            <div class="field-grid">
              <label>Goal
                <input type="text" name="goal" placeholder="Repeat visits">
              </label>
              <label>Schedule
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
          <h3>Recent campaigns</h3>
          <div class="scrollable-table">
            <table>
              <thead><tr><th>Campaign</th><th>Audience</th><th>Count</th><th>Status</th><th></th></tr></thead>
              <tbody>{campaign_rows}</tbody>
            </table>
          </div>
        </div>
      </section>
      <div class="panel" style="margin-bottom:1.5rem">
        <div class="panel-head" style="margin-bottom:0.75rem">
          <h3 style="margin:0">Outreach history</h3>
          <button class="button secondary small" data-collapsible-trigger="outreach-history-panel" data-label-open="Hide history" data-label-closed="Show history">Show history</button>
        </div>
        <div id="outreach-history-panel" class="collapsible-content">
          <table>
            <thead><tr><th>Activity</th><th>Campaign</th><th>Audience</th><th>When</th></tr></thead>
            <tbody>{outreach_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- AUDIENCES TAB -->
    <div data-tab-panel="audiences">
      <!-- Business purpose: show who Seaview can market to today before any campaign work starts. -->
      <section class="stats" style="margin-bottom:1.5rem">
        <article><span>Ready to email</span><strong>{snapshot['segment_counts'].get('email_ready', 0):,}</strong></article>
        <article><span>Ready to text</span><strong>{snapshot['segment_counts'].get('sms_ready', 0):,}</strong></article>
        <article><span>Clean campaign-ready</span><strong>{snapshot['segment_counts'].get('clean_campaign_ready', 0):,}</strong></article>
        <article class="highlight-tile"><span>Needs attention</span><strong>{snapshot['segment_counts'].get('needs_attention', 0):,}</strong></article>
      </section>
      <div class="panel" style="margin-bottom:1rem">
        <h3>Freshline audience segments</h3>
        <div class="scrollable-table">
          <table>
            <thead><tr><th>Audience</th><th>Count</th><th>Channel</th><th></th></tr></thead>
            <tbody>{segment_rows}</tbody>
          </table>
        </div>
      </div>
      <!-- Business purpose: make the Clover capture gap impossible to miss before exporting audiences. -->
      <div class="capture-gap-module">
        <span class="eyebrow">Your Capture Gap</span>
        <div class="capture-gap-headline">99 out of every 100 in-store customers cannot be contacted after they leave.</div>
        <div class="capture-gap-stats">
          <div class="capture-gap-stat"><span class="gap-number">{capture_gap['clover_total']:,}</span><span class="gap-label">In-store customers (Clover)</span></div>
          <div class="capture-gap-stat"><span class="gap-number">{capture_gap['reachable_email']:,}</span><span class="gap-label">Captured email ({capture_gap['capture_rate']:.1f}%)</span></div>
          <div class="capture-gap-stat"><span class="gap-number">{capture_gap['opted_no_contact']:,}</span><span class="gap-label">Opted in, no contact info</span></div>
          <div class="capture-gap-stat"><span class="gap-number">{capture_gap['dark_customers']:,}</span><span class="gap-label">Fully unreachable</span></div>
          <div class="capture-gap-stat"><span class="gap-number">{capture_gap['freshline_campaign_ready']:,}</span><span class="gap-label">Online customers ready to contact (Freshline)</span></div>
        </div>
        <div class="capture-gap-insight">
          99 out of 100 in-store customers cannot be contacted after they leave.<br>
          At 5% email capture at checkout: +{capture_gap['five_percent_goal']:,} reachable customers.<br>
          At 10%: +{capture_gap['ten_percent_goal']:,}.
        </div>
        <div class="button-row">
          <a class="button small" href="/capture">Open Capture Tools</a>
          <a class="button secondary small" href="/capture/qr/preview?location=counter" target="_blank">Print Counter QR</a>
        </div>
      </div>
      <!-- Business purpose: keep every exportable audience in one place for Constant Contact handoff. -->
      <div class="panel" style="margin-bottom:1rem">
        <h3>Named audience exports</h3>
        <div class="scrollable-table">
          <table>
            <thead><tr><th>Segment</th><th>Count</th><th>Best channel</th><th>Action</th></tr></thead>
            <tbody>{named_segment_rows}</tbody>
          </table>
        </div>
      </div>
      <!-- Business purpose: show whether new contacts are coming from QR, website, or wholesale capture. -->
      <div class="panel">
        <h3>Capture source breakdown</h3>
        <ul class="stacked-list">{capture_rows}</ul>
        <div class="qr-scan-summary">
          <strong>QR scans:</strong> {qr_location_rows or "No QR scans yet"}
        </div>
      </div>
    </div>

    <!-- RESULTS TAB -->
    <div data-tab-panel="results">
      <section class="stats results-stats" style="margin-bottom:1.5rem">
        <article>
          <span>Est. Customer Lifetime Value</span>
          <strong>${results['clv']:,.2f}</strong>
          <small class="stat-sub">Based on your business inputs below</small>
        </article>
        <article class="highlight-tile">
          <span>Value of Reachable Audience</span>
          <strong>${results['reachable_value']:,.0f}</strong>
          <small class="stat-sub">{results['campaign_ready']:,} campaign-ready customers × ${results['clv']:,.0f} CLV</small>
        </article>
        <article>
          <span>Unrealized Value (5% Capture)</span>
          <strong>${results['capture_gap_value']:,.0f}</strong>
          <small class="stat-sub">What 5% in-store email capture would add annually</small>
        </article>
        <article>
          <span>Actual Avg Order Value</span>
          <strong>${results['avg_order_value_actual']:,.2f}</strong>
          <small class="stat-sub">From purchase history in the system</small>
        </article>
      </section>

      <div class="panel" id="roi-inputs" style="margin-bottom:1rem">
        <button class="button secondary small" data-collapsible-trigger="roi-form" data-label-closed="Adjust your business inputs ▾" data-label-open="Hide inputs ▴">Adjust your business inputs ▾</button>
        <div id="roi-form" class="collapsible-content">
          <form method="post" action="/marketing/roi-settings" class="stack" style="margin-top:1rem">
            <div class="field-grid">
              <label>Average order value ($)
                <input type="number" name="avg_order_value" value="{float(results['roi']['avg_order_value']):.2f}" step="0.01" min="0">
              </label>
              <label>Average visits per year
                <input type="number" name="avg_visits_per_year" value="{float(results['roi']['avg_visits_per_year']):.1f}" step="0.1" min="0">
              </label>
            </div>
            <div class="field-grid">
              <label>Average customer lifespan (years)
                <input type="number" name="avg_customer_lifespan_years" value="{float(results['roi']['avg_customer_lifespan_years']):.1f}" step="0.5" min="0">
              </label>
            </div>
            <label>Slow season months (comma-separated month numbers)
              <input type="text" name="slow_season_months" value="{escape(results['roi']['slow_season_months'])}" placeholder="1,2,11,12">
            </label>
            <label>Peak season months
              <input type="text" name="peak_season_months" value="{escape(results['roi']['peak_season_months'])}" placeholder="5,6,7,8,9">
            </label>
            <button type="submit">Save inputs</button>
          </form>
          <p class="muted">These inputs are used to estimate customer lifetime value and the cost of your capture gap. They do not affect your actual CRM data.</p>
        </div>
      </div>

      <div class="panel chart-panel">
        <div class="chart-header">
          <div>
            <h3>Customer File Growth</h3>
            <p class="muted">New customers added each week</p>
          </div>
          <div class="chart-summary">
            <span class="wow-badge {wow_tone}">{escape(wow_label)}</span>
            <span class="muted">{results['new_this_week']:,} new this week</span>
          </div>
        </div>
        {file_growth_svg}
      </div>

      <div class="panel chart-panel">
        <div class="chart-header">
          <div>
            <h3>Capture Source Leaderboard</h3>
            <p class="muted">Where your customers are coming from</p>
          </div>
          <div class="chart-summary">
            <span class="muted">Top source: {escape(top_capture_label)}</span>
            <span class="muted">{top_capture_total:,} total captures</span>
          </div>
        </div>
        {qr_leaderboard_svg}
      </div>

      <section class="results-grid">
        <div class="panel chart-panel">
          <h3>Customer Sources</h3>
          <p class="muted">How customers entered the system</p>
          {source_donut_svg}
        </div>
        <div class="panel">
          <h3>Campaign Attribution</h3>
          <p class="muted">Customers who returned after a campaign was sent. Attribution is based on customers who appeared in a new import after a campaign was marked sent.</p>
          {attribution_rows}
        </div>
      </section>
    </div>
    """
    return base_layout("Marketing", body, flash=message, active_section="marketing", user=user)


def render_campaign_export_preview(
    segment_key: str,
    campaign_id: int | None = None,
    message: str = "",
    user: dict | None = None,
) -> bytes:
    segments = segment_definitions()
    segment = segments.get(segment_key)
    if not segment:
        return base_layout(
            "Audience Not Found",
            "<div class='panel'><h2>Audience not found</h2><p>Choose a valid marketing audience before exporting.</p></div>",
            flash=message,
            active_section="marketing",
            user=user,
        )
    conn = db_connection()
    try:
        block_message = None if segment_key in BUSINESS_SEGMENT_KEYS else campaign_export_block_message_with_conn(conn)
        audience_count = count_segment_rows_with_conn(conn, segment_key, segments)
        campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone() if campaign_id else None
    finally:
        conn.close()
    rows = fetch_segment_rows(segment_key)[:8]
    export_query = {"segment": segment_key}
    if campaign and campaign["target_segment"] == segment_key:
        export_query["campaign_id"] = str(campaign["id"])
    sample_rows = "".join(
        f"""
        <tr>
          <td>{escape(display_name(row))}</td>
          <td>{'Present' if row['email'] else '<span class="muted">Missing</span>'}</td>
          <td>{'Present' if row['phone'] else '<span class="muted">Missing</span>'}</td>
          <td>{escape(display_timestamp(row['last_purchase_at'], include_time=False) if row['last_purchase_at'] else 'No purchase yet')}</td>
        </tr>
        """
        for row in rows
    ) or "<tr><td colspan='4'>No customers match this audience yet.</td></tr>"
    campaign_note = ""
    if campaign:
        campaign_note = f"<li><strong>Campaign</strong><span>{escape(campaign['title'])}</span></li>"
    block_html = ""
    export_action = f"<a class='button' href='/marketing/export?{escape(urlencode(export_query))}'>Download CSV and log export</a>"
    if block_message:
        block_html = f"""
        <div class="panel warning-panel">
          <strong>Export blocked</strong>
          <p>{escape(block_message)}</p>
          <a class="button secondary small" href="/duplicates">Review duplicates</a>
        </div>
        """
        export_action = "<button disabled>Export blocked</button>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Export Preview</h2>
        <p>Confirm the audience before handing the CSV to Constant Contact, SMS, or the current outreach tool.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/marketing#audiences">Back to audiences</a>
        {export_action}
      </div>
    </section>
    {block_html}
    <section class="stats">
      <article><span>Audience</span><strong>{escape(segment['label'])}</strong></article>
      <article><span>Exportable rows</span><strong>{audience_count:,}</strong></article>
      <article><span>Best channel</span><strong>{escape(segment['recommended_channel'])}</strong></article>
      <article><span>Refresh behavior</span><strong>Tasks update</strong></article>
    </section>
    <section class="grid balanced-grid">
      <div class="panel">
        <h3>What this export does</h3>
        <ul class="stacked-list compact-list">
          {campaign_note}
          <li><strong>File handoff</strong><span>Downloads first name, last name, email, phone, and date added.</span></li>
          <li><strong>Audit trail</strong><span>Logs an audience exported event in outreach history.</span></li>
          <li><strong>Task brain</strong><span>Refreshes recommendations after the CSV export completes.</span></li>
          <li><strong>Guardrails</strong><span>Duplicate review can block unsafe exports when enabled.</span></li>
        </ul>
      </div>
      <div class="panel">
        <h3>Recommended next step</h3>
        <p>{escape(segment['description'])}</p>
        <p class="muted">After download, send through Seaview's current outreach tool, then return here and mark the campaign sent if this was a saved campaign.</p>
      </div>
    </section>
    <div class="panel">
      <h3>Sample rows</h3>
      <p class="muted">PII is minimized in preview. The downloaded CSV contains the full export fields.</p>
      <div class="scrollable-table">
        <table>
          <thead><tr><th>Customer</th><th>Email</th><th>Phone</th><th>Last purchase</th></tr></thead>
          <tbody>{sample_rows}</tbody>
        </table>
      </div>
    </div>
    """
    return base_layout("Export Preview", body, flash=message, active_section="marketing", user=user)


def render_imports(
    message: str = "",
    summary_id: str = "",
    user: dict | None = None,
    ai_brief: dict | None = None,
    ai_error: str = "",
    ai_saved: bool = False,
) -> bytes:
    settings = get_app_settings()
    conn = db_connection()
    recent = conn.execute("SELECT * FROM import_runs ORDER BY created_at DESC LIMIT 10").fetchall()
    active_import = conn.execute(
        """
        SELECT *
        FROM import_runs
        WHERE status IN ('queued', 'running')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    summary_row = None
    if summary_id.isdigit():
        summary_row = conn.execute("SELECT * FROM import_runs WHERE id = ?", (int(summary_id),)).fetchone()
    if not summary_row:
        summary_row = conn.execute(
            """
            SELECT *
            FROM import_runs
            WHERE COALESCE(intelligence_summary, '') <> ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    freshline_cleanup = freshline_cleanup_with_conn(conn, limit=25)
    capture_gap = capture_gap_with_conn(conn)
    conn.close()
    import_summary = import_summary_from_row(summary_row)
    saved_ai_brief = import_ai_brief_from_row(summary_row)
    if ai_brief is None and saved_ai_brief:
        ai_brief = saved_ai_brief
        ai_saved = True
    summary_id_value = str(summary_row["id"]) if summary_row else ""
    ai_saved_at = summary_row["ai_brief_created_at"] if summary_row and "ai_brief_created_at" in summary_row.keys() else ""
    ai_configured = ai_is_configured(get_setting)
    openai_model = model_from_settings(get_setting)
    openai_key_label = mask_secret(api_key_from_settings(get_setting))
    import_disabled_attr = " disabled" if active_import else ""
    active_import_banner = ""
    if active_import:
        active_import_banner = f"""
        <section class="panel warning-panel import-active-banner">
          <div>
            <strong>Import in progress</strong>
            <p>A customer file is already being cleaned, matched, and saved. Upload controls are locked until this import finishes.</p>
          </div>
          <a class="button secondary small" href="/imports/runs/{active_import['id']}">View progress</a>
        </section>
        """
    guide_rows = "".join(
        f"""
        <li>
          <div class="import-type-copy">
            <strong>{escape(guide['label'])}</strong>
            <span>{escape(guide['summary'])}</span>
          </div>
          <a class="button secondary small" href="{escape(guide['sample_file'])}" target="_blank">Sample</a>
        </li>
        """
        for guide in import_source_guides()
    )
    history_rows = "".join(
        f"""
        <tr>
          <td>{escape(display_timestamp(row['created_at'], include_time=False))}</td>
          <td>{escape(source_system_label(row['source_system']))}</td>
          <td>{row['rows_received']}</td>
          <td>{row['customers_created']}</td>
          <td>{row['customers_updated']}</td>
          <td>{row['review_needed_rows'] or 0}</td>
          <td><a href="/imports/runs/{row['id']}">{status_pill(row['status'])}</a></td>
          <td>{
              f"<a class='button secondary small' href='/imports/ai-brief?summary={row['id']}'>Open brief</a>"
              if row['ai_brief_json']
              else (
                  "<span class='muted'>Waiting for completion</span>"
                  if row['status'] in {'queued', 'running'}
                  else "<span class='muted'>Not saved</span>"
              )
          }</td>
        </tr>
        """
        for row in recent
    ) or "<tr><td colspan='8'>No imports yet.</td></tr>"
    summary_html = ""
    if import_summary:
        net = import_summary.get("net_changes", {})
        source_type = import_summary.get("source_type", "general")
        business_lines_list = import_summary.get("business_lines", import_summary.get("improvements", []))
        impact_line = business_lines_list[0] if business_lines_list else import_summary["headline"]
        support_lines = business_lines_list[1:]
        improvements = import_summary.get("improvements", [])
        still_broken = import_summary.get("still_broken", [])

        def change_tone(value: int) -> str:
            if value > 0:
                return "positive"
            if value < 0:
                return "negative"
            return "neutral"

        def change_copy(value: int) -> tuple[str, str]:
            if value > 0:
                return f"+{value:,}", "gained this upload"
            if value < 0:
                return f"{value:,}", "lost this upload"
            return "0", "unchanged this upload"

        net_rows = "".join(
            f"""
            <article class="import-intel-card {change_tone(value)}">
              <span>{escape(label)}</span>
              <strong>{change_copy(value)[0]}</strong>
              <small>{change_copy(value)[1]}</small>
            </article>
            """
            for label, value in [
                ("Total contacts", net.get("total_contacts", 0)),
                ("Reachable customers", net.get("reachable_customers", 0)),
                ("Campaign-ready customers", net.get("campaign_ready_customers", 0)),
            ]
        )

        detail_cards = []
        action_links = []
        source_detail_html = ""
        chart_rows_primary: list[dict] = []
        chart_rows_secondary: list[dict] = []
        if source_type == "clover":
            gap = import_summary["source_metrics"]
            detail_cards = [
                ("In-store customers", f"{gap['clover_total']:,}", "Customers seen in Clover history."),
                ("Captured email", f"{gap['reachable_email']:,}", f"{gap['capture_rate']:.1f}% capture rate."),
                ("Opted in, no contact", f"{gap['opted_no_contact']:,}", "Permission exists, reachability does not."),
                ("Reachable Freshline", f"{gap['freshline_reachable']:,}", "This is the usable marketing audience."),
            ]
            chart_rows_primary = [
                {"label": "In-store customers", "value": gap["clover_total"], "note": "Total Clover customer file."},
                {"label": "Captured email", "value": gap["reachable_email"], "note": "Can be emailed today."},
                {"label": "Captured phone", "value": gap["reachable_phone"], "note": "Can be texted today."},
                {"label": "Fully unreachable", "value": gap["dark_customers"], "note": "No email or phone on file."},
            ]
            chart_rows_secondary = [
                {"label": "Marketing allowed", "value": gap["marketing_allowed"], "note": "Permission is present."},
                {"label": "Opted in, no contact", "value": gap["opted_no_contact"], "note": "Permission, but no usable channel."},
                {"label": "5% checkout goal", "value": gap["five_percent_goal"], "note": "New reachable customers at 5% capture."},
                {"label": "10% checkout goal", "value": gap["ten_percent_goal"], "note": "The upside if staff capture improves."},
            ]
            action_links = [
                ("Print counter QR", "/capture/qr/preview?location=counter"),
                ("Open capture tools", "/capture"),
                ("See audiences", "/marketing#audiences"),
            ]
        elif source_type == "freshline":
            cleanup = import_summary["source_metrics"]["cleanup"]
            audience = import_summary["source_metrics"]["audience"]
            detail_cards = [
                ("Email-ready", f"{audience['email_ready']:,}", "Can be exported for email today."),
                ("Campaign-ready", f"{audience['clean_campaign_ready']:,}", "Clean and usable after exclusions."),
                ("Invalid phones", f"{cleanup['invalid_phone_total']:,}", "Excluded from text until fixed."),
                ("Duplicate groups", f"{cleanup['duplicate_total']:,}", "Need owner review before cleanup is done."),
            ]
            chart_rows_primary = [
                {"label": "Freshline total", "value": capture_gap.get("freshline_total", 0), "note": "Records in the reachable file."},
                {"label": "Email-ready", "value": audience["email_ready"], "note": "Usable for email now."},
                {"label": "Campaign-ready", "value": audience["clean_campaign_ready"], "note": "Clean after exclusions."},
                {"label": "Needs attention", "value": audience["needs_attention"], "note": "Still needs cleanup before use."},
            ]
            chart_rows_secondary = [
                {"label": "Invalid phones", "value": cleanup["invalid_phone_total"], "note": "Fix these to grow text reach."},
                {"label": "Internal excluded", "value": cleanup["internal_total"], "note": "Already removed from exports."},
                {"label": "Duplicate groups", "value": cleanup["duplicate_total"], "note": "Owner review needed."},
                {"label": "First-name-only", "value": cleanup["first_name_only_total"], "note": "Harder for staff to recognize."},
            ]
            invalid_rows = "".join(
                f"<li><div><strong>{escape(row['name'])}</strong><span>{escape(row['email'])} · {escape(row['issue'])}{' · Internal - excluded' if row.get('is_internal') else ''}</span></div><code>{escape(row['phone'] or 'Missing')}</code></li>"
                for row in cleanup["invalid_phone_records"][:6]
            ) or "<li><div><strong>No invalid phones found</strong><span>The text list is clean.</span></div></li>"
            duplicate_rows = "".join(
                f"""
                <article class="duplicate-mini">
                  <strong>{escape(group['name'])}</strong>
                  <div class="duplicate-mini-grid">
                    {''.join(f"<a href='{escape(customer['href'])}'><span>{escape(customer['email'])}</span><small>{escape(customer['phone'] or 'No phone')}</small></a>" for customer in group['customers'][:2])}
                  </div>
                </article>
                """
                for group in cleanup["duplicate_groups"][:4]
            ) or "<p class='muted'>No duplicate names found.</p>"
            source_detail_html = f"""
            <div class="grid import-source-detail">
              <div>
                <h4>Fix these phone numbers first</h4>
                <ul class="cleanup-list compact-cleanup">{invalid_rows}</ul>
              </div>
              <div>
                <h4>Duplicate names to review</h4>
                <div class="duplicate-mini-list">{duplicate_rows}</div>
              </div>
            </div>
            """
            action_links = [
                ("Open cleanup engine", "/imports#cleanup"),
                ("Export campaign-ready", "/marketing/export?segment=clean_campaign_ready"),
                ("Open marketing", "/marketing#audiences"),
            ]
        else:
            chart_rows_primary = [
                {"label": "Rows imported", "value": int(import_summary.get("records_imported") or 0), "note": "Rows processed in this upload."},
                {"label": "Customers touched", "value": int(import_summary.get("customers_touched") or 0), "note": "Existing or new contacts affected."},
                {"label": "Contacts before", "value": int(import_summary.get("records_previously_on_file") or 0), "note": "CRM size before this upload."},
                {"label": "Contacts after", "value": int(import_summary.get("records_after_import") or 0), "note": "CRM size after this upload."},
            ]
            chart_rows_secondary = [
                {"label": "Total contacts", "value": int(net.get("total_contacts") or 0), "note": "Net contact change."},
                {"label": "Reachable change", "value": abs(int(net.get("reachable_customers") or 0)), "note": "Reachability movement this upload."},
                {"label": "Campaign-ready", "value": abs(int(net.get("campaign_ready_customers") or 0)), "note": "Usable audience movement."},
            ]

        touched_line = (
            f"{import_summary['records_imported']:,} rows touched {import_summary['customers_touched']:,} customer records."
            if import_summary.get("customers_touched")
            else f"{import_summary['records_imported']:,} rows were imported."
        )
        file_totals_line = (
            f"The CRM moved from {import_summary['records_previously_on_file']:,} to {import_summary['records_after_import']:,} contacts."
            if import_summary["records_after_import"] != import_summary["records_previously_on_file"]
            else f"The CRM still holds {import_summary['records_after_import']:,} contacts after this refresh."
        )
        support_html = "".join(f"<li>{escape(line)}</li>" for line in support_lines[:3])
        improvements_html = "".join(f"<li>{escape(line)}</li>" for line in improvements[:3])
        still_broken_html = "".join(f"<li>{escape(line)}</li>" for line in still_broken[:3])
        detail_cards_html = "".join(
            f"""
            <article class="import-intel-metric">
              <span>{escape(label)}</span>
              <strong>{escape(value)}</strong>
              <small>{escape(note)}</small>
            </article>
            """
            for label, value, note in detail_cards
        )
        action_links_html = "".join(
            f"<a class='button{' secondary' if i else ''} small' href='{escape(href)}'>{escape(label)}</a>"
            for i, (label, href) in enumerate(action_links)
        )
        primary_chart_svg = render_metric_bar_chart(
            "Upload breakdown",
            chart_rows_primary,
            aria_label="Import breakdown chart",
        )
        secondary_chart_svg = render_metric_bar_chart(
            "Where the next upside sits",
            chart_rows_secondary,
            aria_label="Import action opportunity chart",
        )
        chart_notes = ai_brief.get("chart_notes") if isinstance(ai_brief, dict) and isinstance(ai_brief.get("chart_notes"), list) else []
        chart_notes_html = "".join(f"<li>{escape(str(note))}</li>" for note in chart_notes[:2])
        ai_panel_html = ""
        if ai_brief:
            takeaways = ai_brief.get("takeaways") if isinstance(ai_brief.get("takeaways"), list) else []
            actions = ai_brief.get("actions") if isinstance(ai_brief.get("actions"), list) else []
            takeaways_html = "".join(f"<li>{escape(str(item))}</li>" for item in takeaways[:3]) or "<li>No additional AI takeaways were returned.</li>"
            ai_actions_html = "".join(
                f"""
                <article class="import-ai-action">
                  <strong>{escape(str(action.get('title', 'Next move')))}</strong>
                  <p>{escape(str(action.get('reason', 'Use the latest import to decide the next step.')))}</p>
                  <span>{escape(str(action.get('cta', 'Assign this now.')))}</span>
                </article>
                """
                for action in actions[:3]
                if isinstance(action, dict)
            ) or "<p class='muted'>No AI actions were returned for this upload.</p>"
            saved_status = (
                f"Saved with import · {escape(display_timestamp(ai_saved_at, include_time=False))}"
                if ai_saved and ai_saved_at
                else "Saved with import"
                if ai_saved
                else "Preview only"
            )
            saved_note = (
                "This brief is attached to the import history and can be reopened later."
                if ai_saved
                else "This version is a live preview. Save it if you want it attached to this weekly import."
            )
            ai_panel_html = f"""
            <section class="import-intel-ai">
              <div class="import-intel-ai-head">
                <div>
                  <span class="eyebrow">AI Upload Readout</span>
                  <h4>{escape(str(ai_brief.get('headline', 'Latest upload brief')))}</h4>
                </div>
                <span class="status-pill">{saved_status}</span>
              </div>
              <p>{escape(str(ai_brief.get('summary', 'AI translated the latest import into a tighter operating brief.')))}</p>
              <p class="muted">{saved_note}</p>
              <div class="import-intel-columns">
                <div class="import-intel-section">
                  <h4>What AI sees</h4>
                  <ul class="import-intel-list">{takeaways_html}</ul>
                </div>
                <div class="import-intel-section">
                  <h4>What to do next</h4>
                  <div class="import-ai-actions">{ai_actions_html}</div>
                </div>
              </div>
              <div class="import-intel-chart-notes">
                <strong>Chart readout</strong>
                <ul class="import-intel-list compact-list">{chart_notes_html or '<li>The left chart shows file reality; the right chart shows where the next opportunity sits.</li>'}</ul>
              </div>
              <form method="post" action="/imports/ai-brief" class="button-row" data-ai-submit-form>
                <input type="hidden" name="summary_id" value="{escape(summary_id_value)}">
                <button type="submit" name="brief_mode" value="save" class="small" data-loading-label="Saving AI brief...">Generate &amp; save new brief</button>
                <button type="submit" name="brief_mode" value="preview" class="button secondary small" data-loading-label="Generating preview...">Preview only</button>
                <span class="loading-note" data-ai-loading hidden>Generating brief from the latest import...</span>
              </form>
            </section>
            """
        elif ai_configured:
            ai_panel_html = f"""
            <section class="import-intel-ai">
              <div class="import-intel-ai-head">
                <div>
                  <span class="eyebrow">AI Upload Readout</span>
                  <h4>Turn the latest upload into a sharper staff brief</h4>
                </div>
                <span class="status-pill">Ready · {escape(openai_model)}</span>
              </div>
              <p class="muted">Generate a one-screen operating readout from the latest upload. Save it to this import if you want it attached to history.</p>
              <form method="post" action="/imports/ai-brief" class="button-row" data-ai-submit-form>
                <input type="hidden" name="summary_id" value="{escape(summary_id_value)}">
                <button type="submit" name="brief_mode" value="save" data-loading-label="Saving AI brief...">Generate &amp; save AI readout</button>
                <button type="submit" name="brief_mode" value="preview" class="button secondary small" data-loading-label="Generating preview...">Preview only</button>
                <a class="button secondary small" href="/admin">AI settings</a>
                <span class="loading-note" data-ai-loading hidden>Generating brief from the latest import...</span>
              </form>
            </section>
            """
        elif user and user.get("role") == "admin":
            ai_panel_html = f"""
            <section class="import-intel-ai">
              <div class="import-intel-ai-head">
                <div>
                  <span class="eyebrow">AI Upload Readout</span>
                  <h4>Add your OpenAI key here and use AI from Imports</h4>
                </div>
                <span class="status-pill">Not configured</span>
              </div>
              <p class="muted">This reuses the same secure AI setting as Admin, but keeps setup close to the upload workflow.</p>
              <form method="post" action="/imports/ai-settings" class="stack">
                <div class="field-grid">
                  <label>OpenAI API Key
                    <input type="password" name="openai_api_key" placeholder="{escape(openai_key_label)}">
                  </label>
                  <label>Model
                    <input type="text" name="openai_model" value="{escape(openai_model)}" placeholder="gpt-4o-mini">
                  </label>
                </div>
                <div class="button-row">
                  <button type="submit">Save AI settings</button>
                  <a class="button secondary small" href="/admin">Open Admin</a>
                </div>
              </form>
            </section>
            """
        else:
            ai_panel_html = """
            <section class="import-intel-ai">
              <div class="import-intel-ai-head">
                <div>
                  <span class="eyebrow">AI Upload Readout</span>
                  <h4>AI can turn this upload into a tighter action brief</h4>
                </div>
                <span class="status-pill">Awaiting setup</span>
              </div>
              <p class="muted">Ask an admin to add the OpenAI API key in Settings, then generate the import readout from this page.</p>
            </section>
            """
        if ai_error:
            ai_panel_html += f"<p class='inline-error'>{escape(ai_error)}</p>"
        summary_html = f"""
        <!-- Business purpose: explain exactly what this upload changed and what the owner should do next. -->
        <section class="panel import-intelligence">
          <div class="import-intel-header">
            <div>
              <span class="eyebrow">Import Intelligence Summary</span>
              <h3>{escape(import_summary['headline'])}</h3>
              <p>{escape(touched_line)} {escape(file_totals_line)}</p>
            </div>
            <span class="import-intel-source">{escape(source_system_label(import_summary.get('source_system', '')))}</span>
          </div>
          <div class="import-intel-primary">
            <strong>{escape(impact_line)}</strong>
            <span>{'This upload confirms the in-store capture gap.' if source_type == 'clover' else 'This upload defines who Seaview can market to right now.' if source_type == 'freshline' else 'Use this upload to decide the next action.'}</span>
          </div>
          <div class="import-intel-card-grid">
            {net_rows}
          </div>
          <div class="import-intel-columns">
            <div class="import-intel-section">
              <h4>What matters now</h4>
              <ul class="import-intel-list">{support_html or improvements_html or "<li>No additional summary notes for this upload.</li>"}</ul>
            </div>
            <div class="import-intel-section">
              <h4>Still blocking action</h4>
              <ul class="import-intel-list">{still_broken_html or "<li>No blocking issues were returned for this upload.</li>"}</ul>
            </div>
          </div>
          <div class="import-intel-metric-grid">
            {detail_cards_html}
          </div>
          <div class="import-intel-actions">
            {action_links_html}
          </div>
          <div class="import-intel-visuals">
            <div class="import-intel-chart">
              {primary_chart_svg}
            </div>
            <div class="import-intel-chart">
              {secondary_chart_svg}
            </div>
          </div>
          {ai_panel_html}
          {source_detail_html}
        </section>
        """
    # Cleanup Engine data
    invalid_phone_rows_html = "".join(
        f"<tr><td><a href='{escape(row['href'])}'>{escape(row['name'])}</a></td><td>{escape(row['email'])}</td><td><code>{escape(row['phone'] or 'Missing')}</code></td><td>{escape(row['issue'])}</td></tr>"
        for row in freshline_cleanup["invalid_phone_records"]
    ) or "<tr><td colspan='4'>No invalid phones found.</td></tr>"

    internal_rows_html = "".join(
        f"<tr><td>{escape(row['email'])}</td><td>{escape(row['name'])}</td><td><span class='muted'>Auto-excluded</span></td></tr>"
        for row in freshline_cleanup["internal_records"]
    ) or "<tr><td colspan='3'>No internal records found.</td></tr>"

    dup_rows_html = "".join(
        f"""
        <tr>
          <td><strong>{escape(group['name'])}</strong></td>
          <td>{escape(group['customers'][0]['email'] if group['customers'] else '')}</td>
          <td>{escape(group['customers'][1]['email'] if len(group['customers']) > 1 else '')}</td>
          <td>
            <form method="post" action="/freshline/duplicates/flag" class="inline-form">
              <input type="hidden" name="primary_customer_id" value="{group['customers'][0]['id'] if group['customers'] else ''}">
              <input type="hidden" name="secondary_customer_id" value="{group['customers'][1]['id'] if len(group['customers']) > 1 else ''}">
              <input type="hidden" name="decision" value="merge_requested">
              <button type="submit" class="secondary small">Flag for Merge</button>
            </form>
          </td>
        </tr>
        """
        for group in freshline_cleanup["duplicate_groups"]
    ) or "<tr><td colspan='4'>No duplicate groups found.</td></tr>"

    body = f"""
    <section class="page-head">
      <div>
        <h2>Imports</h2>
        <p>Upload the weekly CSV or Excel file, preview changes, then confirm.</p>
      </div>
    </section>

    {summary_html}
    {active_import_banner}

    <div class="tab-bar">
      <button class="tab-pill" data-tab-trigger="upload">Upload</button>
      <button class="tab-pill" data-tab-trigger="cleanup">Cleanup Engine</button>
    </div>

    <!-- UPLOAD TAB -->
    <div data-tab-panel="upload">
      <section class="grid balanced-grid" style="margin-bottom:1rem">
        <div class="panel">
          <h3>Weekly file upload</h3>
          <form method="post" action="/imports" enctype="multipart/form-data" class="stack import-form" data-import-submit-form>
            <label>Source system
              <select name="source_system"{import_disabled_attr}>
                <option value="seaview_customer_export">Seaview customer export</option>
                <option value="freshline_customer_export">Freshline customer export</option>
                <option value="clover">Clover export</option>
                <option value="constant_contact">Constant Contact export</option>
                <option value="shopify_legacy">Legacy Shopify export</option>
                <option value="website_signup">Website signup export</option>
                <option value="legacy_csv">Other spreadsheet / manual export</option>
              </select>
            </label>
            <div class="import-upload">
              <span class="import-field-label">CSV or Excel file</span>
              <label class="import-dropzone" for="csv_file" data-dropzone tabindex="0" aria-label="Upload a CSV or Excel file by dragging and dropping or browsing">
                <input class="sr-only" id="csv_file" type="file" name="csv_file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" data-dropzone-input{import_disabled_attr}>
                <span class="import-dropzone-title">Drag and drop your file here</span>
                <span class="import-dropzone-subtitle">or click to browse from your computer</span>
                <span class="import-dropzone-actions">
                  <span class="button secondary import-dropzone-button">Choose file</span>
                  <span class="import-dropzone-filename" data-file-name>No file chosen</span>
                </span>
              </label>
            </div>
            <button type="submit" class="import-submit" data-loading-label="Preparing preview..."{import_disabled_attr}>{'Import locked while another file runs' if active_import else 'Import data'}</button>
            <span class="loading-note" data-import-loading hidden>Reading the file, mapping columns, and checking customer matches.</span>
          </form>
        </div>
        <div class="panel">
          <h3>Import rules</h3>
          <ul class="stacked-list compact-list">
            <li><strong>Primary source</strong><span>{escape(source_system_label(settings['preferred_primary_data_source'] or 'clover'))}</span></li>
            <li><strong>Owner</strong><span>{escape(settings['weekly_import_owner'] or 'Not set')}</span></li>
            <li><strong>Auto-merge</strong><span>Email, phone, or external ID</span></li>
            <li><strong>Review</strong><span>Same name and location</span></li>
          </ul>
          <details class="secondary-details" style="margin-top:1rem">
            <summary>Supported import types</summary>
            <ul class="stacked-list import-type-list">{guide_rows}</ul>
          </details>
        </div>
      </section>
      <div class="panel">
        <h3>Import history</h3>
        <div class="scrollable-table">
          <table>
            <thead><tr><th>Date</th><th>Source</th><th>Rows</th><th>Created</th><th>Merged</th><th>Review</th><th>Status</th><th>AI brief</th></tr></thead>
            <tbody>{history_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- CLEANUP ENGINE TAB -->
    <div data-tab-panel="cleanup">
      <section class="panel operating-brief" style="margin-bottom:1rem">
        <span class="eyebrow">Guided data quality workflow</span>
        <div class="operating-brief-grid">
          <div>
            <strong>1. Remove noise</strong>
            <p>Internal, staff, and test records are automatically excluded from campaign exports.</p>
          </div>
          <div>
            <strong>2. Fix reachability</strong>
            <p>Phone and contact gaps show which customers cannot be used for SMS or email yet.</p>
          </div>
          <div>
            <strong>3. Resolve trust risks</strong>
            <p>Duplicate decisions protect the customer experience before CSV handoff.</p>
          </div>
        </div>
      </section>
      <div class="panel cleanup-section" style="margin-bottom:1rem">
        <h3>Section A &mdash; Internal / Test Records (auto-excluded)</h3>
        <p class="muted">{freshline_cleanup['internal_total']:,} records are automatically excluded from all campaign exports.</p>
        <div class="scrollable-table">
          <table>
            <thead><tr><th>Email</th><th>Name</th><th>Status</th></tr></thead>
            <tbody>{internal_rows_html}</tbody>
          </table>
        </div>
      </div>
      <div class="panel cleanup-section" style="margin-bottom:1rem">
        <h3>Section B &mdash; Invalid Phone Numbers</h3>
        <p class="muted">{freshline_cleanup['invalid_phone_total']:,} phone numbers are malformed and excluded from text campaigns. Fix these in Freshline and re-import.</p>
        <div class="scrollable-table">
          <table>
            <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Issue</th></tr></thead>
            <tbody>{invalid_phone_rows_html}</tbody>
          </table>
        </div>
      </div>
      <div class="panel cleanup-section" style="margin-bottom:1rem">
        <h3>Section C &mdash; Duplicate Accounts</h3>
        <p class="muted">{freshline_cleanup['duplicate_total']:,} name-matched accounts have different emails. Review before sending campaigns.</p>
        <div class="scrollable-table">
          <table>
            <thead><tr><th>Name</th><th>Email 1</th><th>Email 2</th><th></th></tr></thead>
            <tbody>{dup_rows_html}</tbody>
          </table>
        </div>
      </div>
      <div class="panel cleanup-section">
        <h3>Section D &mdash; Capture Gap (read-only)</h3>
        <ul class="stacked-list compact-list">
          <li><strong>In-store customers (Clover)</strong><span>{capture_gap['clover_total']:,}</span></li>
          <li><strong>Captured email</strong><span>{capture_gap['reachable_email']:,}</span></li>
          <li><strong>Captured phone</strong><span>{capture_gap['reachable_phone']:,}</span></li>
          <li><strong>Marketing allowed</strong><span>{capture_gap['marketing_allowed']:,}</span></li>
          <li><strong>Completely unreachable</strong><span>{capture_gap['dark_customers']:,}</span></li>
          <li><strong>Capture rate</strong><span>{capture_gap['capture_rate']:.1f}% of in-store have email</span></li>
          <li><strong>Freshline reachable</strong><span>{capture_gap['freshline_reachable']:,}</span></li>
          <li><strong>Freshline campaign-ready</strong><span>{capture_gap['freshline_campaign_ready']:,}</span></li>
        </ul>
      </div>
    </div>
    """
    return base_layout("Imports", body, flash=message, active_section="imports", user=user)


def render_import_preview(import_id: str, message: str = "", user: dict | None = None) -> bytes:
    pending = load_pending_import(import_id, include_rows=False)
    if not pending:
        return base_layout(
            "Import Preview",
            "<div class='panel'><h2>Import preview expired</h2><p>Upload the file again to review it before importing.</p></div>",
            flash=message,
            active_section="imports",
            user=user,
        )
    analysis = pending.get("analysis")
    if not analysis:
        pending_with_rows = load_pending_import(import_id)
        if not pending_with_rows:
            return base_layout(
                "Import Preview",
                "<div class='panel'><h2>Import preview expired</h2><p>Upload the file again to review it before importing.</p></div>",
                flash=message,
                active_section="imports",
                user=user,
            )
        pending = pending_with_rows
        analysis = analyze_import_rows(pending["source_system"], pending["rows"])
    columns = pending.get("columns") or analysis["columns"]
    sample_rows = pending.get("sample_rows") or preview_rows(pending.get("rows") or [])
    rows_count = int(pending.get("rows_count") or len(pending.get("rows") or []))
    def safe_preview_value(column: str, value: str) -> str:
        cleaned = (value or "").strip()
        lowered = column.lower()
        if any(token in lowered for token in ("name", "email", "phone", "address", "customer id", "postal", "zip")):
            return "Present" if cleaned else ""
        return cleaned[:80]
    column_headers = "".join(f"<th>{escape(col)}</th>" for col in columns) or "<th>No columns detected</th>"
    sample_html = "".join(
        "<tr>" + "".join(f"<td>{escape(safe_preview_value(col, row.get(col) or ''))}</td>" for col in columns) + "</tr>"
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
    outcome_rows = "".join([
        f"<li><strong>Create</strong><span>{analysis['create_rows']} rows</span></li>",
        f"<li><strong>Merge</strong><span>{analysis['merge_rows']} rows</span></li>",
        f"<li><strong>Review</strong><span>{analysis['review_rows']} rows</span></li>",
        f"<li><strong>Skip</strong><span>{analysis['skipped_rows']} rows</span></li>",
    ])
    quality_rows = "".join([
        f"<li><strong>Usable contact</strong><span>{analysis['contactable_rows']} rows</span></li>",
        f"<li><strong>Marketing allowed</strong><span>{analysis['consent_rows']} rows</span></li>",
        f"<li><strong>Campaign-ready</strong><span>{analysis['campaign_ready_rows']} rows</span></li>",
        f"<li><strong>Named, no contact</strong><span>{analysis['named_unreachable_rows']} rows</span></li>",
        f"<li><strong>Low-context</strong><span>{analysis['anonymous_rows']} rows</span></li>",
    ])
    disabled_attr = "" if analysis["can_import"] else " disabled"
    confirm_label = "Confirm import" if analysis["can_import"] else "Import unavailable"
    body = f"""
    <section class="page-head">
      <div>
        <h2>Preview import</h2>
        <p>Check reachability, consent, and merge outcomes before changing the CRM.</p>
      </div>
      <a class="button secondary" href="/imports">Back to imports</a>
    </section>
    <section class="stats">
      <article><span>Rows</span><strong>{rows_count}</strong></article>
      <article><span>Usable contact</span><strong>{analysis['contactable_rows']}</strong></article>
      <article><span>Marketing allowed</span><strong>{analysis['consent_rows']}</strong></article>
      <article><span>Campaign-ready</span><strong>{analysis['campaign_ready_rows']}</strong></article>
    </section>
    <section class="grid balanced-grid">
      <div class="panel">
        <h3>Validation</h3>
        <dl class="details">
          <dt>Source system</dt><dd>{escape(source_system_label(pending['source_system']))}</dd>
          <dt>Filename</dt><dd>{escape(display_upload_name(pending['filename']))}</dd>
          <dt>Duplicate email groups</dt><dd>{analysis['duplicate_email_values']}</dd>
          <dt>Duplicate phone groups</dt><dd>{analysis['duplicate_phone_values']}</dd>
        </dl>
        <ul class="stacked-list compact-list validation-list">
          {validation_rows or "<li><strong>Ready to import</strong><span>The file has enough identity data to safely merge into the CRM.</span></li>"}
        </ul>
      </div>
      <div class="panel">
        <h3>Contact quality</h3>
        <ul class="stacked-list compact-list">{quality_rows}</ul>
      </div>
    </section>
    <section class="grid balanced-grid">
      <div class="panel">
        <h3>Import outcomes</h3>
        <ul class="stacked-list compact-list">{outcome_rows}</ul>
      </div>
      <div class="panel">
        <h3>Mapped fields</h3>
        <ul class="stacked-list compact-list">
          {mapped_groups}
        </ul>
      </div>
    </section>
    <section class="grid balanced-grid">
      <div class="panel">
        <h3>Unmapped columns</h3>
        <div class="pill-row">{unmapped_pills or "<span class='pill'>All detected columns are mapped.</span>"}</div>
      </div>
      <div class="panel">
        <h3>What happens on import</h3>
        <ul class="stacked-list compact-list">
          <li><strong>Auto-merge</strong><span>Exact email, normalized phone, and external ID plus source merge automatically.</span></li>
          <li><strong>Review needed</strong><span>Name-plus-location similarities are imported but routed to duplicate review instead of auto-merge.</span></li>
          <li><strong>Purchase enrichment</strong><span>Order totals, items, and dates create purchase events when those columns are present.</span></li>
          <li><strong>Worker-safe import</strong><span>Rows without enough identity data are skipped instead of creating empty customer records.</span></li>
        </ul>
      </div>
    </section>
    <div class="panel import-preview-sample-panel">
      <h3>Masked sample rows</h3>
      <p class="muted">Wide exports stay inside this preview. Scroll sideways to inspect additional columns.</p>
      <div class="scrollable-table import-preview-sample-table" role="region" aria-label="Masked sample rows">
        <table>
          <thead><tr>{column_headers}</tr></thead>
          <tbody>{sample_html}</tbody>
        </table>
      </div>
    </div>
    <div class="button-row">
      <form method="post" action="/imports/confirm" data-import-submit-form>
        <input type="hidden" name="import_id" value="{escape(import_id)}">
        <button type="submit"{disabled_attr} data-loading-label="Importing customers...">{confirm_label}</button>
        <span class="loading-note" data-import-loading hidden>Saving customers in one safe transaction. Large files may take a moment.</span>
      </form>
      <form method="post" action="/imports/cancel">
        <input type="hidden" name="import_id" value="{escape(import_id)}">
        <button type="submit" class="button secondary">Cancel</button>
      </form>
    </div>
    """
    return base_layout("Import Preview", body, flash=message, active_section="imports", user=user)


def import_run_status_payload(import_run_id: int) -> dict | None:
    row = get_import_run(import_run_id)
    if not row:
        return None
    status = row["status"]
    rows_total = int(row["rows_received"] or 0)
    rows_processed = int(row["rows_processed"] or 0)
    percent = 100 if rows_total == 0 and status == "completed" else int(min(100, (rows_processed / rows_total) * 100)) if rows_total else 0
    is_active = status in {"queued", "running"}
    progress_copy = row["progress_message"] or (
        "Import is queued." if status == "queued" else
        "Import is running." if status == "running" else
        "Import completed." if status == "completed" else
        "Import failed."
    )
    started_at = parse_datetime(row["started_at"] if "started_at" in row.keys() else "")
    completed_at = parse_datetime(row["completed_at"] if "completed_at" in row.keys() else "")
    now = datetime.now(UTC)

    def format_duration(seconds: float | None) -> str:
        if seconds is None or seconds <= 0:
            return "Calculating estimate..."
        if seconds < 45:
            return "Less than a minute"
        if seconds < 90:
            return "About 1 minute"
        if seconds < 3600:
            return f"About {round(seconds / 60)} minutes"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"About {hours} hr {minutes} min" if minutes else f"About {hours} hr"

    elapsed_seconds = None
    rows_per_second = None
    eta_text = "Calculating estimate..."
    completion_text = "Calculating estimate..."
    speed_text = "Calculating speed..."
    if started_at and rows_processed > 0:
        elapsed_seconds = max((now - started_at).total_seconds(), 1)
        rows_per_second = rows_processed / elapsed_seconds
        speed_text = f"{rows_per_second:,.0f} rows/sec" if rows_per_second >= 10 else f"{rows_per_second:.1f} rows/sec"
        if is_active and rows_total > rows_processed and rows_per_second > 0:
            remaining_seconds = (rows_total - rows_processed) / rows_per_second
            eta_text = format_duration(remaining_seconds)
            completion_text = (now + timedelta(seconds=remaining_seconds)).strftime("%I:%M %p").lstrip("0")
        elif status == "completed":
            eta_text = "Finished"
            completion_text = completed_at.strftime("%I:%M %p").lstrip("0") if completed_at else "Finished"
    if status == "completed" and started_at and completed_at:
        eta_text = f"Completed in {format_duration((completed_at - started_at).total_seconds()).lower()}"
        completion_text = completed_at.strftime("%I:%M %p").lstrip("0")

    progress_stage = row["progress_stage"] if "progress_stage" in row.keys() else ""
    if not progress_stage:
        progress_stage = "complete" if status == "completed" else "failed" if status == "failed" else "saving_records" if rows_processed else "queued"
    stage_order = {
        "queued": 3,
        "checking_duplicates": 3,
        "saving_records": 5,
        "building_summary": 6,
        "dashboard": 7,
        "complete": 8,
        "failed": 5,
    }
    current_stage_index = stage_order.get(progress_stage, 5)
    stages = [
        ("Reading file", "File accepted from upload."),
        ("Parsing rows", "Headers and rows were detected."),
        ("Validating customer fields", "Identity, contact, and consent fields checked."),
        ("Checking duplicate records", "Duplicate risk is compared against the CRM."),
        ("Matching existing customers", "Existing records are matched before save."),
        ("Saving customer records", "New and existing customers are written safely."),
        ("Building import summary", "Reachability and cleanup metrics are rebuilt."),
        ("Updating CRM dashboard", "Dashboard counts and task recommendations refresh."),
        ("Preparing imported data for use", "The CRM is ready for follow-up work."),
    ]
    stage_html = ""
    for index, (label, detail) in enumerate(stages):
        if status == "failed" and index == current_stage_index:
            stage_state = "failed"
        elif status == "completed" or index < current_stage_index:
            stage_state = "complete"
        elif index == current_stage_index:
            stage_state = "active"
        else:
            stage_state = "pending"
        stage_html += f"""
        <li class="import-stage {stage_state}">
          <span>{index + 1}</span>
          <div><strong>{escape(label)}</strong><small>{escape(detail)}</small></div>
        </li>
        """

    business_summary = ""
    if status == "completed":
        capture_metrics = {}
        conn = db_connection()
        try:
            capture_metrics = capture_gap_with_conn(conn)
        finally:
            conn.close()
        import_summary = import_summary_from_row(row) or {}
        recommendation = "Review duplicate records first, then generate an AI brief to choose the best campaign segment for this week."
        business_summary = f"""
        <section class="panel import-complete-panel">
          <span class="eyebrow">Import complete</span>
          <h3>Your Seaview customer data is ready to use.</h3>
          <p>{escape(import_summary.get('headline') or 'The customer file has been cleaned, matched, and saved into the CRM.')}</p>
          <div class="stats import-progress-stats">
            <article><span>Campaign-ready</span><strong>{int(capture_metrics.get('freshline_campaign_ready') or 0):,}</strong></article>
            <article><span>Reachable Freshline</span><strong>{int(capture_metrics.get('freshline_reachable') or 0):,}</strong></article>
            <article><span>Unreachable Clover</span><strong>{int(capture_metrics.get('dark_customers') or 0):,}</strong></article>
            <article><span>Marketing allowed</span><strong>{int(capture_metrics.get('marketing_allowed') or 0):,}</strong></article>
          </div>
          <div class="import-next-action">
            <strong>Recommended next action</strong>
            <p>{escape(recommendation)}</p>
          </div>
          <div class="button-row">
            <a class="button" href="/imports?summary={row['id']}">View import summary</a>
            <a class="button secondary" href="/duplicates">Review duplicates</a>
            <a class="button secondary" href="/">Open dashboard</a>
            <a class="button secondary" href="/imports/ai-brief?summary={row['id']}">Generate AI brief</a>
            <a class="button secondary" href="/marketing#audiences">Create campaign segment</a>
          </div>
        </section>
        """

    error_html = (
        f"<div class='warning-panel'><strong>Import error</strong><p>{escape(row['error_message'] or 'Unknown error')}</p></div>"
        if status == "failed"
        else ""
    )
    safety_copy = (
        "This import is running server-side. You can keep this page open to watch progress, but closing the tab will not start a duplicate import."
        if is_active
        else "The import lock has been released. You can safely continue using the CRM."
    )
    status_title = "Import complete" if status == "completed" else "Importing Seaview customer data"
    return {
        "ok": True,
        "status": status,
        "status_title": status_title,
        "status_pill_html": status_pill(status),
        "is_active": is_active,
        "source_label": source_system_label(row["source_system"]),
        "filename": display_upload_name(row["filename"]) or row["filename"] or "Customer import",
        "progress_copy": progress_copy,
        "safety_copy": safety_copy,
        "percent": percent,
        "rows_total": rows_total,
        "rows_processed": rows_processed,
        "customers_created": int(row["customers_created"] or 0),
        "customers_updated": int(row["customers_updated"] or 0),
        "review_needed_rows": int(row["review_needed_rows"] or 0),
        "skipped_rows": int(row["skipped_rows"] or 0),
        "eta_text": eta_text,
        "completion_text": completion_text,
        "speed_text": speed_text,
        "stage_html": stage_html,
        "error_html": error_html,
        "business_summary_html": business_summary,
    }


def render_import_run_status(import_run_id: int, message: str = "", user: dict | None = None) -> bytes:
    payload = import_run_status_payload(import_run_id)
    if not payload:
        return base_layout(
            "Import Status",
            "<div class='panel'><h2>Import not found</h2><p>This import job may have expired or been removed.</p><a class='button secondary' href='/imports'>Back to imports</a></div>",
            flash=message,
            active_section="imports",
            user=user,
        )

    body = f"""
    <div class="import-status-shell" data-import-run-status data-status-url="/imports/runs/{import_run_id}/status.json" data-poll-seconds="3" data-import-active="{str(payload['is_active']).lower()}" data-import-status="{escape(payload['status'])}">
    <section class="page-head">
      <div>
        <h2 data-import-status-title>{escape(payload['status_title'])}</h2>
        <p>We are cleaning, matching, and saving a large customer file so Seaview can use it for customer intelligence and campaign planning.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/imports">Back to imports</a>
      </div>
    </section>
    <section class="panel import-progress-panel" data-import-progress-panel aria-live="polite" aria-busy="{str(payload['is_active']).lower()}">
      <div class="import-progress-head">
        <div>
          <span class="eyebrow"><span data-import-source>{escape(payload['source_label'])}</span> · <span data-import-filename>{escape(payload['filename'])}</span></span>
          <h3><span data-import-status-pill>{payload['status_pill_html']}</span><span class="import-progress-copy" data-import-progress-copy>{escape(payload['progress_copy'])}</span></h3>
          <p class="muted" data-import-safety-copy>{escape(payload['safety_copy'])}</p>
        </div>
        <strong data-import-percent>{payload['percent']}%</strong>
      </div>
      <div class="import-progress-track" aria-label="Import progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{payload['percent']}" data-import-progress-track>
        <span style="width:{payload['percent']}%" data-import-progress-bar></span>
      </div>
      <div class="stats import-progress-stats">
        <article><span>Processed</span><strong data-import-rows-processed>{payload['rows_processed']:,}</strong><small data-import-rows-total>of {payload['rows_total']:,} rows</small></article>
        <article><span>Created</span><strong data-import-created>{payload['customers_created']:,}</strong></article>
        <article><span>Merged</span><strong data-import-updated>{payload['customers_updated']:,}</strong></article>
        <article><span>Review</span><strong data-import-review>{payload['review_needed_rows']:,}</strong></article>
        <article><span>Skipped</span><strong data-import-skipped>{payload['skipped_rows']:,}</strong></article>
      </div>
      <div class="grid import-estimate-grid">
        <article class="panel import-estimate-panel"><span>Estimated time remaining</span><strong data-import-eta>{escape(payload['eta_text'])}</strong></article>
        <article class="panel import-estimate-panel"><span>Estimated completion</span><strong data-import-completion-time>{escape(payload['completion_text'])}</strong></article>
        <article class="panel import-estimate-panel"><span>Processing speed</span><strong data-import-speed>{escape(payload['speed_text'])}</strong></article>
      </div>
      <div class="panel import-safety-panel">
        <strong>What is happening now</strong>
        <p data-import-helper-copy>{escape('Large files can take a few minutes while duplicate checks, customer matching, and dashboard metrics are rebuilt. Progress updates automatically every few seconds without reloading this page.')}</p>
      </div>
      <ol class="import-stage-list" data-import-stage-list>{payload['stage_html']}</ol>
      <div data-import-error>{payload['error_html']}</div>
    </section>
    <div data-import-completion-panel>{payload['business_summary_html']}</div>
    <noscript>
      <div class="warning-panel"><strong>Manual refresh needed</strong><p>JavaScript is disabled, so this page cannot update in place. Refresh this page to check import progress.</p></div>
    </noscript>
    </div>
    """
    return base_layout("Import Status", body, flash=message, active_section="imports", user=user)


def render_signup(message: str = "", user: dict | None = None) -> bytes:
    settings = get_app_settings()
    conn = db_connection()
    try:
        lead_capture = lead_capture_snapshot_with_conn(conn)
    finally:
        conn.close()
    qr_by_location = lead_capture.get("qr_by_location", {})
    public_origin = public_origin_for_display()

    qr_location_cards = "".join(
        f"""
        <div class="qr-location-card">
          <a class="qr-thumb" href="/capture/qr/preview?location={escape(key)}" aria-label="Preview {escape(loc['label'])} QR code">
            <img src="/capture/qr/generate?location={escape(key)}" alt="QR code for {escape(loc['label'])} customer signup" loading="lazy">
          </a>
          <strong>{escape(loc['label'])}</strong>
          <span class="muted">{qr_by_location.get(key, 0)} scans this week</span>
          <input class="copyable-url" data-copy-source value="{escape(public_origin + loc['path'])}" readonly aria-label="Signup URL for {escape(loc['label'])}">
          <div class="button-row">
            <a class="button small" href="/capture/qr/preview?location={escape(key)}">Preview &amp; Print</a>
            <a class="button secondary small" href="{escape(public_origin + loc['path'])}">Open form</a>
            <button type="button" class="button secondary small" data-copy-url="{escape(public_origin + loc['path'])}">Copy</button>
            <a class="button secondary small" href="/capture/qr/generate?location={escape(key)}">Download PNG</a>
          </div>
        </div>
        """
        for key, loc in QR_LOCATIONS.items()
    )

    loc_summary_parts = " &middot; ".join(
        f"{escape(loc['label'])}: {qr_by_location.get(key, 0)}"
        for key, loc in QR_LOCATIONS.items()
    )
    public_capture_cards = "".join(
        f"""
        <article class="public-qr-card">
          <a class="public-qr-thumb" href="/capture/qr/preview?{escape(urlencode({'page': page['path']}))}" target="_blank" aria-label="Preview QR for {escape(page['label'])}">
            <img src="/capture/qr/generate?{escape(urlencode({'page': page['path']}))}" alt="QR code for {escape(page['label'])}" loading="lazy">
          </a>
          <div class="public-qr-copy">
            <strong>{escape(page['label'])}</strong>
            <span class="muted">Scans open a customer signup form and save contacts to the CRM.</span>
            <code>{escape(public_origin + page['path'])}</code>
            <input class="copyable-url" data-copy-source value="{escape(public_origin + page['path'])}" readonly aria-label="Signup URL for {escape(page['label'])}">
          </div>
          <div class="button-row">
            <a class="button small" href="{escape(public_origin + page['path'])}">Open form</a>
            <a class="button secondary small" href="/capture/qr/preview?{escape(urlencode({'page': page['path']}))}">Preview</a>
            <button type="button" class="button secondary small" data-copy-url="{escape(public_origin + page['path'])}">Copy</button>
          </div>
        </article>
        """
        for page in public_capture_pages()
    )
    if ai_is_configured(get_setting):
        ai_capture_html = """
        <div class="panel ai-panel">
          <h3>AI capture-page copy</h3>
          <p class="muted">Generate tighter QR signup copy for the current offer. This updates the public capture pages.</p>
          <form method="post" action="/capture/ai-copy" class="stack">
            <label>Current special or signup hook
              <input type="text" name="special" placeholder="Fresh catch texts, weekend specials, first deal after signup">
            </label>
            <button type="submit" class="secondary">Generate capture copy</button>
          </form>
        </div>
        """
    elif user and user.get("role") == "admin":
        ai_capture_html = """
        <div class="panel ai-panel">
          <h3>AI capture-page copy</h3>
          <p class="muted">Configure your OpenAI API key to generate mobile-first QR signup copy.</p>
          <a class="button secondary small" href="/admin">Configure AI</a>
        </div>
        """
    else:
        ai_capture_html = ""

    body = f"""
    <section class="page-head">
      <div>
        <h2>Capture</h2>
        <p>{escape(settings['capture_prompt'] or 'Capture a new contact or interaction.')}</p>
      </div>
    </section>

    <section class="grid balanced-grid">
      <div class="panel signup-panel">
        <h3>New contact</h3>
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
          <label>Offer hook
            <input type="text" name="capture_offer" value="{escape(settings['primary_offer_hook'] or '')}" placeholder="Weekly seafood specials and fresh catch alerts">
          </label>
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
      </div>
      <div class="panel public-qr-panel">
        <h3>Customer signup QR previews</h3>
        <p class="muted">Scan, test, print, or copy any public capture form. Each signup creates or updates a customer record.</p>
        <p class="qr-network-note">Phone URL: <code>{escape(public_origin)}</code>. Your phone must be on the same Wi-Fi for local demos. For real use, set Admin &rarr; Public CRM URL to the deployed site.</p>
        <div class="public-qr-grid">
          {public_capture_cards}
        </div>
      </div>
    </section>

    <div class="panel qr-panel">
      <h3>QR Codes for In-Store Capture</h3>
      <p class="muted">Generate and print a QR code for each location. Each code tracks where signups come from.</p>
      <div class="qr-location-grid">{qr_location_cards}</div>
      <div class="qr-scan-summary">
        <strong>This week:</strong> {loc_summary_parts}
      </div>
    </div>
    {ai_capture_html}
    """
    return base_layout("Capture", body, flash=message, active_section="capture", user=user)


def render_settings(message: str = "", user: dict | None = None) -> bytes:
    settings = get_app_settings()
    duplicate_review_checked = "checked" if settings["duplicate_review_required_before_campaign_export"] else ""
    body = f"""
    <section class="page-head">
      <div>
        <h2>Settings</h2>
        <p>Set the defaults that appear across capture, imports, and outreach.</p>
      </div>
    </section>

    <section class="grid balanced-grid">
      <div class="panel">
        <h3>Business defaults</h3>
        <form method="post" action="/settings" class="stack">
          <label>Business name
            <input type="text" name="business_name" value="{escape(settings['business_name'] or '')}">
          </label>
          <label>Primary location
            <input type="text" name="primary_location" value="{escape(settings['primary_location'] or '')}">
          </label>
          <div class="field-grid">
            <label>Weekly import owner
              <input type="text" name="weekly_import_owner" value="{escape(settings['weekly_import_owner'] or '')}">
            </label>
            <label>Weekly outreach day
              <input type="text" name="weekly_outreach_day" value="{escape(settings['weekly_outreach_day'] or '')}">
            </label>
          </div>
          <label>Main offer hook
            <input type="text" name="primary_offer_hook" value="{escape(settings['primary_offer_hook'] or '')}">
          </label>
          <label>Capture prompt for staff
            <textarea name="capture_prompt" rows="3">{escape(settings['capture_prompt'] or '')}</textarea>
          </label>
          <div class="field-grid">
            <label>Preferred primary data source
              <select name="preferred_primary_data_source">{option_list([(key, guide['label']) for key, guide in IMPORT_SOURCE_GUIDES.items()], settings['preferred_primary_data_source'] or 'clover')}</select>
            </label>
            <label>Default capture CTA
              <input type="text" name="default_capture_cta" value="{escape(settings['default_capture_cta'] or '')}">
            </label>
          </div>
          <label class="checkbox-inline"><input type="checkbox" name="duplicate_review_required_before_campaign_export" value="1" {duplicate_review_checked}> Require duplicate review cleanup before campaign export</label>
          <button type="submit">Save settings</button>
        </form>
      </div>
      <div class="panel">
        <h3>Current setup</h3>
        <dl class="details">
          <dt>Business</dt><dd>{escape(settings['business_name'] or 'Seaview Crab Company')}</dd>
          <dt>Staff username</dt><dd>{escape(DEFAULT_STAFF_USERNAME)}</dd>
          <dt>Import owner</dt><dd>{escape(settings['weekly_import_owner'] or 'Not set')}</dd>
          <dt>Outreach day</dt><dd>{escape(settings['weekly_outreach_day'] or 'Not set')}</dd>
          <dt>Primary source</dt><dd>{escape(source_system_label(settings['preferred_primary_data_source'] or 'clover'))}</dd>
          <dt>Export gate</dt><dd>{'Duplicate review required' if settings['duplicate_review_required_before_campaign_export'] else 'Optional'}</dd>
          <dt>Updated</dt><dd>{escape(display_timestamp(settings['updated_at']))}</dd>
        </dl>
        <div class="copy-row">
          <a class="button secondary small" href="/join" target="_blank">Website</a>
          <a class="button secondary small" href="/qr-tools">QR Kits</a>
          <a class="button secondary small" href="/imports">Open imports</a>
        </div>
      </div>
    </section>
    """
    return base_layout("Settings", body, flash=message, active_section="settings", user=user)


def render_admin_dashboard(user: dict, message: str = "") -> bytes:
    business_name = get_setting("business_name", "Seaview Crab Company")
    sender_name = get_setting("sender_name")
    sender_email = get_setting("sender_email")
    public_base_url = get_setting("public_base_url")
    openai_key_label = mask_secret(api_key_from_settings(get_setting))
    openai_model = model_from_settings(get_setting)
    ai_status = "Configured" if ai_is_configured(get_setting) else "Not configured"
    conn = db_connection()
    try:
        last_task_refresh = latest_task_refresh_run_with_conn(conn)
    finally:
        conn.close()

    def mask(val: str) -> str:
        if not val:
            return "Not configured"
        return "•" * (len(val) - 4) + val[-4:] if len(val) > 4 else "••••"

    sg_key = mask(get_setting("sendgrid_api_key"))
    twilio_sid = mask(get_setting("twilio_account_sid"))
    twilio_token = mask(get_setting("twilio_auth_token"))
    twilio_phone = get_setting("twilio_phone_number") or "Not configured"

    body = f"""
    <section class="page-head">
      <div>
        <h2>Admin Settings</h2>
        <p>System configuration and staff management.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/admin/staff">Manage Staff</a>
        <a class="button secondary" href="/admin/audit">Audit Log</a>
      </div>
    </section>

    <div class="panel">
      <h3>Business Settings</h3>
      <form method="post" action="/admin/settings" class="stack">
        <div class="field-grid">
          <label>Business name
            <input type="text" name="business_name" value="{escape(business_name)}">
          </label>
          <label>Sender name (for future emails)
            <input type="text" name="sender_name" value="{escape(sender_name)}">
          </label>
        </div>
        <label>Sender email address
          <input type="email" name="sender_email" value="{escape(sender_email)}" placeholder="hello@seaviewcrab.com">
        </label>
        <label>Public CRM URL for QR codes
          <input type="url" name="public_base_url" value="{escape(public_base_url)}" placeholder="https://seaview-crm.onrender.com">
        </label>
        <p class="muted">Leave blank during local demos. The app will use your LAN IP so a phone on the same Wi-Fi can scan the QR code.</p>
        <button type="submit">Save settings</button>
      </form>
    </div>

    <div class="panel">
      <h3>Email &amp; SMS Configuration</h3>
      <p class="muted">Email and SMS sending will be activated once these are configured. These fields are stored securely and never displayed in full.</p>
      <form method="post" action="/admin/settings" class="stack">
        <div class="field-grid">
          <label>SendGrid API Key
            <input type="password" name="sendgrid_api_key" placeholder="{escape(sg_key)}">
          </label>
          <label>Twilio Account SID
            <input type="password" name="twilio_account_sid" placeholder="{escape(twilio_sid)}">
          </label>
        </div>
        <div class="field-grid">
          <label>Twilio Auth Token
            <input type="password" name="twilio_auth_token" placeholder="{escape(twilio_token)}">
          </label>
          <label>Twilio Phone Number
            <input type="text" name="twilio_phone_number" value="{escape(twilio_phone if twilio_phone != 'Not configured' else '')}" placeholder="+19105550100">
          </label>
        </div>
        <button type="submit">Save API configuration</button>
      </form>
    </div>

    <div class="panel ai-panel">
      <div class="panel-head">
        <div>
          <h3>AI Configuration</h3>
          <p class="muted">Native AI assists the weekly brief, campaign drafts, and QR capture-page copy.</p>
        </div>
        <span class="status-pill">{escape(ai_status)}</span>
      </div>
      <form method="post" action="/admin/settings" class="stack">
        <div class="field-grid">
          <label>OpenAI API Key
            <input type="password" name="openai_api_key" placeholder="{escape(openai_key_label)}">
          </label>
          <label>Model
            <input type="text" name="openai_model" value="{escape(openai_model)}" placeholder="gpt-4o-mini">
          </label>
        </div>
        <p class="muted">For production, prefer setting OPENAI_API_KEY in Render. The environment value overrides the stored admin setting.</p>
        <button type="submit">Save AI settings</button>
      </form>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div>
          <h3>Task Recommendation Audit</h3>
          <p class="muted">Shows the latest refresh that updated the existing dashboard task list.</p>
        </div>
        <form method="post" action="/tasks/refresh">
          <button type="submit" class="button secondary small">Refresh now</button>
        </form>
      </div>
      {(
        f"<dl class='details'>"
        f"<dt>Last refresh</dt><dd>{escape(display_timestamp(last_task_refresh['created_at']))}</dd>"
        f"<dt>Trigger</dt><dd>{escape(last_task_refresh['trigger_event'])}</dd>"
        f"<dt>Mode</dt><dd>{escape('AI' if last_task_refresh['used_ai'] else 'Rule fallback')}</dd>"
        f"<dt>Created / updated</dt><dd>{last_task_refresh['tasks_created']} / {last_task_refresh['tasks_updated']}</dd>"
        f"<dt>Error</dt><dd>{escape(last_task_refresh['error_message'] or 'None')}</dd>"
        f"</dl>"
      ) if last_task_refresh else "<p class='muted'>No task recommendation refresh has run yet.</p>"}
    </div>

    <div class="panel">
      <h3>Production Handoff Notes</h3>
      <ul class="stacked-list compact-list">
        <li><strong>Health check</strong><span><a href="/healthz">/healthz</a> verifies the app can reach SQLite.</span></li>
        <li><strong>SQLite location</strong><span>{escape(str(DB_PATH))}</span></li>
        <li><strong>Backup routine</strong><span>On Render, periodically download or snapshot the persistent disk mounted at <code>/data</code>.</span></li>
        <li><strong>Secrets</strong><span>Use Render environment variables for <code>SEAVIEW_SESSION_SECRET</code> and <code>OPENAI_API_KEY</code>; do not commit keys.</span></li>
        <li><strong>Recovery</strong><span>Keep recent CSV imports available so the customer file can be rebuilt if needed.</span></li>
      </ul>
    </div>
    """
    return base_layout("Admin Settings", body, flash=message, active_section="admin", user=user)


def render_admin_staff(user: dict, message: str = "") -> bytes:
    conn = db_connection()
    try:
        staff_rows = conn.execute(
            "SELECT * FROM staff_users ORDER BY role DESC, id"
        ).fetchall()
    finally:
        conn.close()

    row_html_parts = []
    for row in staff_rows:
        if row["username"] == user["username"]:
            actions_html = "—"
        else:
            deactivate_disabled = " disabled" if not row["is_active"] else ""
            deactivate_label = "Inactive" if not row["is_active"] else "Deactivate"
            actions_html = f"""
            <div class="table-actions">
              <form method="post" action="/admin/staff/{row['id']}/deactivate">
                <button type="submit" class="button secondary small"{deactivate_disabled}>{deactivate_label}</button>
              </form>
              <form method="post" action="/admin/staff/{row['id']}/reset" class="inline-reset-form">
                <input type="password" name="new_password" placeholder="New password" required style="width:140px">
                <button type="submit" class="button secondary small">Reset</button>
              </form>
            </div>
            """
        row_html_parts.append(
            f"""
            <tr>
              <td><strong>{escape(row['display_name'])}</strong></td>
              <td>{escape(row['username'])}</td>
              <td>{escape(row['role'].title())}</td>
              <td>{escape(display_timestamp(row['last_login_at'], include_time=False) if row['last_login_at'] else 'Never')}</td>
              <td>{'Active' if row['is_active'] else 'Inactive'}</td>
              <td>{actions_html}</td>
            </tr>
            """
        )
    rows_html = "".join(row_html_parts)

    body = f"""
    <section class="page-head">
      <div>
        <h2>Staff Management</h2>
        <p>Add and manage staff access to the CRM.</p>
      </div>
      <a class="button secondary" href="/admin">Back to settings</a>
    </section>

    <section class="grid">
      <div class="panel">
        <h3>Add staff member</h3>
        <form method="post" action="/admin/staff" class="stack">
          <div class="field-grid">
            <label>Display name
              <input type="text" name="display_name" required>
            </label>
            <label>Username
              <input type="text" name="username" required>
            </label>
          </div>
          <div class="field-grid">
            <label>Password
              <input type="password" name="password" required>
            </label>
            <label>Role
              <select name="role">
                <option value="staff">Staff</option>
                <option value="admin">Admin</option>
              </select>
            </label>
          </div>
          <button type="submit">Add staff member</button>
        </form>
      </div>
      <div class="panel">
        <h3>Access notes</h3>
        <ul class="stacked-list compact-list">
          <li><strong>Admin</strong><span>Full access including settings and audit log</span></li>
          <li><strong>Staff</strong><span>Customers, marketing, capture, and imports. No admin settings.</span></li>
        </ul>
      </div>
    </section>

    <div class="panel">
      <h3>Current staff</h3>
      <table>
        <thead>
          <tr><th>Name</th><th>Username</th><th>Role</th><th>Last login</th><th>Status</th><th>Actions</th></tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """
    return base_layout("Staff Management", body, flash=message, active_section="admin", user=user)


def render_admin_audit(user: dict, message: str = "") -> bytes:
    conn = db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    finally:
        conn.close()

    audit_rows = "".join(
        f"""
        <tr>
          <td>{escape(display_timestamp(row['created_at']))}</td>
          <td>{escape(row['username'] or '—')}</td>
          <td>{escape(row['action'].replace('_', ' ').title())}</td>
          <td>{escape(row['detail'] or '—')}</td>
        </tr>
        """
        for row in rows
    ) or "<tr><td colspan='4'>No audit events yet.</td></tr>"

    body = f"""
    <section class="page-head">
      <div>
        <h2>Audit Log</h2>
        <p>Staff actions and system events.</p>
      </div>
      <a class="button secondary" href="/admin">Back to settings</a>
    </section>
    <div class="panel">
      <table>
        <thead>
          <tr><th>When</th><th>User</th><th>Action</th><th>Detail</th></tr>
        </thead>
        <tbody>{audit_rows}</tbody>
      </table>
    </div>
    """
    return base_layout("Audit Log", body, flash=message, active_section="admin", user=user)


def ai_operating_context(conn: sqlite3.Connection, extra: dict | None = None) -> dict:
    """Compact CRM state for optional AI helpers; no customer lists or secrets."""
    segments = segment_definitions()
    segment_counts = segment_counts_with_conn(conn, segments)
    capture_gap = capture_gap_with_conn(conn)
    cleanup = freshline_cleanup_with_conn(conn, limit=5)
    results = results_snapshot_with_conn(conn)
    settings = conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
    context = {
        "business_name": settings["business_name"] if settings else "Seaview Crab Company",
        "primary_location": settings["primary_location"] if settings else "",
        "current_offer_hook": settings["primary_offer_hook"] if settings else "",
        "capture_prompt": settings["capture_prompt"] if settings else "",
        "locked_data_findings": {
            "freshline_total": capture_gap.get("freshline_total", 3069),
            "freshline_campaign_ready": capture_gap.get("freshline_campaign_ready", 3029),
            "clover_total": capture_gap.get("clover_total", 79737),
            "clover_email_reachable": capture_gap.get("reachable_email", 381),
            "clover_dark_customers": capture_gap.get("dark_customers", 78394),
            "clover_opted_in_no_contact": capture_gap.get("opted_no_contact", 1588),
        },
        "audience_counts": {
            key: segment_counts.get(key, 0)
            for key in (
                "email_ready",
                "sms_ready",
                "clean_campaign_ready",
                "needs_attention",
                "vip_customers",
                "recent_buyers",
                "lapsed_buyers",
                "newsletter_prospects",
            )
        },
        "cleanup": {
            "invalid_phones": cleanup.get("invalid_phone_total", 0),
            "internal_excluded": cleanup.get("internal_total", 0),
            "duplicate_groups": cleanup.get("duplicate_total", 0),
            "example_invalid_phones": cleanup.get("invalid_phone_records", [])[:5],
        },
        "roi": {
            "clv": round(float(results.get("clv", 0.0)), 2),
            "reachable_value": round(float(results.get("reachable_value", 0.0)), 2),
            "capture_gap_value": round(float(results.get("capture_gap_value", 0.0)), 2),
            "actual_avg_order_value": round(float(results.get("avg_order_value_actual", 0.0)), 2),
        },
    }
    if extra:
        context["request"] = extra
    return context


def ai_import_context(
    conn: sqlite3.Connection,
    import_summary: dict,
    summary_row: sqlite3.Row | None,
) -> dict:
    compact_summary = {
        "source_type": import_summary.get("source_type", "general"),
        "source_system": import_summary.get("source_system", ""),
        "headline": import_summary.get("headline", ""),
        "business_lines": import_summary.get("business_lines", [])[:4],
        "improvements": import_summary.get("improvements", [])[:4],
        "still_broken": import_summary.get("still_broken", [])[:4],
        "net_changes": import_summary.get("net_changes", {}),
        "records_imported": int(import_summary.get("records_imported") or 0),
        "customers_touched": int(import_summary.get("customers_touched") or 0),
        "records_previously_on_file": int(import_summary.get("records_previously_on_file") or 0),
        "records_after_import": int(import_summary.get("records_after_import") or 0),
        "imported_at": summary_row["created_at"] if summary_row else "",
    }

    source_type = compact_summary["source_type"]
    source_metrics = import_summary.get("source_metrics", {})
    if source_type == "clover":
        compact_summary["source_metrics"] = {
            "clover_total": int(source_metrics.get("clover_total") or 0),
            "reachable_email": int(source_metrics.get("reachable_email") or 0),
            "reachable_phone": int(source_metrics.get("reachable_phone") or 0),
            "marketing_allowed": int(source_metrics.get("marketing_allowed") or 0),
            "opted_no_contact": int(source_metrics.get("opted_no_contact") or 0),
            "dark_customers": int(source_metrics.get("dark_customers") or 0),
            "capture_rate": float(source_metrics.get("capture_rate") or 0.0),
            "five_percent_goal": int(source_metrics.get("five_percent_goal") or 0),
            "ten_percent_goal": int(source_metrics.get("ten_percent_goal") or 0),
            "freshline_reachable": int(source_metrics.get("freshline_reachable") or 0),
            "freshline_campaign_ready": int(source_metrics.get("freshline_campaign_ready") or 0),
        }
    elif source_type == "freshline":
        audience = source_metrics.get("audience", {})
        cleanup = source_metrics.get("cleanup", {})
        compact_summary["source_metrics"] = {
            "audience": {
                "email_ready": int(audience.get("email_ready") or 0),
                "sms_ready": int(audience.get("sms_ready") or 0),
                "clean_campaign_ready": int(audience.get("clean_campaign_ready") or 0),
                "needs_attention": int(audience.get("needs_attention") or 0),
            },
            "cleanup": {
                "internal_total": int(cleanup.get("internal_total") or 0),
                "invalid_phone_total": int(cleanup.get("invalid_phone_total") or 0),
                "invalid_campaign_phone_total": int(cleanup.get("invalid_campaign_phone_total") or 0),
                "duplicate_total": int(cleanup.get("duplicate_total") or 0),
                "first_name_only_total": int(cleanup.get("first_name_only_total") or 0),
                "invalid_phone_examples": cleanup.get("invalid_phone_records", [])[:5],
            },
        }
    else:
        compact_summary["source_metrics"] = source_metrics

    return ai_operating_context(
        conn,
        {
            "task": "import_intelligence_brief",
            "latest_import": compact_summary,
        },
    )


def import_ai_brief_from_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    raw = row["ai_brief_json"] if "ai_brief_json" in row.keys() else ""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def persist_import_ai_brief(summary_id: int, brief: dict, user: dict | None = None, client_ip: str = "") -> str:
    saved_at = utc_now()
    conn = db_connection()
    try:
        conn.execute(
            """
            UPDATE import_runs
            SET ai_brief_json = ?, ai_brief_created_at = ?
            WHERE id = ?
            """,
            (json.dumps(brief), saved_at, summary_id),
        )
        if user:
            log_audit(
                conn,
                user["id"],
                user["username"],
                "imports_ai_brief_saved",
                f"Saved AI brief for import {summary_id}",
                client_ip,
            )
        conn.commit()
    finally:
        conn.close()
    return saved_at


def generate_saved_import_ai_brief(summary_id: int, user: dict | None = None, client_ip: str = "") -> tuple[dict | None, str | None]:
    if not ai_is_configured(get_setting):
        return None, "OpenAI API key is not configured."

    conn = db_connection()
    try:
        summary_row = conn.execute("SELECT * FROM import_runs WHERE id = ?", (summary_id,)).fetchone()
        import_summary = import_summary_from_row(summary_row)
        if not summary_row or not import_summary:
            return None, "No import intelligence summary is available yet."
        context = ai_import_context(conn, import_summary, summary_row)
    finally:
        conn.close()

    try:
        brief = generate_import_brief(
            api_key=api_key_from_settings(get_setting),
            model=model_from_settings(get_setting),
            context=context,
        )
    except AIError as exc:
        return None, str(exc)
    except Exception as exc:
        logger.exception("AI import brief failed for import %s", summary_id)
        return None, str(exc)[:220]

    try:
        persist_import_ai_brief(summary_id, brief, user=user, client_ip=client_ip)
    except Exception as exc:
        logger.exception("AI import brief persistence failed for import %s", summary_id)
        return None, str(exc)[:220]
    return brief, None


def healthcheck_response() -> tuple[HTTPStatus, bytes]:
    # Render uses this endpoint to decide whether the web process is alive.
    # Keep it independent of SQLite so a large import cannot make health
    # checks look down while the app is busy writing customer batches.
    payload = json.dumps(
        {
            "ok": True,
            "service": "seaview-crm",
            "database": "not_checked",
            "time": utc_now(),
        }
    ).encode("utf-8")
    return HTTPStatus.OK, payload


def render_ai_weekly_brief(brief: dict, user: dict | None, message: str = "") -> bytes:
    def clean_text(value, fallback: str = "") -> str:
        text = str(value or "").strip()
        return text if text else fallback

    def clean_list(value, limit: int = 5) -> list[str]:
        if isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, dict):
                    text = clean_text(item.get("text") or item.get("summary") or item.get("title") or item.get("reason"))
                else:
                    text = clean_text(item)
                if text:
                    items.append(text)
            return items[:limit]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    sections = brief.get("sections") if isinstance(brief.get("sections"), dict) else {}
    actions = brief.get("actions") if isinstance(brief.get("actions"), list) else []
    executive_summary = clean_text(
        brief.get("executive_summary") or brief.get("summary"),
        "Focus this week on the highest-confidence customer work, then verify the data quality risks before campaign handoff.",
    )
    key_customer_insights = (
        clean_list(sections.get("key_customer_insights"))
        or clean_list(brief.get("key_customer_insights"))
        or [executive_summary]
    )
    data_quality_issues = (
        clean_list(sections.get("data_quality_issues"))
        or clean_list(brief.get("data_quality_issues"))
        or clean_list(brief.get("risks"))
        or ["Review duplicate, consent, and reachability signals before exporting a campaign list."]
    )
    campaign_opportunities = (
        clean_list(sections.get("campaign_opportunities"))
        or clean_list(brief.get("campaign_opportunities"))
        or clean_list([action.get("reason") for action in actions if isinstance(action, dict)])
        or ["Use the current CRM segments to choose one focused audience for this week's outreach."]
    )
    risks_or_missing_information = (
        clean_list(sections.get("risks_or_missing_information"))
        or clean_list(brief.get("risks_or_missing_information"))
        or clean_list(brief.get("risks"))
        or ["AI can structure the review, but locked CRM counts remain the source of truth."]
    )

    metrics = brief.get("key_metrics") if isinstance(brief.get("key_metrics"), list) else []
    if not metrics:
        metrics = [
            {"label": "Brief mode", "value": "CRM counts", "context": "Recommendations are generated from locked CRM totals."},
            {"label": "Next actions", "value": str(len(actions) or "Review"), "context": "Manager-ready work items for the week."},
            {"label": "Data check", "value": "Required", "context": "Confirm reachability, consent, and duplicate risk before exports."},
        ]
    metric_html = "".join(
        f"""
        <article class="brief-metric-card">
          <span>{escape(clean_text(metric.get('label'), 'Metric'))}</span>
          <strong>{escape(clean_text(metric.get('value'), 'Review'))}</strong>
          <p>{escape(clean_text(metric.get('context'), 'Use this signal in the weekly review.'))}</p>
        </article>
        """
        for metric in metrics[:6]
        if isinstance(metric, dict)
    )

    def section_card(title: str, items: list[str]) -> str:
        bullet_html = "".join(f"<li>{escape(item)}</li>" for item in items[:6])
        return f"""
        <article class="brief-section-card">
          <h3>{escape(title)}</h3>
          <ul class="brief-list">{bullet_html}</ul>
        </article>
        """

    actions = brief.get("actions") if isinstance(brief.get("actions"), list) else []
    action_html = "".join(
        f"""
        <li class="brief-action-card">
          <span class="brief-action-number">{str(i + 1).zfill(2)}</span>
          <div class="brief-action-body">
            <strong>{escape(clean_text(action.get('title'), 'Next action'))}</strong>
            <p>{escape(clean_text(action.get('reason'), 'Use the latest CRM data to decide the next move.'))}</p>
            <div class="brief-action-meta">
              <span>Owner: {escape(clean_text(action.get('owner'), 'Manager'))}</span>
              <span>Timing: {escape(clean_text(action.get('timing'), 'This week'))}</span>
              <span>{escape(clean_text(action.get('cta'), 'Assign this to staff.'))}</span>
            </div>
          </div>
        </li>
        """
        for i, action in enumerate(actions[:5])
        if isinstance(action, dict)
    )
    if not action_html:
        action_html = "<li class='brief-empty'>The AI brief did not return action items. Regenerate after the CRM counts refresh.</li>"
    body = f"""
    <section class="page-head">
      <div>
        <h2>AI Weekly Brief</h2>
        <p>Generated from the current CRM counts, cleanup state, capture gap, and ROI inputs.</p>
      </div>
      <div class="button-row">
        <a class="button secondary" href="/">Back to dashboard</a>
        <form method="post" action="/ai/weekly-brief" class="inline-form">
          <button type="submit">Regenerate</button>
        </form>
      </div>
    </section>
    <section class="panel ai-output brief-hero">
      <div class="brief-hero-copy">
        <span class="eyebrow">Executive Summary</span>
        <h3>{escape(clean_text(brief.get('headline'), 'This week at Seaview'))}</h3>
        <p>{escape(executive_summary)}</p>
      </div>
      <div class="brief-source-chip">
        <strong>Source of truth</strong>
        <span>Locked CRM counts plus cleanup state</span>
      </div>
    </section>
    <section class="brief-metric-grid">
      {metric_html}
    </section>
    <section class="brief-section-grid">
      {section_card("Key Customer Insights", key_customer_insights)}
      {section_card("Data Quality Issues", data_quality_issues)}
      {section_card("Campaign Opportunities", campaign_opportunities)}
      {section_card("Risks or Missing Information", risks_or_missing_information)}
    </section>
    <section class="panel brief-actions-panel">
      <div class="section-heading-row">
        <div>
          <h3>Recommended Next Actions</h3>
          <p class="muted">Use these as the manager review checklist before campaign or handoff work.</p>
        </div>
      </div>
      <ol class="brief-action-list">{action_html}</ol>
    </section>
    """
    return base_layout("AI Weekly Brief", body, flash=message, active_section="dashboard", user=user)


# Business logic now lives in crm/* modules. The render functions above stay in
# this file for now, but their late-bound globals are wired to the modular
# implementation before the HTTP handler starts serving requests.
from crm.ai import (  # noqa: E402
    AIError,
    api_key_from_settings,
    generate_campaign_draft,
    generate_capture_copy,
    generate_import_brief,
    generate_weekly_brief,
    is_configured as ai_is_configured,
    mask_secret,
    model_from_settings,
)
from crm.charts import (  # noqa: E402
    render_file_growth_chart,
    render_metric_bar_chart,
    render_qr_leaderboard_chart,
    render_source_donut_chart,
)
from crm.config import (  # noqa: E402
    ALIASES,
    BASE_DIR,
    CAMPAIGN_CHANNELS,
    CAMPAIGN_STATUSES,
    DATA_DIR,
    DB_PATH,
    DEFAULT_STAFF_PASSWORD,
    DEFAULT_STAFF_USERNAME,
    HOST,
    IMPORT_FIELD_GROUPS,
    IMPORT_FIELD_LABELS,
    IMPORT_SOURCE_GUIDES,
    MAX_UPLOAD_BYTES,
    OUTREACH_EVENT_TYPES,
    PENDING_IMPORTS,
    PENDING_IMPORT_TTL_SECONDS,
    PORT,
    PREFERRED_CHANNELS,
    PUBLIC_CAPTURE_TOUCHPOINTS,
    PUBLIC_CAPTURE_VARIANTS,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    SESSION_SECRET,
    STATIC_ASSET_CACHE,
    TASK_TYPES,
    TOUCHPOINT_TYPES,
    UPLOADS_DIR,
)
from crm.customers import (  # noqa: E402
    add_customer_note,
    build_name_location_match_value,
    customer_campaign_activity_with_conn,
    customer_duplicate_candidates_with_conn,
    customer_matches_segment_with_conn,
    customer_next_actions,
    customer_priority_key,
    customer_record_health_with_conn,
    customer_related_imports_with_conn,
    customer_relationship_summary,
    customer_tasks_with_conn,
    customer_timeline_with_conn,
    dismiss_duplicate_candidate,
    duplicate_candidate_rows_with_conn,
    duplicate_comparison_rows,
    duplicate_review_row_with_conn,
    get_customer,
    get_customer_record,
    get_customer_with_conn,
    list_customer_notes_with_conn,
    list_customers,
    merge_customer_records,
    merge_customer_records_with_conn,
    pair_bounds,
    primary_record_reason,
    probable_duplicate_snapshot_with_conn,
    save_customer,
    save_duplicate_review_with_conn,
    select_primary_customer,
    upsert_customer,
    upsert_customer_record,
)
from crm.db import db_connection, ensure_column, ensure_dirs, init_db, seed_demo_data  # noqa: E402
from crm.imports import (  # noqa: E402
    analyze_import_rows,
    active_import_run,
    asset_url,
    create_import_job_from_pending,
    delete_pending_import,
    export_segment_csv,
    get_import_run,
    import_identity_details,
    import_row_decision_with_conn,
    import_row_is_identifiable,
    import_rows,
    import_source_guides,
    load_pending_import,
    matched_preview_columns,
    pop_pending_import,
    process_import_job,
    prune_pending_imports,
    public_capture_page,
    public_capture_pages,
    qr_page_items,
    save_pending_import,
    save_upload,
    static_asset_bytes,
    value_for,
)
from crm.intelligence import (  # noqa: E402
    BUSINESS_SEGMENT_KEYS,
    business_audience_segments_with_conn,
    capture_gap_with_conn,
    freshline_cleanup_with_conn,
    import_summary_from_row,
    weekly_action_plan_with_conn,
)
from crm.labels import (  # noqa: E402
    acquisition_label,
    channel_label,
    customer_account_type,
    customer_context_labels,
    display_upload_name,
    message_query,
    option_list,
    outreach_event_label,
    source_system_label,
    status_pill,
    task_type_label,
    touchpoint_label,
)
from crm.marketing import (  # noqa: E402
    backfill_file_growth,
    campaign_export_block_message_with_conn,
    count_segment_rows_with_conn,
    create_campaign,
    create_touchpoint_capture,
    dashboard_action_queue_with_conn,
    dashboard_insights,
    dashboard_metrics,
    dashboard_metrics_with_conn,
    fetch_segment_rows,
    fetch_segment_rows_with_conn,
    lead_capture_snapshot_with_conn,
    log_outreach_event_with_conn,
    log_segment_export,
    mark_campaign_sent,
    marketing_focus,
    marketing_snapshot,
    marketing_snapshot_with_conn,
    recent_outreach_history_with_conn,
    record_weekly_snapshot,
    reporting_snapshot,
    results_snapshot_with_conn,
    run_campaign_attribution,
    segment_counts_with_conn,
    update_customers_last_contacted_for_segment_with_conn,
    weekly_playbook,
)
from crm.segments import segment_definitions  # noqa: E402
from crm.settings_store import get_app_settings, save_app_settings  # noqa: E402
from crm.tasks import (  # noqa: E402
    complete_task,
    create_task,
    latest_task_refresh_run_with_conn,
    list_tasks,
    list_tasks_with_conn,
    refresh_task_recommendations,
    task_counts_with_conn,
)
from crm.utils import (  # noqa: E402
    display_name,
    display_timestamp,
    infer_preferred_channel,
    max_timestamp,
    merge_list_text,
    merge_notes,
    normalize_header,
    normalize_phone,
    parse_csv_bytes,
    parse_table_bytes,
    parse_datetime,
    parsed_timestamp,
    preview_columns,
    preview_rows,
    split_name,
    to_bool,
    to_float,
    to_int,
    utc_now,
    validate_email,
    yes_no,
)

try:  # QR generation is optional at import time, but the demo route reports clearly if unavailable.
    import qrcode  # type: ignore[import-not-found]  # noqa: E402
except ImportError:  # pragma: no cover - exercised only when dependency is missing locally.
    qrcode = None
    print("Warning: qrcode library not installed; QR PNG downloads will be unavailable.", flush=True)


IMPORT_JOB_THREADS: dict[int, threading.Thread] = {}
IMPORT_JOB_THREADS_LOCK = threading.Lock()


def start_import_worker(import_run_id: int) -> None:
    with IMPORT_JOB_THREADS_LOCK:
        existing = IMPORT_JOB_THREADS.get(import_run_id)
        if existing and existing.is_alive():
            return

        def worker() -> None:
            try:
                result = process_import_job(import_run_id)
                if not result.get("error_message"):
                    refresh_task_recommendations("import_completed")
            except Exception:
                logger.exception("Background import worker crashed for run_id=%s", import_run_id)
            finally:
                with IMPORT_JOB_THREADS_LOCK:
                    IMPORT_JOB_THREADS.pop(import_run_id, None)

        thread = threading.Thread(
            target=worker,
            name=f"import-run-{import_run_id}",
            daemon=True,
        )
        IMPORT_JOB_THREADS[import_run_id] = thread
        thread.start()


def _register_qr_capture_variants() -> None:
    qr_variants = {}
    for key, location in QR_LOCATIONS.items():
        qr_variants[location["path"]] = {
            "label": f"{location['label']} QR",
            "headline": "Scan for fresh deals from Seaview Crab",
            "description": "Join Seaview updates in seconds. Leave an email or phone and we will send the next deal.",
            "touchpoint_type": location["touchpoint_type"],
            "preferred_channel": "either" if key in {"event", "wholesale"} else "sms",
            "cta": "Get the deal",
            "interest_placeholder": "weekly specials, crab boil, fresh catch",
            "capture_note": f"{location['label']} placement.",
            "location_tag": location["tag"],
            "benefits": [
                "Fast signup from a QR scan",
                "Tracks where signups come from",
            ],
        }
    PUBLIC_CAPTURE_VARIANTS.update(qr_variants)
    globals()["PUBLIC_CAPTURE_TOUCHPOINTS"] = tuple(
        page["touchpoint_type"] for page in PUBLIC_CAPTURE_VARIANTS.values()
    )
    try:
        import crm.config as _config_module  # noqa: PLC0415
        import crm.imports as _imports_module  # noqa: PLC0415
        import crm.marketing as _marketing_module  # noqa: PLC0415

        _config_module.PUBLIC_CAPTURE_VARIANTS.update(qr_variants)
        _config_module.PUBLIC_CAPTURE_TOUCHPOINTS = globals()["PUBLIC_CAPTURE_TOUCHPOINTS"]
        _imports_module.PUBLIC_CAPTURE_VARIANTS.update(qr_variants)
        _marketing_module.PUBLIC_CAPTURE_TOUCHPOINTS = globals()["PUBLIC_CAPTURE_TOUCHPOINTS"]
    except Exception:
        pass


_register_qr_capture_variants()


def ensure_runtime_schema() -> None:
    conn = db_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS staff_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'staff',
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_login_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS roi_settings (
                id INTEGER PRIMARY KEY,
                avg_order_value REAL NOT NULL DEFAULT 35.00,
                avg_visits_per_year REAL NOT NULL DEFAULT 4.0,
                avg_customer_lifespan_years REAL NOT NULL DEFAULT 3.0,
                slow_season_months TEXT NOT NULL DEFAULT '1,2,11,12',
                peak_season_months TEXT NOT NULL DEFAULT '5,6,7,8,9',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS file_growth_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL UNIQUE,
                total_customers INTEGER NOT NULL DEFAULT 0,
                reachable_customers INTEGER NOT NULL DEFAULT 0,
                campaign_ready INTEGER NOT NULL DEFAULT 0,
                qr_captures INTEGER NOT NULL DEFAULT 0,
                new_this_week INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaign_attribution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                attributed_at TEXT NOT NULL,
                attribution_type TEXT NOT NULL DEFAULT 'import_match',
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (customer_id) REFERENCES customers(id),
                UNIQUE(campaign_id, customer_id)
            );

            CREATE TABLE IF NOT EXISTS task_refresh_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_event TEXT NOT NULL,
                used_ai INTEGER NOT NULL DEFAULT 0,
                used_fallback INTEGER NOT NULL DEFAULT 0,
                tasks_created INTEGER NOT NULL DEFAULT 0,
                tasks_updated INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_imports (
                id TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                filename TEXT NOT NULL,
                rows_json TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                sample_rows_json TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                rows_count INTEGER NOT NULL DEFAULT 0,
                rows_storage TEXT NOT NULL DEFAULT 'inline',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_file_growth_week
                ON file_growth_snapshots(week_start DESC);

            CREATE INDEX IF NOT EXISTS idx_campaign_attribution_campaign
                ON campaign_attribution(campaign_id);

            CREATE INDEX IF NOT EXISTS idx_staff_users_username
                ON staff_users(username);

            CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
                ON audit_log(created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_task_refresh_runs_created_at
                ON task_refresh_runs(created_at);

            CREATE INDEX IF NOT EXISTS idx_pending_imports_created_at
                ON pending_imports(created_at);
            """
        )
        ensure_column(conn, "touchpoints", "scan_location", "TEXT")
        ensure_column(conn, "tasks", "priority", "TEXT DEFAULT 'medium'")
        ensure_column(conn, "tasks", "priority_score", "INTEGER DEFAULT 50")
        ensure_column(conn, "tasks", "source", "TEXT DEFAULT 'manual'")
        ensure_column(conn, "tasks", "ai_reason", "TEXT")
        ensure_column(conn, "tasks", "related_metric", "TEXT")
        ensure_column(conn, "tasks", "generated_from_event", "TEXT")
        ensure_column(conn, "tasks", "refreshed_at", "TEXT")
        ensure_column(conn, "import_runs", "rows_processed", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "import_runs", "pending_import_id", "TEXT")
        ensure_column(conn, "import_runs", "progress_message", "TEXT")
        ensure_column(conn, "import_runs", "progress_stage", "TEXT")
        ensure_column(conn, "import_runs", "started_at", "TEXT")
        ensure_column(conn, "import_runs", "last_progress_at", "TEXT")
        ensure_column(conn, "import_runs", "completed_at", "TEXT")
        ensure_column(conn, "pending_imports", "rows_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "pending_imports", "rows_storage", "TEXT NOT NULL DEFAULT 'inline'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source_status ON tasks(source, status, priority_score)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_import_runs_status_created_at ON import_runs(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_import_runs_pending_import_id ON import_runs(pending_import_id)")
        user_count = conn.execute(
            "SELECT COUNT(*) AS c FROM staff_users"
        ).fetchone()["c"]
        if not user_count:
            now = utc_now()
            conn.execute(
                """
                INSERT INTO staff_users
                (username, display_name, role, password_hash, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                ("seaview", "Owner", "admin", hash_password(DEFAULT_STAFF_PASSWORD), now, now),
            )
            conn.execute(
                """
                INSERT INTO staff_users
                (username, display_name, role, password_hash, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                ("staff", "Staff", "staff", hash_password("seaview-staff"), now, now),
            )

        settings_count = conn.execute(
            "SELECT COUNT(*) AS c FROM system_settings"
        ).fetchone()["c"]
        if not settings_count:
            now = utc_now()
            defaults = [
                ("business_name", "Seaview Crab Company"),
                ("sender_name", "Seaview Crab Company"),
                ("sender_email", ""),
                ("public_base_url", ""),
                ("openai_api_key", ""),
                ("openai_model", "gpt-4o-mini"),
                ("sendgrid_api_key", ""),
                ("twilio_account_sid", ""),
                ("twilio_auth_token", ""),
                ("twilio_phone_number", ""),
            ]
            for key, value in defaults:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO system_settings
                    (key, value, updated_at) VALUES (?, ?, ?)
                    """,
                    (key, value, now),
                )
        for key, value in [
            ("public_base_url", ""),
            ("openai_api_key", ""),
            ("openai_model", "gpt-4o-mini"),
        ]:
            conn.execute(
                """
                INSERT OR IGNORE INTO system_settings
                (key, value, updated_at) VALUES (?, ?, ?)
                """,
                (key, value, utc_now()),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO roi_settings (
                id, avg_order_value, avg_visits_per_year,
                avg_customer_lifespan_years, slow_season_months,
                peak_season_months, updated_at
            ) VALUES (1, 35.00, 4.0, 3.0, '1,2,11,12', '5,6,7,8,9', ?)
            """,
            (utc_now(),),
        )
        conn.commit()
    finally:
        conn.close()


_base_create_touchpoint_capture = create_touchpoint_capture


def create_touchpoint_capture(fields: dict, *, public_signup: bool = False) -> dict:
    result = _base_create_touchpoint_capture(fields, public_signup=public_signup)
    if result.get("error"):
        return result

    location_tag = fields.get("location_tag", "").strip()
    scan_location = fields.get("scan_location", "").strip()
    if not scan_location and location_tag.startswith("qr_"):
        scan_location = location_tag.removeprefix("qr_")
    if not scan_location and fields.get("source_label", "").strip().lower().endswith(" qr"):
        source_label = fields.get("source_label", "").strip().lower()
        for key, location in QR_LOCATIONS.items():
            if location["label"].lower() in source_label:
                scan_location = key
                location_tag = location["tag"]
                break
    if not scan_location and not location_tag:
        return result

    conn = db_connection()
    try:
        ensure_column(conn, "touchpoints", "scan_location", "TEXT")
        customer_id = result.get("customer_id")
        if customer_id:
            conn.execute(
                """
                UPDATE touchpoints
                SET scan_location = ?
                WHERE id = (
                    SELECT id
                    FROM touchpoints
                    WHERE customer_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                )
                """,
                (scan_location or None, customer_id),
            )
            if location_tag:
                customer = get_customer_record(customer_id, conn=conn)
                if customer:
                    conn.execute(
                        "UPDATE customers SET tags = ?, updated_at = ? WHERE id = ?",
                        (merge_list_text(customer["tags"] or "", location_tag), utc_now(), customer_id),
                    )
        conn.commit()
    finally:
        conn.close()
    return result


def list_customers(search: str = "", review_mode: str = "", filter_key: str = "") -> list[sqlite3.Row]:
    conn = db_connection()
    try:
        if review_mode == "duplicates":
            rows: list[sqlite3.Row] = []
            seen_ids: set[int] = set()
            for group in duplicate_candidate_rows_with_conn(conn):
                for customer in group["customers"]:
                    if customer["id"] in seen_ids:
                        continue
                    seen_ids.add(customer["id"])
                    rows.append(customer)
            return rows

        cutoff_30 = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
        cutoff_45 = (datetime.now(UTC) - timedelta(days=45)).replace(microsecond=0).isoformat()
        filter_clauses = {
            "email_ready": "COALESCE(email, '') <> ''",
            "needs_attention": "COALESCE(email, '') = '' AND COALESCE(phone, '') = ''",
            "vip": "total_spent >= 250",
            "lapsed": "last_purchase_at < ? AND total_spent > 0",
            "recent_buyers": "last_purchase_at >= ?",
        }
        where_clauses: list[str] = []
        params: list = []
        if filter_key in filter_clauses:
            where_clauses.append(filter_clauses[filter_key])
            if filter_key == "lapsed":
                params.append(cutoff_45)
            elif filter_key == "recent_buyers":
                params.append(cutoff_30)
        if search:
            like = f"%{search.lower()}%"
            where_clauses.append(
                """
                (
                    lower(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) LIKE ?
                    OR lower(COALESCE(email, '')) LIKE ?
                    OR lower(COALESCE(tags, '')) LIKE ?
                    OR lower(COALESCE(acquisition_source, '')) LIKE ?
                    OR lower(COALESCE(source_system, '')) LIKE ?
                )
                """
            )
            params.extend([like, like, like, like, like])
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        return conn.execute(
            f"SELECT * FROM customers {where_sql} ORDER BY updated_at DESC LIMIT 200",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()


class SeaviewCRMHandler(BaseHTTPRequestHandler):
    server_version = "SeaviewCRM/0.2"

    def is_authenticated(self) -> bool:
        return get_current_user(self.headers.get("Cookie")) is not None

    def current_user(self) -> dict | None:
        return get_current_user(self.headers.get("Cookie"))

    def request_is_secure(self) -> bool:
        return public_origin_for_request(self).lower().startswith("https://")

    def requires_auth(self, path: str) -> bool:
        if path == "/login":
            return False
        if path == "/healthz":
            return False
        if path.startswith("/static/"):
            return False
        if path in PUBLIC_CAPTURE_VARIANTS:
            return False
        return True

    def requires_admin(self, path: str) -> bool:
        return path.startswith("/admin")

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        prune_pending_imports()

        if self.requires_auth(parsed.path) and not self.is_authenticated():
            self.respond_redirect("/login?" + message_query("Staff login required."))
            return

        if parsed.path == "/healthz":
            status, payload = healthcheck_response()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if parsed.path == "/static/styles.css":
            payload = static_asset_bytes("static/styles.css")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if parsed.path == "/static/app.js":
            payload = static_asset_bytes("static/app.js")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD not supported for this path")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        message = params.get("message", [""])[0]
        prune_pending_imports()

        if self.requires_auth(parsed.path) and not self.is_authenticated():
            self.respond_redirect("/login?" + message_query("Staff login required."))
            return

        user = self.current_user()
        if self.requires_admin(parsed.path):
            if not user or user["role"] != "admin":
                self.respond_redirect("/?" + message_query("Admin access required."))
                return

        if parsed.path == "/login":
            if self.is_authenticated():
                self.respond_redirect("/")
                return
            self.respond_html(render_login(message))
            return
        if parsed.path == "/healthz":
            status, payload = healthcheck_response()
            self.respond_bytes(payload, "application/json; charset=utf-8", status=status, headers={"Cache-Control": "no-store"})
            return
        if parsed.path in PUBLIC_CAPTURE_VARIANTS:
            self.respond_html(render_public_capture(parsed.path, message))
            return
        if parsed.path == "/":
            self.respond_html(render_dashboard(message, user=user))
            return
        if parsed.path == "/reports":
            self.respond_html(render_reports(message, user=user))
            return
        if parsed.path == "/debug/counts":
            conn = db_connection()
            try:
                payload = {
                    "capture_gap": capture_gap_with_conn(conn),
                    "freshline_cleanup": {
                        key: value
                        for key, value in freshline_cleanup_with_conn(conn, limit=25).items()
                        if key.endswith("_total")
                    },
                    "freshline_segments": business_audience_segments_with_conn(conn),
                }
            finally:
                conn.close()
            encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.respond_bytes(encoded, "application/json; charset=utf-8")
            return
        if parsed.path == "/guide":
            self.respond_html(render_guide(message, user=user))
            return
        if parsed.path == "/qr-tools":
            self.respond_html(render_qr_tools(message, user=user))
            return
        if parsed.path == "/duplicates":
            self.respond_html(render_duplicate_review(message, user=user))
            return
        if parsed.path == "/customers":
            self.respond_html(render_customers(
                params.get("q", [""])[0],
                params.get("view", [""])[0],
                filter_key=params.get("filter", [""])[0],
                user=user,
            ))
            return
        if parsed.path == "/tasks":
            self.respond_html(render_tasks(message, user=user, filter_key=params.get("filter", ["all"])[0]))
            return
        if parsed.path == "/customers/new":
            self.respond_html(render_customer_form(message, user=user))
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
            self.respond_html(render_customer_form(message, customer=customer, user=user))
            return
        if parsed.path.startswith("/customers/"):
            try:
                customer_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.respond_not_found()
                return
            self.respond_html(render_customer_detail(customer_id, message, user=user))
            return
        if parsed.path == "/marketing/export":
            segment_key = params.get("segment", [""])[0]
            if segment_key not in segment_definitions():
                self.respond_not_found()
                return
            conn = db_connection()
            try:
                block_message = None if segment_key in BUSINESS_SEGMENT_KEYS else campaign_export_block_message_with_conn(conn)
            finally:
                conn.close()
            if block_message:
                self.respond_redirect("/duplicates?" + message_query(block_message))
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
            refresh_task_recommendations("campaign_exported")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            export_slugs = {
                "email_ready": "email-ready",
                "sms_ready": "text-ready",
                "clean_campaign_ready": "campaign-ready",
                "needs_attention": "needs-attention",
            }
            export_name = f"{export_slugs.get(segment_key, segment_key.replace('_', '-'))}-{datetime.now().date().isoformat()}.csv"
            self.send_header("Content-Disposition", f"attachment; filename={export_name}")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/marketing/export/preview":
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
            self.respond_html(render_campaign_export_preview(segment_key, campaign_id, message, user=user))
            return
        if parsed.path == "/marketing":
            conn = db_connection()
            try:
                snapshot_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM file_growth_snapshots"
                ).fetchone()["count"]
                if snapshot_count < 12:
                    backfill_file_growth(conn)
                record_weekly_snapshot(conn)
                run_campaign_attribution(conn)
                conn.commit()
            finally:
                conn.close()
            self.respond_html(render_marketing(message, user=user))
            return
        if parsed.path == "/admin":
            self.respond_html(render_admin_dashboard(user, message))
            return
        if parsed.path == "/admin/staff":
            self.respond_html(render_admin_staff(user, message))
            return
        if parsed.path == "/admin/audit":
            self.respond_html(render_admin_audit(user, message))
            return
        if parsed.path == "/settings":
            self.respond_html(render_settings(message, user=user))
            return
        if parsed.path == "/imports/ai-brief":
            self.respond_html(render_imports(message, params.get("summary", [""])[0], user=user))
            return
        if parsed.path == "/imports":
            self.respond_html(render_imports(message, params.get("summary", [""])[0], user=user))
            return
        if parsed.path.startswith("/imports/runs/") and parsed.path.endswith("/status.json"):
            try:
                import_run_id = int(parsed.path.split("/")[-2])
            except (IndexError, ValueError):
                self.respond_not_found()
                return
            run = get_import_run(import_run_id)
            if run and run["status"] == "queued":
                start_import_worker(import_run_id)
            payload = import_run_status_payload(import_run_id)
            if not payload:
                self.respond_bytes(
                    json.dumps({"ok": False, "error": "Import job not found."}).encode("utf-8"),
                    "application/json; charset=utf-8",
                    status=HTTPStatus.NOT_FOUND,
                    headers={"Cache-Control": "no-store"},
                )
                return
            self.respond_bytes(
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
            return
        if parsed.path.startswith("/imports/runs/"):
            try:
                import_run_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.respond_not_found()
                return
            run = get_import_run(import_run_id)
            if run and run["status"] == "queued":
                start_import_worker(import_run_id)
            self.respond_html(render_import_run_status(import_run_id, message, user=user))
            return
        if parsed.path.startswith("/imports/preview/"):
            import_id = parsed.path.rsplit("/", 1)[-1]
            self.respond_html(render_import_preview(import_id, message, user=user))
            return
        if parsed.path == "/capture/qr/generate":
            target = qr_target_from_params(self, params)
            if not target:
                self.respond_not_found()
                return
            qr_url = target["url"]
            if qrcode is None:
                error_msg = b"qrcode library not installed. Run: pip install qrcode[pil]"
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(error_msg)))
                self.end_headers()
                self.wfile.write(error_msg)
                return
            try:
                qr = qrcode.QRCode(version=1, box_size=10, border=4)
                qr.add_data(qr_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                payload = buf.getvalue()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", f"attachment; filename=seaview-qr-{target['slug']}.png")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                error_msg = b"Unable to generate QR code."
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(error_msg)))
                self.end_headers()
                self.wfile.write(error_msg)
            return
        if parsed.path == "/capture/qr/preview":
            target = qr_target_from_params(self, params)
            if not target:
                self.respond_not_found()
                return
            qr_url = target["url"]
            img_query = urlencode({"page": target["path"]})
            styles_href = asset_url("static/styles.css")
            preview_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QR: {escape(target['label'])}</title>
  <link rel="stylesheet" href="{escape(styles_href)}">
</head>
<body class="qr-preview-body">
  <main class="qr-preview-shell">
    <div class="no-print qr-preview-intro">
      <span class="eyebrow">Customer signup QR</span>
      <h1>{escape(target['label'])}</h1>
      <p>Scan this code with a phone to open the customer signup form for name, email, and phone capture.</p>
    </div>
    <section class="qr-print-zone">
      <img src="/capture/qr/generate?{escape(img_query)}" alt="Seaview QR Code - {escape(target['label'])}" width="280" height="280">
      <div class="qr-print-label">Scan for Seaview Deals</div>
      <div class="qr-print-label">{escape(target['label'])}</div>
      <div class="qr-print-url">{escape(qr_url)}</div>
    </section>
    <div class="no-print qr-preview-actions">
      <a href="{escape(qr_url)}" class="button secondary">Open signup form</a>
      <button onclick="window.print()" class="button">Print this QR code</button>
      <a href="/capture/qr/generate?{escape(img_query)}" download="seaview-qr-{escape(target['slug'])}.png" class="button secondary">Download PNG</a>
      <a href="/capture" class="button secondary">&larr; Back to Capture</a>
    </div>
  </main>
</body>
</html>""".encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(preview_html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(preview_html)
            return
        if parsed.path in {"/signup", "/capture"}:
            self.respond_html(render_signup(message, user=user))
            return
        if parsed.path == "/logout":
            if user:
                conn = db_connection()
                try:
                    log_audit(
                        conn,
                        user["id"],
                        user["username"],
                        "logout",
                        "Staff signed out",
                        self.client_address[0] if getattr(self, "client_address", None) else "",
                    )
                    conn.commit()
                finally:
                    conn.close()
            self.respond_redirect(
                "/login?" + message_query("You have been signed out."),
                headers={"Set-Cookie": clear_session_cookie_value(secure=self.request_is_secure())},
            )
            return
        if parsed.path == "/static/styles.css":
            css = static_asset_bytes("static/styles.css")
            self.respond_bytes(css, "text/css; charset=utf-8", headers={"Cache-Control": "no-store"})
            return
        if parsed.path == "/static/app.js":
            js = static_asset_bytes("static/app.js")
            self.respond_bytes(js, "text/javascript; charset=utf-8", headers={"Cache-Control": "no-store"})
            return
        if parsed.path.startswith("/samples/"):
            sample_name = Path(parsed.path).name
            if sample_name != Path(parsed.path).name or not sample_name.endswith(".csv"):
                self.respond_not_found()
                return
            sample_path = f"samples/{sample_name}"
            try:
                payload = static_asset_bytes(sample_path)
            except FileNotFoundError:
                self.respond_not_found()
                return
            self.respond_bytes(
                payload,
                "text/csv; charset=utf-8",
                headers={
                    "Cache-Control": "public, max-age=300",
                    "Content-Disposition": f"attachment; filename={sample_name}",
                },
            )
            return
        self.respond_not_found()

    def do_POST(self) -> None:
        prune_pending_imports()
        if self.path == "/login":
            fields = self.parse_urlencoded()
            username = fields.get("username", "").strip()
            password = fields.get("password", "")

            conn = db_connection()
            try:
                user = conn.execute(
                    """
                    SELECT *
                    FROM staff_users
                    WHERE username = ?
                      AND is_active = 1
                    """,
                    (username,),
                ).fetchone()

                if not user or not verify_password(password, user["password_hash"]):
                    if not (
                        hmac.compare_digest(username, DEFAULT_STAFF_USERNAME)
                        and hmac.compare_digest(password, DEFAULT_STAFF_PASSWORD)
                    ):
                        self.respond_redirect("/login?" + message_query("Invalid staff credentials."))
                        return
                    user = conn.execute(
                        "SELECT * FROM staff_users WHERE username = ?",
                        (username,),
                    ).fetchone()
                    if not user:
                        conn.execute(
                            """
                            INSERT INTO staff_users
                            (username, display_name, role, password_hash, is_active, created_at, updated_at)
                            VALUES (?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                username,
                                "Owner",
                                "admin",
                                hash_password(password),
                                utc_now(),
                                utc_now(),
                            ),
                        )
                        conn.commit()
                        user = conn.execute(
                            "SELECT * FROM staff_users WHERE username = ?",
                            (username,),
                        ).fetchone()

                now = utc_now()
                conn.execute(
                    """
                    UPDATE staff_users
                    SET last_login_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, user["id"]),
                )
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "login",
                    "Successful login",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()

            cookie = auth_cookie_header_v2(user["id"], user["username"], user["role"], secure=self.request_is_secure())
            self.respond_redirect("/", headers={"Set-Cookie": cookie})
            return
        if self.path in PUBLIC_CAPTURE_VARIANTS:
            is_fetch = self.headers.get("X-Requested-With", "") == "fetch"
            fields = self.parse_urlencoded()
            page = public_capture_page(self.path)
            if page:
                fields["touchpoint_type"] = page["touchpoint_type"]
                if not fields.get("preferred_channel"):
                    fields["preferred_channel"] = page["preferred_channel"]
                if not fields.get("location_tag") and page.get("location_tag"):
                    fields["location_tag"] = page["location_tag"]
            result = create_touchpoint_capture(fields, public_signup=True)
            if is_fetch:
                if result["error"]:
                    payload = json.dumps({"success": False, "error": result["error"]}).encode("utf-8")
                else:
                    refresh_task_recommendations("public_capture_submitted")
                    payload = json.dumps({"success": True}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if result["error"]:
                self.respond_redirect(f"{self.path}?{message_query(result['error'])}")
                return
            refresh_task_recommendations("public_capture_submitted")
            if result["result_state"] == "new_customer":
                message_text = "Thanks. You are now on Seaview's update list."
            elif result["result_state"] == "existing_customer":
                message_text = "Thanks. Your Seaview profile was updated."
            else:
                message_text = "Thanks. Your signup was saved, but Seaview still needs a better contact method for follow-up."
            self.respond_redirect(f"{self.path}?{message_query(message_text)}")
            return

        if self.requires_auth(self.path) and not self.is_authenticated():
            self.respond_redirect("/login?" + message_query("Staff login required."))
            return

        user = self.current_user()
        if self.requires_admin(self.path):
            if not user or user["role"] != "admin":
                self.respond_redirect("/?" + message_query("Admin access required."))
                return

        if self.path == "/admin/settings":
            fields = self.parse_urlencoded()
            saveable = [
                "business_name",
                "sender_name",
                "sender_email",
                "public_base_url",
                "openai_api_key",
                "openai_model",
                "sendgrid_api_key",
                "twilio_account_sid",
                "twilio_auth_token",
                "twilio_phone_number",
            ]
            conn = db_connection()
            try:
                for key in saveable:
                    val = fields.get(key, "").strip()
                    if val and val != "Not configured":
                        set_setting(key, val)
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "settings_updated",
                    "System settings saved",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect("/admin?" + message_query("Settings saved."))
            return

        if self.path == "/admin/staff":
            fields = self.parse_urlencoded()
            username = fields.get("username", "").strip()
            display_name_value = fields.get("display_name", "").strip()
            password = fields.get("password", "").strip()
            role = fields.get("role", "staff").strip()
            if role not in ("admin", "staff"):
                role = "staff"
            if not username or not display_name_value or not password:
                self.respond_redirect("/admin/staff?" + message_query("All fields are required."))
                return
            conn = db_connection()
            try:
                existing = conn.execute(
                    "SELECT id FROM staff_users WHERE username = ?",
                    (username,),
                ).fetchone()
                if existing:
                    self.respond_redirect("/admin/staff?" + message_query("Username already exists."))
                    return
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO staff_users
                    (username, display_name, role, password_hash, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (username, display_name_value, role, hash_password(password), now, now),
                )
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "staff_created",
                    f"Created {role} account: {username}",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect(
                "/admin/staff?" + message_query(f"Staff account created for {display_name_value}.")
            )
            return

        if self.path.startswith("/admin/staff/") and self.path.endswith("/deactivate"):
            try:
                staff_id = int(self.path.split("/")[-2])
            except ValueError:
                self.respond_not_found()
                return
            conn = db_connection()
            try:
                target = conn.execute(
                    "SELECT * FROM staff_users WHERE id = ?",
                    (staff_id,),
                ).fetchone()
                if not target:
                    self.respond_redirect("/admin/staff?" + message_query("User not found."))
                    return
                if target["username"] == user["username"]:
                    self.respond_redirect(
                        "/admin/staff?" + message_query("Cannot deactivate your own account.")
                    )
                    return
                active_admins = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM staff_users
                    WHERE role = 'admin' AND is_active = 1
                    """
                ).fetchone()["c"]
                if target["role"] == "admin" and active_admins <= 1:
                    self.respond_redirect(
                        "/admin/staff?" + message_query("Cannot deactivate the last admin account.")
                    )
                    return
                conn.execute(
                    "UPDATE staff_users SET is_active = 0, updated_at = ? WHERE id = ?",
                    (utc_now(), staff_id),
                )
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "staff_deactivated",
                    f"Deactivated: {target['username']}",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect("/admin/staff?" + message_query("Account deactivated."))
            return

        if self.path.startswith("/admin/staff/") and self.path.endswith("/reset"):
            try:
                staff_id = int(self.path.split("/")[-2])
            except ValueError:
                self.respond_not_found()
                return
            fields = self.parse_urlencoded()
            new_password = fields.get("new_password", "").strip()
            if not new_password:
                self.respond_redirect("/admin/staff?" + message_query("Enter a new password."))
                return
            conn = db_connection()
            try:
                conn.execute(
                    "UPDATE staff_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (hash_password(new_password), utc_now(), staff_id),
                )
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "password_reset",
                    f"Password reset for user id {staff_id}",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect("/admin/staff?" + message_query("Password updated."))
            return

        if self.path == "/ai/weekly-brief":
            if not ai_is_configured(get_setting):
                self.respond_redirect("/admin?" + message_query("Add an OpenAI API key before generating an AI brief."))
                return
            conn = db_connection()
            try:
                context = ai_operating_context(conn)
            finally:
                conn.close()
            try:
                brief = generate_weekly_brief(
                    api_key=api_key_from_settings(get_setting),
                    model=model_from_settings(get_setting),
                    context=context,
                )
            except AIError as exc:
                self.respond_redirect("/?" + message_query(f"AI brief failed: {str(exc)[:180]}"))
                return
            conn = db_connection()
            try:
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "ai_weekly_brief_generated",
                    "Generated AI operating brief",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_html(render_ai_weekly_brief(brief, user))
            return

        if self.path == "/marketing/ai-campaign":
            if not ai_is_configured(get_setting):
                self.respond_redirect("/marketing?" + message_query("Add an OpenAI API key before generating campaigns.") + "#campaigns")
                return
            fields = self.parse_urlencoded()
            segments = segment_definitions()
            target_segment = fields.get("target_segment", "clean_campaign_ready").strip()
            if target_segment not in segments:
                target_segment = "clean_campaign_ready"
            channel = fields.get("channel", "email").strip() or "email"
            if channel not in {key for key, _ in CAMPAIGN_CHANNELS}:
                channel = "email"
            conn = db_connection()
            try:
                audience_count = count_segment_rows_with_conn(conn, target_segment, segments)
                context = ai_operating_context(
                    conn,
                    {
                        "task": "campaign_draft",
                        "target_segment": target_segment,
                        "target_segment_label": segments[target_segment]["label"],
                        "audience_count": audience_count,
                        "channel": channel,
                        "campaign_special": fields.get("campaign_special", "").strip(),
                    },
                )
            finally:
                conn.close()
            try:
                draft = generate_campaign_draft(
                    api_key=api_key_from_settings(get_setting),
                    model=model_from_settings(get_setting),
                    context=context,
                )
            except AIError as exc:
                self.respond_redirect("/marketing?" + message_query(f"AI campaign failed: {str(exc)[:180]}") + "#campaigns")
                return
            result = create_campaign(
                {
                    "title": str(draft.get("title") or "Seaview weekly special").strip(),
                    "offer_details": str(draft.get("offer_details") or "Send this week's Seaview offer to the selected audience.").strip(),
                    "goal": str(draft.get("goal") or "Drive repeat visits").strip(),
                    "target_segment": target_segment,
                    "channel": channel,
                    "status": "draft",
                }
            )
            if result["error"]:
                self.respond_redirect("/marketing?" + message_query(result["error"]) + "#campaigns")
                return
            conn = db_connection()
            try:
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "ai_campaign_draft_created",
                    f"{draft.get('title', 'Campaign')} · {result['audience_count']} recipients",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect(
                "/marketing?"
                + message_query(f"AI campaign draft saved for {result['audience_count']} customers.")
                + "#campaigns"
            )
            return

        if self.path == "/capture/ai-copy":
            if not ai_is_configured(get_setting):
                self.respond_redirect("/capture?" + message_query("Add an OpenAI API key before generating capture copy."))
                return
            fields = self.parse_urlencoded()
            conn = db_connection()
            try:
                context = ai_operating_context(
                    conn,
                    {
                        "task": "capture_page_copy",
                        "special": fields.get("special", "").strip(),
                    },
                )
            finally:
                conn.close()
            try:
                copy = generate_capture_copy(
                    api_key=api_key_from_settings(get_setting),
                    model=model_from_settings(get_setting),
                    context=context,
                )
            except AIError as exc:
                self.respond_redirect("/capture?" + message_query(f"AI capture copy failed: {str(exc)[:180]}"))
                return
            conn = db_connection()
            try:
                conn.execute(
                    """
                    UPDATE app_settings
                    SET primary_offer_hook = ?,
                        capture_prompt = ?,
                        default_capture_cta = ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        str(copy.get("primary_offer_hook") or "").strip()[:140],
                        str(copy.get("capture_prompt") or "").strip()[:260],
                        str(copy.get("default_capture_cta") or "Get the deal").strip()[:40],
                        utc_now(),
                    ),
                )
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "ai_capture_copy_updated",
                    "Updated QR capture-page copy",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect("/capture?" + message_query("AI capture page copy updated."))
            return

        if self.path == "/imports/ai-settings":
            if not user or user.get("role") != "admin":
                self.respond_redirect("/imports?" + message_query("Admin access required to save AI settings."))
                return
            fields = self.parse_urlencoded()
            api_key = fields.get("openai_api_key", "").strip()
            model = fields.get("openai_model", "").strip()
            conn = db_connection()
            try:
                if api_key:
                    set_setting("openai_api_key", api_key)
                if model:
                    set_setting("openai_model", model)
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "imports_ai_settings_updated",
                    "Updated OpenAI settings from Imports",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect("/imports?" + message_query("AI settings saved."))
            return

        if self.path == "/imports/ai-brief":
            if not ai_is_configured(get_setting):
                self.respond_redirect("/imports?" + message_query("Add an OpenAI API key before generating an import brief."))
                return
            fields = self.parse_urlencoded()
            summary_id = fields.get("summary_id", "").strip()
            brief_mode = fields.get("brief_mode", "save").strip().lower()
            if brief_mode not in {"save", "preview"}:
                brief_mode = "save"
            conn = db_connection()
            try:
                summary_row = None
                if summary_id.isdigit():
                    summary_row = conn.execute("SELECT * FROM import_runs WHERE id = ?", (int(summary_id),)).fetchone()
                if not summary_row:
                    summary_row = conn.execute(
                        """
                        SELECT *
                        FROM import_runs
                        WHERE COALESCE(intelligence_summary, '') <> ''
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """
                    ).fetchone()
                import_summary = import_summary_from_row(summary_row)
                if not import_summary:
                    self.respond_redirect("/imports?" + message_query("No import intelligence summary is available yet."))
                    return
                summary_id = str(summary_row["id"])
                context = ai_import_context(conn, import_summary, summary_row)
            finally:
                conn.close()
            try:
                brief = generate_import_brief(
                    api_key=api_key_from_settings(get_setting),
                    model=model_from_settings(get_setting),
                    context=context,
                )
            except AIError as exc:
                self.respond_html(
                    render_imports(
                        summary_id=summary_id,
                        user=user,
                        ai_error=f"AI import brief failed: {str(exc)[:180]}",
                    )
                )
                return
            if brief_mode == "save":
                persist_import_ai_brief(
                    int(summary_id),
                    brief,
                    user=user,
                    client_ip=self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                self.respond_redirect(
                    f"/imports/ai-brief?summary={summary_id}&" + message_query("AI brief saved to import history.")
                )
                return
            conn = db_connection()
            try:
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "ai_import_brief_generated",
                    f"Previewed AI brief for import {summary_id or 'latest'}",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_html(render_imports(summary_id=summary_id, user=user, ai_brief=brief, ai_saved=False))
            return

        if self.path == "/imports":
            self.handle_import_upload()
            return
        if self.path == "/settings":
            fields = self.parse_urlencoded()
            save_app_settings(fields)
            self.respond_redirect("/settings?" + message_query("Settings updated."))
            return
        if self.path == "/tasks/refresh":
            summary = refresh_task_recommendations("manual_refresh")
            self.respond_redirect("/tasks?" + message_query(task_refresh_message(summary)))
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
        if self.path == "/customers/duplicates/merge":
            fields = self.parse_urlencoded()
            try:
                primary_customer_id = int(fields.get("primary_customer_id", "0"))
                secondary_customer_id = int(fields.get("secondary_customer_id", "0"))
            except ValueError:
                self.respond_redirect("/duplicates?" + message_query("Duplicate pair not found."))
                return
            result = merge_customer_records(primary_customer_id, secondary_customer_id)
            if result["error"]:
                self.respond_redirect("/duplicates?" + message_query(result["error"]))
                return
            self.respond_redirect(
                f"/customers/{primary_customer_id}?" + message_query("Duplicate records merged into the primary profile.")
            )
            return
        if self.path == "/customers/duplicates/dismiss":
            fields = self.parse_urlencoded()
            try:
                primary_customer_id = int(fields.get("primary_customer_id", "0"))
                secondary_customer_id = int(fields.get("secondary_customer_id", "0"))
            except ValueError:
                self.respond_redirect("/duplicates?" + message_query("Duplicate pair not found."))
                return
            result = dismiss_duplicate_candidate(
                primary_customer_id,
                secondary_customer_id,
                fields.get("reason", ""),
                fields.get("match_value", ""),
            )
            if result["error"]:
                self.respond_redirect("/duplicates?" + message_query(result["error"]))
                return
            self.respond_redirect("/duplicates?" + message_query("Pair marked keep separate."))
            return
        if self.path == "/freshline/duplicates/flag":
            fields = self.parse_urlencoded()
            try:
                primary_customer_id = int(fields.get("primary_customer_id", "0"))
                secondary_customer_id = int(fields.get("secondary_customer_id", "0"))
            except ValueError:
                self.respond_redirect("/?" + message_query("Duplicate pair not found.") + "#cleanup-priorities")
                return
            decision = fields.get("decision", "merge_requested").strip()
            if decision not in {"keep_separate", "merge_requested"}:
                decision = "merge_requested"
            conn = db_connection()
            try:
                primary = get_customer_record(primary_customer_id, conn=conn)
                secondary = get_customer_record(secondary_customer_id, conn=conn)
                if not primary or not secondary:
                    self.respond_redirect("/?" + message_query("Duplicate pair not found.") + "#cleanup-priorities")
                    return
                save_duplicate_review_with_conn(
                    conn,
                    customer_a_id=primary_customer_id,
                    customer_b_id=secondary_customer_id,
                    decision=decision,
                    primary_customer_id=primary_customer_id,
                    secondary_customer_id=secondary_customer_id,
                    reason="Freshline duplicate-name review",
                    match_value=display_name(primary),
                )
                if decision == "merge_requested":
                    for customer in (primary, secondary):
                        conn.execute(
                            "UPDATE customers SET tags = ?, updated_at = ? WHERE id = ?",
                            (
                                merge_list_text(customer["tags"] or "", "duplicate_flagged"),
                                utc_now(),
                                customer["id"],
                            ),
                        )
                conn.commit()
            finally:
                conn.close()
            message_text = "Duplicate pair marked keep both." if decision == "keep_separate" else "Duplicate pair flagged for merge review."
            self.respond_redirect("/?" + message_query(message_text) + "#cleanup-priorities")
            return
        if self.path == "/imports/confirm":
            fields = self.parse_urlencoded()
            import_id = fields.get("import_id", "")
            pending = load_pending_import(import_id, include_rows=False)
            if not pending:
                self.respond_redirect("/imports?" + message_query("Import preview expired. Upload the file again."))
                return
            analysis = pending.get("analysis")
            if not analysis:
                pending_with_rows = load_pending_import(import_id)
                if not pending_with_rows:
                    self.respond_redirect("/imports?" + message_query("Import preview expired. Upload the file again."))
                    return
                pending = pending_with_rows
                analysis = analyze_import_rows(pending["source_system"], pending["rows"])
            if not analysis["can_import"]:
                self.respond_redirect(f"/imports/preview/{import_id}?" + message_query("This file needs usable identity columns before it can be imported."))
                return
            job = create_import_job_from_pending(import_id)
            if job.get("error"):
                self.respond_redirect("/imports?" + message_query(str(job["error"])))
                return
            import_run_id = int(job["import_run_id"])
            rows_count = int(pending.get("rows_count") or len(pending.get("rows") or []))
            conn = db_connection()
            try:
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "import_queued",
                    f"{rows_count} rows queued for background import",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
            start_import_worker(import_run_id)
            message = "Import started in the background. This page will update as rows are processed."
            if job.get("already_exists"):
                message = "This import is already running or completed. Opening its status page."
            self.respond_redirect(f"/imports/runs/{import_run_id}?" + message_query(message))
            return
        if self.path == "/imports/cancel":
            fields = self.parse_urlencoded()
            delete_pending_import(fields.get("import_id", ""))
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
        if self.path == "/marketing/roi-settings":
            fields = self.parse_urlencoded()
            avg_order_value = max(to_float(fields.get("avg_order_value", "")) or 0.0, 0.0)
            avg_visits_per_year = max(to_float(fields.get("avg_visits_per_year", "")) or 0.0, 0.0)
            avg_customer_lifespan_years = max(
                to_float(fields.get("avg_customer_lifespan_years", "")) or 0.0,
                0.0,
            )
            slow_season_months = fields.get("slow_season_months", "").strip() or "1,2,11,12"
            peak_season_months = fields.get("peak_season_months", "").strip() or "5,6,7,8,9"
            conn = db_connection()
            try:
                conn.execute(
                    """
                    UPDATE roi_settings
                    SET avg_order_value = ?,
                        avg_visits_per_year = ?,
                        avg_customer_lifespan_years = ?,
                        slow_season_months = ?,
                        peak_season_months = ?,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        avg_order_value,
                        avg_visits_per_year,
                        avg_customer_lifespan_years,
                        slow_season_months,
                        peak_season_months,
                        utc_now(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            self.respond_redirect("/marketing?" + message_query("Business inputs updated.") + "#results")
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
            conn = db_connection()
            try:
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "campaign_sent",
                    f"{result['title']} · {result['audience_count']} recipients",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
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
            refresh_task_recommendations("internal_capture_submitted")
            if result["result_state"] == "new_customer":
                message_text = "New customer created from this touchpoint."
            elif result["result_state"] == "existing_customer":
                message_text = "Existing customer updated from this touchpoint."
            else:
                message_text = "Touchpoint saved, but the contact is still incomplete."
            self.respond_redirect("/marketing?" + message_query(message_text))
            return
        if self.path == "/customers/new":
            fields = self.parse_urlencoded()
            result = save_customer(fields)
            if result["error"]:
                self.respond_redirect("/customers/new?" + message_query(result["error"]))
                return
            conn = db_connection()
            try:
                log_audit(
                    conn,
                    user["id"],
                    user["username"],
                    "customer_created",
                    f"Customer id {result['customer_id']}",
                    self.client_address[0] if getattr(self, "client_address", None) else "",
                )
                conn.commit()
            finally:
                conn.close()
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
            refresh_task_recommendations("internal_capture_submitted")
            if result["result_state"] == "new_customer":
                message_text = "New customer created from this capture."
            elif result["result_state"] == "existing_customer":
                message_text = "Existing customer updated with this capture."
            else:
                message_text = "Capture saved, but the contact still needs better follow-up details."
            self.respond_redirect("/capture?" + message_query(message_text))
            return
        self.respond_not_found()

    def handle_import_upload(self) -> None:
        running_import = active_import_run()
        if running_import:
            self.respond_redirect(
                f"/imports/runs/{running_import['id']}?"
                + message_query("Another import is already running. Finish that import before uploading a new file.")
            )
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.respond_redirect("/imports?" + message_query("Upload failed: expected multipart form data."))
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.respond_redirect("/imports?" + message_query("Upload failed: invalid upload size header."))
            return
        if content_length <= 0:
            self.respond_redirect("/imports?" + message_query("Upload failed: empty upload."))
            return
        if content_length > MAX_UPLOAD_BYTES:
            self.respond_redirect("/imports?" + message_query("Upload failed: import files must be 10 MB or smaller."))
            return

        form = self.parse_multipart()
        source_system = (form["fields"].get("source_system") or "legacy_csv").strip()
        file_part = form["files"].get("csv_file")
        if not file_part or not file_part["content"]:
            self.respond_redirect("/imports?" + message_query("Choose a CSV or Excel file before importing."))
            return
        filename = file_part["filename"] or "upload.csv"
        if not filename.lower().endswith((".csv", ".xlsx")):
            self.respond_redirect("/imports?" + message_query("Upload failed: choose a .csv or .xlsx file."))
            return
        if len(file_part["content"]) > MAX_UPLOAD_BYTES:
            self.respond_redirect("/imports?" + message_query("Upload failed: import files must be 10 MB or smaller."))
            return

        try:
            rows = parse_table_bytes(filename, file_part["content"])
        except Exception:
            self.respond_redirect("/imports?" + message_query("Upload failed: file could not be parsed as CSV or Excel."))
            return
        detected_headers = {normalize_header(column) for column in preview_columns(rows)}
        if {"email", "name", "phone", "created_at"}.issubset(detected_headers):
            source_system = "freshline_customer_export"
        elif {"customer_id", "marketing_allowed", "customer_since"}.issubset(detected_headers):
            source_system = "clover" if source_system == "legacy_csv" else source_system
        analysis = analyze_import_rows(source_system, rows)
        filename = save_upload(filename, file_part["content"])
        import_id = uuid.uuid4().hex
        save_pending_import(
            import_id,
            source_system=source_system,
            filename=filename,
            rows=rows,
            analysis=analysis,
            sample_rows=preview_rows(rows),
        )
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
        self.respond_html(
            base_layout(
                "Not Found",
                """
                <div class='panel'>
                  <h2>Page not found</h2>
                  <p class='muted'>That page is not part of the Seaview CRM workspace.</p>
                  <a class='button secondary small' href='/'>Back to dashboard</a>
                </div>
                """,
            ),
            status=HTTPStatus.NOT_FOUND,
        )

    def log_message(self, format: str, *args) -> None:
        return


def run() -> None:
    init_db()
    ensure_runtime_schema()
    seed_demo_data()
    server = ThreadingHTTPServer((HOST, PORT), SeaviewCRMHandler)
    print(f"Database: {DB_PATH}")
    print(f"Uploads: {UPLOADS_DIR}")
    if not DATA_DIR.exists():
        print(f"WARNING: DATA_DIR {DATA_DIR} does not exist")
    elif not os.access(DATA_DIR, os.W_OK):
        print(f"WARNING: DATA_DIR {DATA_DIR} is not writable")
    print(f"Seaview CRM running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
