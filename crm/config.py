import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "seaview_crm.db"

# ── Server / session ──────────────────────────────────────────────────────────
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_COOKIE_NAME = "seaview_session"
SESSION_MAX_AGE = 60 * 60 * 12
PENDING_IMPORT_TTL_SECONDS = 60 * 30
DEFAULT_STAFF_USERNAME = os.environ.get("SEAVIEW_CRM_USERNAME", "seaview")
DEFAULT_STAFF_PASSWORD = os.environ.get("SEAVIEW_CRM_PASSWORD", "crabshack-demo")
SESSION_SECRET = os.environ.get("SEAVIEW_SESSION_SECRET", "seaview-internal-demo-secret")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# ── Domain constants ──────────────────────────────────────────────────────────
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

IMPORT_FIELD_LABELS: dict[str, str] = {
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
    "customer_since": "Customer since",
    "preferred_channel": "Preferred channel",
    "marketing_consent": "Marketing consent",
    "postal_code": "Postal code",
}

IMPORT_FIELD_GROUPS: list[tuple[str, list[str]]] = [
    ("Identity", ["external_id", "email", "phone", "full_name", "first_name", "last_name"]),
    ("Marketing context", ["marketing_consent", "preferred_channel", "tags", "notes"]),
    ("Customer context", ["customer_since", "city", "state", "postal_code"]),
    ("Purchase context", ["order_total", "item_name", "quantity", "purchased_at"]),
]

ALIASES: dict[str, set[str]] = {
    "external_id": {"external_id", "customer_id", "id", "contact_id"},
    "first_name": {"first_name", "firstname", "first"},
    "last_name": {"last_name", "lastname", "last"},
    "full_name": {"name", "full_name", "customer_name"},
    "email": {"email", "email_address", "customer_email"},
    "phone": {"phone", "phone_number", "mobile", "customer_phone"},
    "city": {"city", "town"},
    "state": {"state", "province", "state_province", "state___province"},
    "postal_code": {"postal_code", "postal_zip_code", "postal___zip_code", "zip", "zip_code", "postal", "postcode"},
    "tags": {"tags", "segment", "segments", "group", "interests"},
    "notes": {"notes", "note", "comments"},
    "order_total": {"order_total", "total", "transaction_total", "amount_spent"},
    "item_name": {"item_name", "item", "product", "product_name"},
    "quantity": {"quantity", "qty"},
    "purchased_at": {"purchased_at", "purchase_date", "date", "last_order_date", "transaction_date"},
    "customer_since": {"customer_since", "first_seen_at", "customer_since_date", "created_at"},
    "preferred_channel": {"preferred_channel", "contact_channel"},
    "marketing_consent": {"marketing_consent", "marketing_allowed", "consent", "opt_in", "email_opt_in", "sms_opt_in"},
}

IMPORT_SOURCE_GUIDES: dict[str, dict] = {
    "seaview_customer_export": {
        "label": "Seaview customer export",
        "summary": "Best for loading the real customer export and measuring the contact-capture gap.",
        "steps": [
            "Upload the customer export with customer ID, name, phone, email, customer since, and marketing allowed.",
            "Use the preview to see how many records are reachable and campaign-ready.",
            "After import, use capture workflows to convert named-but-unreachable records into usable contacts.",
        ],
        "sample_file": "/samples/seaview_customer_export_anonymized.csv",
    },
    "freshline_customer_export": {
        "label": "Freshline customer export",
        "summary": "Best for Excel customer lists with email, name, phone, notes, and created-at timestamps.",
        "steps": [
            "Upload the Freshline customer spreadsheet or CSV export.",
            "Use the preview to confirm names, emails, phones, notes, and created-at dates are mapped.",
            "Treat this list as a reachability source, then capture consent before campaign export.",
        ],
        "sample_file": "/samples/freshline_customers_anonymized.csv",
    },
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

PUBLIC_CAPTURE_VARIANTS: dict[str, dict] = {
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
}

PUBLIC_CAPTURE_TOUCHPOINTS: tuple[str, ...] = tuple(
    page["touchpoint_type"] for page in PUBLIC_CAPTURE_VARIANTS.values()
)

# ── Shared mutable state (module-level singletons) ────────────────────────────
PENDING_IMPORTS: dict[str, dict] = {}
STATIC_ASSET_CACHE: dict[str, tuple[float, bytes]] = {}
