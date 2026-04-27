import csv
import io
import logging
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from crm.config import (
    ALIASES,
    BASE_DIR,
    IMPORT_FIELD_GROUPS,
    IMPORT_FIELD_LABELS,
    IMPORT_SOURCE_GUIDES,
    PENDING_IMPORT_TTL_SECONDS,
    PENDING_IMPORTS,
    PUBLIC_CAPTURE_VARIANTS,
    STATIC_ASSET_CACHE,
    UPLOADS_DIR,
)
from crm.utils import (
    normalize_header,
    normalize_phone,
    parse_csv_bytes,
    preview_columns,
    preview_rows,
    split_name,
    to_bool,
    to_float,
    to_int,
    utc_now,
    validate_email,
    parsed_timestamp,
    build_name_location_match_value,
)

logger = logging.getLogger(__name__)

def value_for(row: dict, logical_name: str) -> str:
    aliases = ALIASES[logical_name]
    for key, value in row.items():
        if normalize_header(key) in aliases and value:
            return value.strip()
    return ""


def matched_preview_columns(columns: list[str], logical_name: str) -> list[str]:
    aliases = ALIASES[logical_name]
    return [column for column in columns if normalize_header(column) in aliases]


# ── Import row analysis ───────────────────────────────────────────────────────

def import_row_is_identifiable(row: dict) -> bool:
    first_name = value_for(row, "first_name")
    last_name = value_for(row, "last_name")
    full_name = value_for(row, "full_name")
    return any([
        value_for(row, "external_id"),
        value_for(row, "email"),
        value_for(row, "phone"),
        full_name,
        first_name and last_name,
    ])


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


def import_row_has_usable_contact(row: dict) -> bool:
    email = value_for(row, "email").strip().lower()
    phone = normalize_phone(value_for(row, "phone").strip())
    return bool((email and validate_email(email)) or len(phone) == 10)


def import_row_has_name(row: dict) -> bool:
    identity = import_identity_details(row)
    return bool(identity["first_name"] or identity["last_name"])


def import_row_marketing_allowed(row: dict) -> bool:
    return bool(to_bool(value_for(row, "marketing_consent")))


