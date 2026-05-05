import csv
import io
import json
import logging
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
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
    parse_datetime,
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

ALIAS_TO_FIELDS: dict[str, tuple[str, ...]] = {}
for _field_name, _aliases in ALIASES.items():
    for _alias in _aliases:
        ALIAS_TO_FIELDS[_alias] = (*ALIAS_TO_FIELDS.get(_alias, ()), _field_name)


def _pending_import_cutoff(now: float | None = None) -> str:
    cutoff_ts = (now or time.time()) - PENDING_IMPORT_TTL_SECONDS
    return datetime.fromtimestamp(cutoff_ts, tz=UTC).replace(microsecond=0).isoformat()


def save_pending_import(
    import_id: str,
    *,
    source_system: str,
    filename: str,
    rows: list[dict],
    analysis: dict,
    columns: list[str] | None = None,
    sample_rows: list[dict] | None = None,
) -> None:
    payload = {
        "source_system": source_system,
        "filename": filename,
        "rows": rows,
        "columns": columns or analysis.get("columns") or preview_columns(rows),
        "sample_rows": sample_rows or preview_rows(rows),
        "analysis": analysis,
        "created_at_ts": time.time(),
    }
    PENDING_IMPORTS[import_id] = payload
    from crm.db import db_connection

    conn = db_connection()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_imports (
                id, source_system, filename, rows_json, columns_json,
                sample_rows_json, analysis_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                import_id,
                source_system,
                filename,
                json.dumps(rows, separators=(",", ":")),
                json.dumps(payload["columns"], separators=(",", ":")),
                json.dumps(payload["sample_rows"], separators=(",", ":")),
                json.dumps(analysis, separators=(",", ":")),
                utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_pending_import(import_id: str) -> dict | None:
    cached = PENDING_IMPORTS.get(import_id)
    if cached:
        return cached
    prune_pending_imports()
    from crm.db import db_connection

    conn = db_connection()
    try:
        row = conn.execute("SELECT * FROM pending_imports WHERE id = ?", (import_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = {
            "source_system": row["source_system"],
            "filename": row["filename"],
            "rows": json.loads(row["rows_json"]),
            "columns": json.loads(row["columns_json"]),
            "sample_rows": json.loads(row["sample_rows_json"]),
            "analysis": json.loads(row["analysis_json"]),
            "created_at_ts": parse_datetime(row["created_at"]).timestamp() if parse_datetime(row["created_at"]) else time.time(),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        delete_pending_import(import_id)
        return None
    PENDING_IMPORTS[import_id] = payload
    return payload


def delete_pending_import(import_id: str) -> None:
    PENDING_IMPORTS.pop(import_id, None)
    from crm.db import db_connection

    conn = db_connection()
    try:
        conn.execute("DELETE FROM pending_imports WHERE id = ?", (import_id,))
        conn.commit()
    finally:
        conn.close()


def pop_pending_import(import_id: str) -> dict | None:
    payload = load_pending_import(import_id)
    if not payload:
        return None
    delete_pending_import(import_id)
    return payload

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

def import_row_values(row: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, raw_value in row.items():
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if not value:
            continue
        for logical_name in ALIAS_TO_FIELDS.get(normalize_header(key), ()):
            values.setdefault(logical_name, value)
    return values


def import_values_are_identifiable(values: dict[str, str]) -> bool:
    first_name = values.get("first_name", "")
    last_name = values.get("last_name", "")
    return any([
        values.get("external_id"),
        values.get("email"),
        values.get("phone"),
        values.get("full_name"),
        first_name and last_name,
    ])


def import_identity_from_values(values: dict[str, str]) -> dict:
    first_name = values.get("first_name", "")
    last_name = values.get("last_name", "")
    if not first_name and not last_name:
        first_name, last_name = split_name(values.get("full_name", ""))
    return {
        "external_id": values.get("external_id", ""),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "email": values.get("email", "").strip().lower(),
        "phone": normalize_phone(values.get("phone", "").strip()),
        "city": values.get("city", "").strip(),
        "state": values.get("state", "").strip(),
    }


def import_row_is_identifiable(row: dict) -> bool:
    return import_values_are_identifiable(import_row_values(row))


def import_identity_details(row: dict) -> dict:
    return import_identity_from_values(import_row_values(row))


def import_row_has_usable_contact(row: dict) -> bool:
    identity = import_identity_details(row)
    return bool((identity["email"] and validate_email(identity["email"])) or len(identity["phone"]) == 10)


def import_row_has_name(row: dict) -> bool:
    identity = import_identity_details(row)
    return bool(identity["first_name"] or identity["last_name"])


def import_row_marketing_allowed(row: dict) -> bool:
    return bool(to_bool(import_row_values(row).get("marketing_consent", "")))


def compile_import_row(row: dict) -> dict:
    values = import_row_values(row)
    identity = import_identity_from_values(values)
    email = identity["email"]
    phone = identity["phone"]
    has_usable_contact = bool((email and validate_email(email)) or len(phone) == 10)
    return {
        "values": values,
        "identity": identity,
        "identifiable": import_values_are_identifiable(values),
        "has_usable_contact": has_usable_contact,
        "has_name": bool(identity["first_name"] or identity["last_name"]),
        "marketing_allowed": bool(to_bool(values.get("marketing_consent", ""))),
        "valid_email": email if email and validate_email(email) else "",
        "valid_phone": phone if len(phone) == 10 else "",
        "has_purchase": bool(values.get("item_name") or values.get("order_total") or values.get("purchased_at")),
    }


def _row_value(row: sqlite3.Row | dict, key: str, default=None):
    if isinstance(row, sqlite3.Row):
        try:
            return row[key]
        except (IndexError, KeyError):
            return default
    return row.get(key, default)


def _name_location_key(identity: dict) -> tuple[str, str, str] | None:
    normalized_name = " ".join(
        part for part in [
            (identity.get("first_name") or "").lower(),
            (identity.get("last_name") or "").lower(),
        ]
        if part
    ).strip()
    city = (identity.get("city") or "").lower()
    state = (identity.get("state") or "").lower()
    if normalized_name and (city or state):
        return normalized_name, city, state
    return None


def _minimal_customer_from_identity(customer_id: int, identity: dict, marketing_consent: int = 0) -> dict:
    return {
        "id": customer_id,
        "external_id": identity.get("external_id") or None,
        "source_system": identity.get("source_system") or "",
        "first_name": identity.get("first_name") or None,
        "last_name": identity.get("last_name") or None,
        "email": identity.get("email") or None,
        "phone": identity.get("phone") or None,
        "city": identity.get("city") or None,
        "state": identity.get("state") or None,
        "marketing_consent": marketing_consent,
    }


def _add_customer_to_match_cache(cache: dict, customer: sqlite3.Row | dict, source_system: str | None = None) -> None:
    customer_id = _row_value(customer, "id")
    if not customer_id:
        return

    customer_source = (source_system or _row_value(customer, "source_system") or "").strip()
    external_id = str(_row_value(customer, "external_id") or "").strip()
    email = str(_row_value(customer, "email") or "").strip().lower()
    phone = normalize_phone(str(_row_value(customer, "phone") or "").strip())
    identity = {
        "first_name": str(_row_value(customer, "first_name") or "").strip(),
        "last_name": str(_row_value(customer, "last_name") or "").strip(),
        "city": str(_row_value(customer, "city") or "").strip(),
        "state": str(_row_value(customer, "state") or "").strip(),
    }

    if customer_source and external_id:
        cache["source_external"].setdefault((customer_source, external_id), customer)
    if email:
        cache["email_global"].setdefault(email, customer)
        if customer_source:
            cache["email_source"].setdefault((customer_source, email), customer)
    if phone:
        cache["phone"].setdefault(phone, customer)
    key = _name_location_key(identity)
    if key:
        cache["name_location"].setdefault(key, customer)


def build_import_match_cache(conn: sqlite3.Connection, source_system: str) -> dict:
    cache = {
        "source_system": source_system,
        "source_identity_only": source_system in {"clover", "seaview_customer_export"},
        "source_scoped_identity": source_system in {"clover", "seaview_customer_export", "freshline_customer_export"},
        "source_external": {},
        "email_global": {},
        "email_source": {},
        "phone": {},
        "name_location": {},
    }
    rows = conn.execute(
        """
        SELECT id, external_id, source_system, first_name, last_name, email, phone,
               city, state, marketing_consent
        FROM customers
        ORDER BY updated_at DESC, id DESC
        """
    ).fetchall()
    for customer in rows:
        _add_customer_to_match_cache(cache, customer)
    return cache


def import_decision_from_identity(
    cache: dict,
    source_system: str,
    identity: dict,
    *,
    identifiable: bool = True,
) -> dict:
    if not identifiable:
        return {"outcome": "skip", "reason": "Missing identity", "match_value": ""}

    source_identity_only = cache.get("source_identity_only", source_system in {"clover", "seaview_customer_export"})
    source_scoped_identity = cache.get(
        "source_scoped_identity",
        source_identity_only or source_system == "freshline_customer_export",
    )

    if source_identity_only and identity["external_id"]:
        existing = cache["source_external"].get((source_system, identity["external_id"]))
        if existing:
            return {"outcome": "merge", "reason": "External ID and source", "match_value": identity["external_id"], "customer": existing}
        return {"outcome": "create", "reason": "New source record", "match_value": identity["external_id"]}

    if identity["email"] and validate_email(identity["email"]):
        if source_scoped_identity:
            existing = cache["email_source"].get((source_system, identity["email"]))
        else:
            existing = cache["email_global"].get(identity["email"])
        if existing:
            return {"outcome": "merge", "reason": "Exact email", "match_value": identity["email"], "customer": existing}

    if identity["phone"] and source_system != "freshline_customer_export":
        existing = cache["phone"].get(identity["phone"])
        if existing:
            return {"outcome": "merge", "reason": "Normalized phone", "match_value": identity["phone"], "customer": existing}

    if identity["external_id"]:
        existing = cache["source_external"].get((source_system, identity["external_id"]))
        if existing:
            return {"outcome": "merge", "reason": "External ID and source", "match_value": identity["external_id"], "customer": existing}

    key = _name_location_key(identity)
    if key:
        existing = cache["name_location"].get(key)
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


def import_row_decision_with_cache(cache: dict, source_system: str, row: dict) -> dict:
    values = import_row_values(row)
    return import_decision_from_identity(
        cache,
        source_system,
        import_identity_from_values(values),
        identifiable=import_values_are_identifiable(values),
    )


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
    contactable_rows = 0
    consent_rows = 0
    campaign_ready_rows = 0
    named_unreachable_rows = 0
    anonymous_rows = 0
    reachable_needs_consent_rows = 0
    valid_emails: list[str] = []
    valid_phones: list[str] = []
    purchase_rows = 0
    conn = db_connection()
    try:
        match_cache = build_import_match_cache(conn, source_system)
        for row in rows:
            compiled = compile_import_row(row)
            outcome = import_decision_from_identity(
                match_cache,
                source_system,
                compiled["identity"],
                identifiable=compiled["identifiable"],
            )["outcome"]
            outcome_counts[outcome] += 1
            if compiled["has_usable_contact"]:
                contactable_rows += 1
            if compiled["marketing_allowed"]:
                consent_rows += 1
            if compiled["has_usable_contact"] and compiled["marketing_allowed"]:
                campaign_ready_rows += 1
            if compiled["has_name"] and not compiled["has_usable_contact"]:
                named_unreachable_rows += 1
            if not compiled["has_name"]:
                anonymous_rows += 1
            if compiled["has_usable_contact"] and not compiled["marketing_allowed"]:
                reachable_needs_consent_rows += 1
            if compiled["valid_email"]:
                valid_emails.append(compiled["valid_email"])
            if compiled["valid_phone"]:
                valid_phones.append(compiled["valid_phone"])
            if compiled["has_purchase"]:
                purchase_rows += 1
    finally:
        conn.close()

    identifiable_rows = outcome_counts["create"] + outcome_counts["merge"] + outcome_counts["review"]
    duplicate_email_values = sum(1 for _value, count in Counter(valid_emails).items() if count > 1)
    duplicate_phone_values = sum(1 for _value, count in Counter(valid_phones).items() if count > 1)
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
        match_cache = build_import_match_cache(conn, source_system)
        for row in rows:
            compiled = compile_import_row(row)
            decision = import_decision_from_identity(
                match_cache,
                source_system,
                compiled["identity"],
                identifiable=compiled["identifiable"],
            )
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
            identity = compiled["identity"].copy()
            identity["source_system"] = source_system
            _add_customer_to_match_cache(
                match_cache,
                _minimal_customer_from_identity(
                    customer_id,
                    identity,
                    1 if compiled["marketing_allowed"] else 0,
                ),
                source_system,
            )
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
    from crm.db import db_connection

    conn = db_connection()
    try:
        conn.execute("DELETE FROM pending_imports WHERE created_at < ?", (_pending_import_cutoff(now),))
        conn.commit()
    finally:
        conn.close()


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
