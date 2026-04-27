import sqlite3
from html import escape
from urllib.parse import urlencode

from crm.config import (
    CAMPAIGN_CHANNELS,
    OUTREACH_EVENT_TYPES,
    PREFERRED_CHANNELS,
    TASK_TYPES,
    TOUCHPOINT_TYPES,
)
from crm.utils import parse_datetime, display_upload_name  # re-export display_upload_name
from datetime import UTC, datetime


def touchpoint_label(value: str) -> str:
    return dict(TOUCHPOINT_TYPES).get(value, value.replace("_", " ").title())


def source_system_label(value: str | None) -> str:
    labels = {
        "clover": "Clover export",
        "seaview_customer_export": "Seaview customer export",
        "freshline_customer_export": "Freshline customer export",
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


def channel_label(value: str | None) -> str:
    labels = dict(PREFERRED_CHANNELS + CAMPAIGN_CHANNELS)
    return labels.get(value or "", (value or "").replace("_", " ").title())


def outreach_event_label(value: str | None) -> str:
    labels = dict(OUTREACH_EVENT_TYPES)
    return labels.get(value or "", (value or "").replace("_", " ").title())


def task_type_label(value: str) -> str:
    return dict(TASK_TYPES).get(value, value.replace("_", " ").title())


def status_pill(value: str | None, *, prefix: str = "status") -> str:
    raw = (value or "").strip().lower().replace(" ", "_")
    if not raw:
        return "—"
    label_source = outreach_event_label(value) if prefix == "event" else (value or "").replace("_", " ").title()
    label = escape(label_source)
    return f"<span class='status-pill {prefix}-{escape(raw)}'>{label}</span>"


def option_list(options: list[tuple[str, str]], selected: str = "") -> str:
    html = []
    for value, label in options:
        is_selected = " selected" if value == selected else ""
        html.append(f"<option value='{escape(value)}'{is_selected}>{escape(label)}</option>")
    return "".join(html)


def message_query(message: str) -> str:
    return urlencode({"message": message})


def customer_account_type(customer: sqlite3.Row) -> str:
    tags = (customer["tags"] or "").lower()
    acquisition_source = (customer["acquisition_source"] or "").lower()
    if "wholesale" in tags or acquisition_source == "wholesale_inquiry":
        return "Wholesale"
    return "Retail / consumer"


def customer_context_labels(customer: sqlite3.Row) -> list[str]:
    labels: list[str] = []
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