def import_row_decision_with_conn(conn: sqlite3.Connection, source_system: str, row: dict) -> dict:
    if not import_row_is_identifiable(row):
        return {"outcome": "skip", "reason": "Missing identity", "match_value": ""}

    identity = import_identity_details(row)
    source_identity_only = source_system in {"clover", "seaview_customer_export"}
    source_scoped_identity = source_identity_only or source_system == "freshline_customer_export"

    if source_identity_only and identity["external_id"]:
        existing = conn.execute(
            "SELECT * FROM customers WHERE external_id = ? AND source_system = ?",
            (identity["external_id"], source_system),
        ).fetchone()
        if existing:
            return {"outcome": "merge", "reason": "External ID and source", "match_value": identity["external_id"], "customer": existing}
        return {"outcome": "create", "reason": "New source record", "match_value": identity["external_id"]}

    if identity["email"] and validate_email(identity["email"]):
        if source_scoped_identity:
            existing = conn.execute(
                "SELECT * FROM customers WHERE lower(email) = ? AND source_system = ?",
                (identity["email"], source_system),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT * FROM customers WHERE lower(email) = ?", (identity["email"],)
            ).fetchone()
        if existing:
            return {"outcome": "merge", "reason": "Exact email", "match_value": identity["email"], "customer": existing}

    if identity["phone"] and source_system != "freshline_customer_export":
        existing = conn.execute(
            "SELECT * FROM customers WHERE phone = ?", (identity["phone"],)
        ).fetchone()
        if existing:
            return {"outcome": "merge", "reason": "Normalized phone", "match_value": identity["phone"], "customer": existing}

    if identity["external_id"]:
        existing = conn.execute(
            "SELECT * FROM customers WHERE external_id = ? AND source_system = ?",
            (identity["external_id"], source_system),
        ).fetchone()
        if existing:
            return {"outcome": "merge", "reason": "External ID and source", "match_value": identity["external_id"], "customer": existing}

    normalized_name = " ".join(
        part for part in [identity["first_name"].lower(), identity["last_name"].lower()] if part
    ).strip()
    if normalized_name and (identity["city"] or identity["state"]):
        existing = conn.execute(
            """
            SELECT * FROM customers
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
                    identity["first_name"], identity["last_name"],
                    identity["city"], identity["state"],
                ),
                "customer": existing,
            }

    return {"outcome": "create", "reason": "New record", "match_value": ""}


def analyze_import_rows(source_system: str, rows: list[dict]) -> dict:
    from crm.db import db_connection

    columns = preview_columns(rows)
    mapped_fields: list[dict] = []
    for group_label, field_keys in IMPORT_FIELD_GROUPS:
        fields: list[dict] = []
        for field_key in field_keys:
            matched_columns = matched_preview_columns(columns, field_key)
            if not matched_columns:
                continue
            fields.append({
                "key": field_key,
                "label": IMPORT_FIELD_LABELS[field_key],
                "columns": matched_columns,
            })
        mapped_fields.append({"group": group_label, "fields": fields})

    unmapped_columns = [
        column for column in columns
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
    contactable_rows = sum(1 for row in rows if import_row_has_usable_contact(row))
    consent_rows = sum(1 for row in rows if import_row_marketing_allowed(row))
    campaign_ready_rows = sum(
        1 for row in rows if import_row_has_usable_contact(row) and import_row_marketing_allowed(row)
    )
    named_unreachable_rows = sum(
        1 for row in rows if import_row_has_name(row) and not import_row_has_usable_contact(row)
    )
    anonymous_rows = sum(1 for row in rows if not import_row_has_name(row))
    reachable_needs_consent_rows = sum(
        1 for row in rows if import_row_has_usable_contact(row) and not import_row_marketing_allowed(row)
    )
    valid_emails = [
        value_for(row, "email").strip().lower()
        for row in rows
        if value_for(row, "email").strip().lower()
        and validate_email(value_for(row, "email").strip().lower())
    ]
    valid_phones = [
        normalize_phone(value_for(row, "phone").strip())
        for row in rows
        if len(normalize_phone(value_for(row, "phone").strip())) == 10
    ]
    duplicate_email_values = sum(1 for _value, count in Counter(valid_emails).items() if count > 1)
    duplicate_phone_values = sum(1 for _value, count in Counter(valid_phones).items() if count > 1)
    purchase_rows = sum(
        1 for row in rows
        if value_for(row, "item_name") or value_for(row, "order_total") or value_for(row, "purchased_at")
    )
    skipped_rows = outcome_counts["skip"]

    warnings: list[str] = []
    blocking_warnings: list[str] = []
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
    elif rows:
        contact_rate = contactable_rows / len(rows)
        campaign_ready_rate = campaign_ready_rows / len(rows)
        if contact_rate < 0.25:
            warnings.append(
                f"Only {contactable_rows} of {len(rows)} rows include usable contact info. Treat this as a capture-gap import, not a campaign-ready list."
            )
        if campaign_ready_rate < 0.1:
            warnings.append(
                f"Only {campaign_ready_rows} rows are both reachable and marketing-allowed. Capture and consent should be the next workflow."
            )
    if named_unreachable_rows:
        warnings.append(
            f"{named_unreachable_rows} named rows are missing email or phone. These should become the checkout/counter capture priority."
        )
    if duplicate_email_values or duplicate_phone_values:
        warnings.append(
            f"The file has duplicate contact values: {duplicate_email_values} email group(s), {duplicate_phone_values} phone group(s)."
        )
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
        "campaign_ready_rows": campaign_ready_rows,
        "named_unreachable_rows": named_unreachable_rows,
        "anonymous_rows": anonymous_rows,
        "reachable_needs_consent_rows": reachable_needs_consent_rows,
        "duplicate_email_values": duplicate_email_values,
        "duplicate_phone_values": duplicate_phone_values,
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


def import_rows(source_system: str, filename: str, rows: list[dict]) -> dict:
    from crm.db import db_connection
    from crm.customers import upsert_customer, save_duplicate_review_with_conn
    from crm.intelligence import (
        build_import_intelligence_summary,
        build_source_aware_import_summary,
        customer_contact_state,
        customer_file_snapshot_with_conn,
        record_import_summary,
    )

    conn = db_connection()
    created = updated = review_needed = skipped = purchases = 0
    error_message = None
    import_run_id = None
    intelligence_summary = None
    touched_before: dict[int, dict] = {}
    touched_ids: set[int] = set()
    try:
        before_snapshot = customer_file_snapshot_with_conn(conn)
        for row in rows:
            decision = import_row_decision_with_conn(conn, source_system, row)
            if decision["outcome"] == "skip":
                skipped += 1
                continue
            if decision["outcome"] == "merge" and decision.get("customer"):
                existing_customer = decision["customer"]
                touched_before[existing_customer["id"]] = customer_contact_state(existing_customer)
            customer_id, action = upsert_customer(conn, source_system, row)
            touched_ids.add(customer_id)
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

        import_result = {
            "rows_received": len(rows),
            "customers_created": created,
            "customers_updated": updated,
            "review_needed_rows": review_needed,
            "skipped_rows": skipped,
            "purchase_events_created": purchases,
        }
        after_snapshot = customer_file_snapshot_with_conn(conn)
        touched_after = {}
        touched_id_list = list(touched_ids)
        for start_index in range(0, len(touched_id_list), 800):
            chunk = touched_id_list[start_index : start_index + 800]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(f"SELECT * FROM customers WHERE id IN ({placeholders})", tuple(chunk)).fetchall():
                touched_after[row["id"]] = row
        intelligence_summary = build_import_intelligence_summary(
            source_system=source_system,
            filename=filename,
            import_result=import_result,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            touched_before=touched_before,
            touched_after=touched_after,
        )
        intelligence_summary = build_source_aware_import_summary(conn, source_system, intelligence_summary)
        if intelligence_summary.get("source_metrics"):
            print(
                "[import-debug]",
                source_system,
                intelligence_summary.get("source_type"),
                intelligence_summary["source_metrics"],
                flush=True,
            )
        conn.execute(
            """
            INSERT INTO import_runs (
                source_system, filename, rows_received, customers_created, customers_updated,
                review_needed_rows, skipped_rows, purchase_events_created, intelligence_summary,
                status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_system, filename, len(rows), created, updated, review_needed, skipped,
                purchases, record_import_summary(intelligence_summary), "completed", None, utc_now(),
            ),
        )
        import_run_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
    except Exception as exc:
        logger.exception("Import failed for source=%s file=%s", source_system, filename)
        conn.rollback()
        error_message = str(exc)
        try:
            conn.execute(
                """
                INSERT INTO import_runs (
                    source_system, filename, rows_received, customers_created, customers_updated,
                    review_needed_rows, skipped_rows, purchase_events_created, status, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_system, filename, len(rows), created, updated, review_needed, skipped, purchases, "failed", error_message, utc_now()),
            )
            conn.commit()
        except Exception:
            logger.exception("Failed to log import failure")
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
        "import_run_id": import_run_id,
        "intelligence_summary": intelligence_summary,
    }


def list_import_runs(limit: int = 10) -> list[sqlite3.Row]:
    from crm.db import db_connection
    conn = db_connection()
    try:
        return conn.execute(
            "SELECT * FROM import_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


# ── File / static helpers ─────────────────────────────────────────────────────

def save_upload(filename: str, payload: bytes) -> str:
    safe_name = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{Path(filename).name or 'upload.csv'}"
    target = UPLOADS_DIR / safe_name
    target.write_bytes(payload)
    return safe_name


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


def prune_pending_imports(now: float | None = None) -> None:
    cutoff = (now or time.time()) - PENDING_IMPORT_TTL_SECONDS
    expired_ids = [
        import_id
        for import_id, pending in PENDING_IMPORTS.items()
        if pending.get("created_at_ts", 0) < cutoff
    ]
    for import_id in expired_ids:
        PENDING_IMPORTS.pop(import_id, None)


# ── Capture page helpers ──────────────────────────────────────────────────────

def public_capture_page(path: str) -> dict | None:
    page = PUBLIC_CAPTURE_VARIANTS.get(path)
    if not page:
        return None
    return {"path": path, **page}


def public_capture_pages() -> list[dict]:
    return [public_capture_page(path) for path in PUBLIC_CAPTURE_VARIANTS]  # type: ignore[misc]


def import_source_guides() -> list[dict]:
    ordered_keys = ["seaview_customer_export", "freshline_customer_export", "clover", "constant_contact", "legacy_csv"]
    return [{"key": key, **IMPORT_SOURCE_GUIDES[key]} for key in ordered_keys]


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
        pages.append({
            **page,
            "placement": placement_copy.get(page["path"], page["capture_note"]),
            "download_name": f"seaview-{page['touchpoint_type']}.png",
        })
    return pages


# ── Segment CSV export ────────────────────────────────────────────────────────

def export_segment_csv(segment_key: str) -> bytes:
    from crm.marketing import fetch_segment_rows

    rows = fetch_segment_rows(segment_key)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["First Name", "Last Name", "Email", "Phone", "Date Added"])

    def formatted_phone(phone: str) -> str:
        digits = "".join(ch for ch in (phone or "") if ch.isdigit())
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        return phone or ""

    for row in rows:
        writer.writerow([
            row["first_name"] or "",
            row["last_name"] or "",
            row["email"] or "",
            formatted_phone(row["phone"] or ""),
            row["customer_since"] or row["created_at"] or "",
        ])
    return output.getvalue().encode("utf-8")
