import logging
import sqlite3

from crm.config import DATA_DIR, DB_PATH, UPLOADS_DIR
from crm.utils import utc_now

logger = logging.getLogger(__name__)


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
            customer_since TEXT,
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
            intelligence_summary TEXT,
            ai_brief_json TEXT,
            ai_brief_created_at TEXT,
            status TEXT NOT NULL,
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
            priority TEXT DEFAULT 'medium',
            priority_score INTEGER DEFAULT 50,
            source TEXT DEFAULT 'manual',
            ai_reason TEXT,
            related_metric TEXT,
            generated_from_event TEXT,
            refreshed_at TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
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
        CREATE INDEX IF NOT EXISTS idx_customers_source_external_id ON customers(source_system, external_id);
        CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);
        CREATE INDEX IF NOT EXISTS idx_customers_updated_at ON customers(updated_at);
        CREATE INDEX IF NOT EXISTS idx_customers_last_purchase_at ON customers(last_purchase_at);
        CREATE INDEX IF NOT EXISTS idx_purchase_events_customer_id ON purchase_events(customer_id);
        CREATE INDEX IF NOT EXISTS idx_purchase_events_customer_purchased_at ON purchase_events(customer_id, purchased_at);
        CREATE INDEX IF NOT EXISTS idx_touchpoints_customer_id ON touchpoints(customer_id);
        CREATE INDEX IF NOT EXISTS idx_touchpoints_customer_created_at ON touchpoints(customer_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_touchpoints_created_at ON touchpoints(created_at);
        CREATE INDEX IF NOT EXISTS idx_import_runs_created_at ON import_runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_pending_imports_created_at ON pending_imports(created_at);
        CREATE INDEX IF NOT EXISTS idx_campaigns_status_scheduled_for ON campaigns(status, scheduled_for, created_at);
        CREATE INDEX IF NOT EXISTS idx_outreach_history_created_at ON outreach_history(created_at);
        CREATE INDEX IF NOT EXISTS idx_outreach_history_campaign_id ON outreach_history(campaign_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_status_due_at ON tasks(status, due_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_customer_id ON tasks(customer_id);
        CREATE INDEX IF NOT EXISTS idx_task_refresh_runs_created_at ON task_refresh_runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_customer_notes_customer_id ON customer_notes(customer_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_duplicate_reviews_pair ON duplicate_reviews(customer_low_id, customer_high_id);
        """
    )

    ensure_column(conn, "customers", "preferred_channel", "TEXT")
    ensure_column(conn, "customers", "marketing_consent", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "customers", "acquisition_source", "TEXT")
    ensure_column(conn, "customers", "last_contacted_at", "TEXT")
    ensure_column(conn, "customers", "customer_since", "TEXT")
    ensure_column(conn, "import_runs", "review_needed_rows", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "import_runs", "skipped_rows", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "import_runs", "intelligence_summary", "TEXT")
    ensure_column(conn, "import_runs", "ai_brief_json", "TEXT")
    ensure_column(conn, "import_runs", "ai_brief_created_at", "TEXT")
    ensure_column(conn, "tasks", "priority", "TEXT DEFAULT 'medium'")
    ensure_column(conn, "tasks", "priority_score", "INTEGER DEFAULT 50")
    ensure_column(conn, "tasks", "source", "TEXT DEFAULT 'manual'")
    ensure_column(conn, "tasks", "ai_reason", "TEXT")
    ensure_column(conn, "tasks", "related_metric", "TEXT")
    ensure_column(conn, "tasks", "generated_from_event", "TEXT")
    ensure_column(conn, "tasks", "refreshed_at", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source_status ON tasks(source, status, priority_score)")
    ensure_column(conn, "app_settings", "preferred_primary_data_source", "TEXT")
    ensure_column(conn, "app_settings", "default_capture_cta", "TEXT")
    ensure_column(
        conn,
        "app_settings",
        "duplicate_review_required_before_campaign_export",
        "INTEGER NOT NULL DEFAULT 1",
    )

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
                "CC-1001", "constant_contact", "Maria", "Lopez",
                "maria@example.com", "910-555-0111", "Wilmington", "NC",
                "newsletter, retail, families",
                "Subscribed from seafood boil promo and responds to seasonal offers.",
                320.50, "2026-03-18T14:30:00", "email", 1, "newsletter_signup", None, now, now,
            ),
            (
                "CL-2002", "clover", "James", "Carter",
                "jcarter@example.com", "910-555-0144", "Leland", "NC",
                "repeat, wholesale",
                "High-value wholesale contact who orders trays regularly.",
                1450.00, "2026-03-20T09:15:00", "either", 1, "clover", None, now, now,
            ),
            (
                "LEG-3003", "legacy_csv", "Dana", "Holt",
                "dholt@example.com", "910-555-0177", "Carolina Beach", "NC",
                "legacy, lapsed",
                "Imported from an older spreadsheet that had not been reused.",
                0, None, "email", 0, "legacy_csv", None, now, now,
            ),
            (
                "FL-4004", "freshline_customer_export", "Evelyn", "Brooks",
                "evelyn.brooks@example.com", "9105550188", "Wilmington", "NC",
                "recent buyer, crab boil",
                "Bought weekend boil trays and opted into weekly specials.",
                188.75, "2026-04-24T17:45:00+00:00", "email", 1, "freshline_customer_export", None, now, now,
            ),
            (
                "FL-4005", "freshline_customer_export", "Marcus", "Reed",
                "marcus.reed@example.com", "", "Wrightsville Beach", "NC",
                "lapsed, family packs",
                "Used to buy family packs but has not purchased recently.",
                274.20, "2025-11-20T12:00:00+00:00", "email", 1, "freshline_customer_export", None, now, now,
            ),
            (
                "QR-5006", "touchpoint_capture", "Nina", "Patel",
                "nina.patel@example.com", "9105550199", "Wilmington", "NC",
                "captured lead, qr counter, shrimp",
                "Joined from the counter QR after asking about shrimp specials.",
                0, None, "either", 1, "in_store_qr", None, now, now,
            ),
            (
                "QR-5007", "touchpoint_capture", "Owen", "Miles",
                "", "9105550200", "Leland", "NC",
                "captured lead, receipt qr",
                "Receipt QR signup; prefers text updates.",
                0, None, "sms", 1, "receipt_qr", None, now, now,
            ),
            (
                "CL-6008", "clover", "Harbor", "Cafe",
                "orders@harborcafe.example.com", "9105550211", "Carolina Beach", "NC",
                "wholesale, vip, restaurant",
                "Potential wholesale account for oysters and crab trays.",
                2310.40, "2026-04-15T10:30:00+00:00", "either", 1, "clover", None, now, now,
            ),
            (
                "LEG-7009", "legacy_csv", "Sam", "Taylor",
                "", "", "Wilmington", "NC",
                "missing contact, event lead",
                "Name captured at an event but no usable contact route yet.",
                0, None, None, 0, "event_booth", None, now, now,
            ),
            (
                "DUP-8010", "legacy_csv", "James", "Carter",
                "james.carter.alt@example.com", "9105550144", "Leland", "NC",
                "possible duplicate, wholesale",
                "Alternate export row that should be reviewed against the wholesale James Carter record.",
                120.00, "2026-04-02T09:15:00+00:00", "either", 1, "legacy_csv", None, now, now,
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
            row["email"]: row["id"]
            for row in conn.execute("SELECT id, email FROM customers").fetchall()
        }
        conn.executemany(
            """
            INSERT INTO purchase_events (
                customer_id, source_system, item_name, quantity, order_total, purchased_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (customer_lookup["maria@example.com"], "clover", "Blue Crab Special", 2, 78.0, "2026-03-18T14:30:00", now),
                (customer_lookup["jcarter@example.com"], "clover", "Wholesale Oyster Tray", 10, 450.0, "2026-03-20T09:15:00", now),
                (customer_lookup["evelyn.brooks@example.com"], "freshline_customer_export", "Weekend Crab Boil", 1, 188.75, "2026-04-24T17:45:00+00:00", now),
                (customer_lookup["marcus.reed@example.com"], "freshline_customer_export", "Family Shrimp Pack", 2, 96.00, "2025-11-20T12:00:00+00:00", now),
                (customer_lookup["orders@harborcafe.example.com"], "clover", "Wholesale Oyster Tray", 8, 620.00, "2026-04-15T10:30:00+00:00", now),
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
        row["email"]: row["id"]
        for row in conn.execute("SELECT id, email FROM customers WHERE email IS NOT NULL").fetchall()
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
                    "email", 1, 0, now,
                ),
                (
                    customer_lookup["jcarter@example.com"],
                    "wholesale_inquiry",
                    "Requested pricing and seasonal inventory updates for wholesale trays.",
                    "either", 1, 1, now,
                ),
                (
                    customer_lookup["nina.patel@example.com"],
                    "in_store_qr",
                    "Counter QR signup for shrimp and crab specials.",
                    "either", 1, 1, now,
                ),
                (
                    customer_lookup[""],
                    "receipt_qr",
                    "Receipt QR signup for text updates.",
                    "sms", 0, 1, now,
                ) if "" in customer_lookup else (
                    customer_lookup["nina.patel@example.com"],
                    "receipt_qr",
                    "Receipt QR test capture for demo reporting.",
                    "sms", 0, 1, now,
                ),
            ],
        )

    duplicate_count = conn.execute("SELECT COUNT(*) AS count FROM duplicate_reviews").fetchone()["count"]
    if not duplicate_count:
        james = conn.execute("SELECT id FROM customers WHERE email = 'jcarter@example.com'").fetchone()
        james_alt = conn.execute("SELECT id FROM customers WHERE email = 'james.carter.alt@example.com'").fetchone()
        if james and james_alt:
            low_id, high_id = sorted([james["id"], james_alt["id"]])
            conn.execute(
                """
                INSERT OR IGNORE INTO duplicate_reviews (
                    customer_low_id, customer_high_id, primary_customer_id, secondary_customer_id,
                    decision, reason, match_value, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    low_id, high_id, james["id"], james_alt["id"],
                    "Same wholesale contact appears in Clover and legacy export.",
                    "James Carter · Leland NC", now,
                ),
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
                    "email", "recent_buyers", "Drive weekend repeat visits", "2026-03-28", "scheduled", 2, now,
                ),
                (
                    "Wholesale Restock Reminder",
                    "Follow up with wholesale accounts before the next inventory delivery window.",
                    "sms", "wholesale_accounts", "Increase wholesale reorder frequency", None, "draft", 1, now,
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
                seed_events.append((
                    row["id"], "saved", row["title"], row["channel"], row["target_segment"],
                    row["audience_count"], "Campaign saved from the marketing workspace.", now,
                ))
            if len(campaigns) > 1:
                second = campaigns[1]
                seed_events.append((
                    second["id"], "exported", second["title"], second["channel"],
                    second["target_segment"], second["audience_count"],
                    "Audience exported to CSV for outreach planning.", now,
                ))
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
                    "follow_up", "2026-04-05", "open", now, None,
                ),
                (
                    customer_lookup["jcarter@example.com"],
                    "Check wholesale reorder timing",
                    "Confirm expected reorder date before the next delivery window.",
                    "campaign", "2026-04-06", "open", now, None,
                ),
                (
                    None,
                    "Import latest Clover export",
                    "Refresh customer and purchase data before this week's outreach.",
                    "import", "2026-04-04", "open", now, None,
                ),
            ],
        )

    conn.commit()
    conn.close()
