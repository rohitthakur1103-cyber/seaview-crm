import logging
import sqlite3
from datetime import UTC, datetime

from crm.db import db_connection
from crm.labels import (
    acquisition_label,
    channel_label,
    customer_account_type,
    outreach_event_label,
    source_system_label,
    task_type_label,
    touchpoint_label,
)
from crm.segments import segment_definitions
from crm.utils import (
    build_name_location_match_value,
    display_name,
    display_timestamp,
    infer_preferred_channel,
    max_timestamp,
    merge_list_text,
    merge_notes,
    normalize_phone,
    parse_datetime,
    parsed_timestamp,
    to_float,
    to_int,
    utc_now,
    validate_email,
    yes_no,
)

logger = logging.getLogger(__name__)


# ── Duplicate pair helpers ────────────────────────────────────────────────────

def pair_bounds(customer_a_id: int, customer_b_id: int) -> tuple[int, int]:
    return tuple(sorted((customer_a_id, customer_b_id)))  # type: ignore[return-value]


def duplicate_review_row_with_conn(
    conn: sqlite3.Connection, customer_a_id: int, customer_b_id: int
) -> sqlite3.Row | None:
    low_id, high_id = pair_bounds(customer_a_id, customer_b_id)
    return conn.execute(
        "SELECT * FROM duplicate_reviews WHERE customer_low_id = ? AND customer_high_id = ?",
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
            low_id, high_id, primary_customer_id, secondary_customer_id,
            decision, reason or None, match_value or None, utc_now(),
        ),
    )


# ── Customer priority / selection ─────────────────────────────────────────────

def customer_priority_key(row: sqlite3.Row | dict) -> tuple:
    def _get(key: str):
        return row[key] if isinstance(row, sqlite3.Row) else row.get(key)

    return (
        1 if _get("email") else 0,
        1 if _get("phone") else 0,
        float(_get("total_spent") or 0),
        parse_datetime(_get("updated_at")) or datetime.min.replace(tzinfo=UTC),
        parse_datetime(_get("created_at")) or datetime.min.replace(tzinfo=UTC),
    )


def select_primary_customer(rows: list[sqlite3.Row]) -> sqlite3.Row:
    return sorted(rows, key=customer_priority_key, reverse=True)[0]


# ── Upsert helpers ────────────────────────────────────────────────────────────

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
    customer_since: str | None = None,
    preferred_channel: str = "",
    marketing_consent: int = 0,
    acquisition_source: str = "",
) -> tuple[int, str]:
    email = email.strip().strip('"').strip("'").lower()
    source_identity_only = source_system in {"clover", "seaview_customer_export"}
    source_scoped_identity = source_identity_only or source_system == "freshline_customer_export"
    if email and not validate_email(email) and not source_identity_only:
        logger.warning("Invalid email skipped during upsert: %s", email)
        email = ""
    phone = normalize_phone(phone.strip())
    now = utc_now()

    existing = None
    if source_identity_only and external_id:
        existing = conn.execute(
            "SELECT * FROM customers WHERE external_id = ? AND source_system = ?",
            (external_id, source_system),
        ).fetchone()
    if not existing and email and not source_identity_only:
        if source_scoped_identity:
            existing = conn.execute(
                "SELECT * FROM customers WHERE lower(email) = ? AND source_system = ?",
                (email, source_system),
            ).fetchone()
        else:
            existing = conn.execute("SELECT * FROM customers WHERE lower(email) = ?", (email,)).fetchone()
    if not existing and phone and source_system != "freshline_customer_export" and not source_identity_only:
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
            SET external_id = ?, first_name = ?, last_name = ?, email = ?, phone = ?,
                city = ?, state = ?, tags = ?, notes = ?, total_spent = ?,
                last_purchase_at = ?, preferred_channel = ?, marketing_consent = ?,
                acquisition_source = ?, customer_since = ?, updated_at = ?
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
                existing["customer_since"] or customer_since,
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
            acquisition_source, last_contacted_at, customer_since, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            external_id or None, source_system,
            first_name or None, last_name or None,
            email or None, phone or None,
            city or None, state or None,
            tags or None, notes or None,
            total_spent, last_purchase_at,
            inferred_channel, marketing_consent,
            acquisition_source or source_system, None, customer_since,
            now, now,
        ),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0], "created"


def upsert_customer(conn: sqlite3.Connection, source_system: str, row: dict) -> tuple[int, str]:
    from crm.imports import value_for  # local import avoids circular dep at module load

    first_name = value_for(row, "first_name")
    last_name = value_for(row, "last_name")
    if not first_name and not last_name:
        from crm.utils import split_name
        first_name, last_name = split_name(value_for(row, "full_name"))

    email = value_for(row, "email")
    phone = value_for(row, "phone")
    marketing_consent = to_bool_val(value_for(row, "marketing_consent"))
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
        customer_since=parsed_timestamp(value_for(row, "customer_since")),
        preferred_channel=value_for(row, "preferred_channel"),
        marketing_consent=marketing_consent,
        acquisition_source=source_system,
    )


def to_bool_val(value: str) -> int:
    return 1 if value.strip().lower() in {"1", "true", "yes", "y", "on", "subscribed"} else 0


# ── Customer queries ──────────────────────────────────────────────────────────

def list_customers(search: str = "", review_mode: str = "") -> list[sqlite3.Row]:
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
        contact_clause = "(COALESCE(email, '') <> '' OR COALESCE(phone, '') <> '')"
        named_clause = "(COALESCE(first_name, '') <> '' OR COALESCE(last_name, '') <> '')"
        view_clauses = {
            "reachable": contact_clause,
            "missing_contact": f"NOT {contact_clause}",
            "missing_email": "COALESCE(email, '') = ''",
            "missing_phone": "COALESCE(phone, '') = ''",
            "missing_name": f"NOT {named_clause}",
            "marketing_allowed": "marketing_consent = 1",
            "needs_consent": f"{contact_clause} AND marketing_consent = 0",
            "named_unreachable": f"{named_clause} AND NOT {contact_clause}",
            "customer_export": "source_system IN ('seaview_customer_export', 'freshline_customer_export', 'clover', 'legacy_csv')",
        }
        where_clauses: list[str] = []
        params: list = []
        if review_mode in view_clauses:
            where_clauses.append(view_clauses[review_mode])
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


def get_customer_with_conn(
    conn: sqlite3.Connection, customer_id: int
) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    events = conn.execute(
        "SELECT * FROM purchase_events WHERE customer_id = ? ORDER BY purchased_at DESC, created_at DESC",
        (customer_id,),
    ).fetchall()
    touchpoints = conn.execute(
        "SELECT * FROM touchpoints WHERE customer_id = ? ORDER BY created_at DESC",
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


def save_customer(fields: dict, customer_id: int | None = None) -> dict:
    email = fields.get("email", "").strip()
    phone = normalize_phone(fields.get("phone", "").strip())
    if not email and not phone:
        return {"error": "Add at least an email or phone number for the customer record."}
    if email and not validate_email(email):
        return {"error": "Enter a valid email address (e.g. name@example.com)."}

    conn = db_connection()
    try:
        existing = get_customer_record(customer_id, conn=conn) if customer_id else None
        if customer_id and not existing:
            return {"error": "Customer not found."}

        notes = fields.get("notes", "").strip()
        tags = fields.get("tags", "").strip()
        total_spent = to_float(fields.get("total_spent", "0"))
        last_purchase = (
            parsed_timestamp(fields.get("last_purchase_at", "").strip())
            if fields.get("last_purchase_at")
            else None
        )
        marketing_consent = 1 if fields.get("marketing_consent") else 0

        if existing:
            conn.execute(
                """
                UPDATE customers
                SET first_name = ?, last_name = ?, email = ?, phone = ?, city = ?, state = ?,
                    tags = ?, notes = ?, total_spent = ?, last_purchase_at = ?,
                    preferred_channel = ?, marketing_consent = ?, acquisition_source = ?, updated_at = ?
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

        new_id, _ = upsert_customer_record(
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
        return {"error": None, "customer_id": new_id, "action": "created"}
    finally:
        conn.close()


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
            "INSERT INTO customer_notes (customer_id, body, created_at) VALUES (?, ?, ?)",
            (customer_id, note, created_at),
        )
        conn.execute("UPDATE customers SET updated_at = ? WHERE id = ?", (created_at, customer_id))
        conn.commit()
        return {"error": None}
    finally:
        conn.close()


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
        SELECT * FROM customer_notes
        WHERE customer_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (customer_id, limit),
    ).fetchall()


def customer_matches_segment_with_conn(
    conn: sqlite3.Connection, customer_id: int, segment_key: str, segments: dict | None = None
) -> bool:
    active_segments = segments or segment_definitions()
    segment = active_segments.get(segment_key)
    if not segment:
        return False
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE id = ? AND {segment['where']}",
        (customer_id, *segment["params"]),
    ).fetchone()
    return bool(row["count"])


def customer_campaign_activity_with_conn(
    conn: sqlite3.Connection, customer_id: int, segments: dict | None = None, limit: int = 6
) -> list[sqlite3.Row]:
    active_segments = segments or segment_definitions()
    rows = conn.execute(
        """
        SELECT oh.*, c.title AS campaign_title, c.target_segment, c.channel AS campaign_channel
        FROM outreach_history oh
        LEFT JOIN campaigns c ON c.id = oh.campaign_id
        ORDER BY oh.created_at DESC, oh.id DESC
        LIMIT 40
        """
    ).fetchall()
    matches: list[sqlite3.Row] = []
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
        "SELECT * FROM import_runs WHERE source_system = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (customer["source_system"], limit),
    ).fetchall()


# ── Duplicate detection ───────────────────────────────────────────────────────

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
            "SELECT * FROM customers WHERE lower(email) = ? ORDER BY updated_at DESC, id DESC",
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
            "SELECT * FROM customers WHERE phone = ? ORDER BY updated_at DESC, id DESC",
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
        GROUP BY
            lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))),
            lower(COALESCE(city, '')),
            lower(COALESCE(state, ''))
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, match_value
        LIMIT 20
        """
    ).fetchall()
    for row in name_rows:
        customers = conn.execute(
            """
            SELECT * FROM customers
            WHERE lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) = ?
              AND lower(COALESCE(city, '')) = ?
              AND lower(COALESCE(state, '')) = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (row["match_value"], row["city"], row["state"]),
        ).fetchall()
        location = ", ".join(part.title() for part in [row["city"], row["state"]] if part)
        add_candidates(customers, "Same name and location", f"{row['match_value']} · {location}")

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            item["sort_reason"],
            -(item["primary"]["total_spent"] or 0),
            -(item["secondary"]["total_spent"] or 0),
            item["match_value"],
        ),
    )
    return ordered[:limit]


def probable_duplicate_snapshot_with_conn(conn: sqlite3.Connection) -> dict:
    candidates = duplicate_candidate_rows_with_conn(conn, limit=200)
    return {
        "email_groups": sum(1 for r in candidates if r["reason"] == "Shared email"),
        "phone_groups": sum(1 for r in candidates if r["reason"] == "Shared phone"),
        "name_groups": sum(1 for r in candidates if r["reason"] == "Same name and location"),
        "candidate_groups": len(candidates),
    }


def customer_duplicate_candidates_with_conn(
    conn: sqlite3.Connection, customer_id: int, limit: int = 4
) -> list[dict]:
    rows: list[dict] = []
    for candidate in duplicate_candidate_rows_with_conn(conn, limit=80):
        if candidate["primary"]["id"] == customer_id or candidate["secondary"]["id"] == customer_id:
            rows.append(candidate)
        if len(rows) >= limit:
            break
    return rows


# ── Merge / dismiss ───────────────────────────────────────────────────────────

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
        primary["notes"], secondary["notes"],
        f"Merged duplicate record from {display_name(secondary)}.",
    )

    for table in ("purchase_events", "touchpoints", "tasks", "customer_notes"):
        conn.execute(
            f"UPDATE {table} SET customer_id = ? WHERE customer_id = ?",
            (primary_customer_id, secondary_customer_id),
        )

    conn.execute(
        """
        UPDATE customers
        SET external_id = ?, source_system = ?, first_name = ?, last_name = ?,
            email = ?, phone = ?, city = ?, state = ?, tags = ?, notes = ?,
            total_spent = ?, last_purchase_at = ?, preferred_channel = ?,
            marketing_consent = ?, acquisition_source = ?, last_contacted_at = ?, updated_at = ?
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
        "INSERT INTO customer_notes (customer_id, body, created_at) VALUES (?, ?, ?)",
        (primary_customer_id, f"Merged duplicate record from {display_name(secondary)}.", now),
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
        conn.commit() if not result["error"] else conn.rollback()
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


# ── Customer profile helpers ──────────────────────────────────────────────────

def customer_record_health_with_conn(
    conn: sqlite3.Connection,
    customer: sqlite3.Row,
    touchpoints: list[sqlite3.Row],
    open_tasks_count: int,
) -> list[dict]:
    duplicate_candidates = customer_duplicate_candidates_with_conn(conn, customer["id"], limit=4)
    last_purchase = parse_datetime(customer["last_purchase_at"])
    if last_purchase:
        days = (datetime.now(UTC) - last_purchase).days
        recency_label = f"{days} day{'s' if days != 1 else ''} since purchase"
    else:
        recency_label = "No purchase history yet"
    return [
        {"label": "Account type", "value": customer_account_type(customer), "tone": "neutral"},
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
            "value": (
                f"{len(duplicate_candidates)} review candidate{'s' if len(duplicate_candidates) != 1 else ''}"
                if duplicate_candidates
                else "No open duplicate review"
            ),
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
    actions: list[str] = []
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
    summary: list[str] = [
        f"This record is currently treated as a {customer_account_type(customer).lower()} relationship."
    ]
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
        timeline.append({
            "kind": "profile", "label": "Profile", "title": "Profile context saved",
            "summary": customer["notes"], "meta": "Saved on the core customer record",
            "occurred_at": max_timestamp(customer["updated_at"], customer["created_at"]) or customer["created_at"],
        })

    for row in notes:
        timeline.append({
            "kind": "note", "label": "Note", "title": "Internal note added",
            "summary": row["body"], "meta": "Saved from the customer profile",
            "occurred_at": row["created_at"],
        })

    for row in events:
        total = f"${(row['order_total'] or 0):.2f}"
        timeline.append({
            "kind": "purchase", "label": "Purchase",
            "title": row["item_name"] or "Purchase recorded",
            "summary": f"{row['quantity'] or 0} item(s) | {total}",
            "meta": source_system_label(row["source_system"]),
            "occurred_at": row["purchased_at"] or row["created_at"],
        })

    for row in touchpoints:
        preferences = []
        if row["preferred_channel"]:
            preferences.append(channel_label(row["preferred_channel"]))
        preferences.append(f"Email {yes_no(row['consent_email'])}")
        preferences.append(f"SMS {yes_no(row['consent_sms'])}")
        timeline.append({
            "kind": "touchpoint", "label": "Capture",
            "title": touchpoint_label(row["touchpoint_type"]),
            "summary": row["summary"] or "Customer interaction captured.",
            "meta": " | ".join(preferences),
            "occurred_at": row["created_at"],
        })

    for row in tasks:
        task_meta = [task_type_label(row["task_type"])]
        if row["status"] == "completed":
            task_meta.append("Completed")
        elif row["due_at"]:
            task_meta.append(f"Due {display_timestamp(row['due_at'], include_time=False)}")
        else:
            task_meta.append("No due date")
        timeline.append({
            "kind": "task", "label": "Task", "title": row["title"],
            "summary": row["details"] or "Customer follow-up task",
            "meta": " | ".join(task_meta),
            "occurred_at": row["completed_at"] or row["due_at"] or row["created_at"],
        })

    for row in campaigns:
        segment_key = row["segment_key"] or row["target_segment"]
        segment = segment_definitions().get(segment_key, {})
        timeline.append({
            "kind": "campaign", "label": "Campaign",
            "title": f"{row['campaign_title'] or row['title']} · {outreach_event_label(row['event_type'])}",
            "summary": row["details"] or "Campaign activity matched this customer's current audience.",
            "meta": segment.get("label", segment_key.replace("_", " ").title()) if segment_key else "Campaign audience",
            "occurred_at": row["created_at"],
        })

    for row in related_imports:
        if row["status"] != "completed":
            continue
        import_summary = f"{row['customers_created']} created, {row['customers_updated']} updated"
        if row["skipped_rows"]:
            import_summary += f", {row['skipped_rows']} skipped"
        from crm.utils import display_upload_name
        timeline.append({
            "kind": "import", "label": "Import",
            "title": f"{source_system_label(row['source_system'])} sync activity",
            "summary": import_summary,
            "meta": display_upload_name(row["filename"]) or "Imported file",
            "occurred_at": row["created_at"],
        })

    timeline.sort(
        key=lambda item: parse_datetime(item["occurred_at"]) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return timeline[:limit], tasks, notes


# ── Duplicate display helpers ─────────────────────────────────────────────────

def primary_record_reason(primary: sqlite3.Row, secondary: sqlite3.Row) -> str:
    reasons: list[str] = []
    if primary["email"] and not secondary["email"]:
        reasons.append("has an email address")
    if primary["phone"] and not secondary["phone"]:
        reasons.append("has a phone number")
    if (primary["total_spent"] or 0) > (secondary["total_spent"] or 0):
        reasons.append("has higher recorded spend")
    p_updated = parse_datetime(primary["updated_at"])
    s_updated = parse_datetime(secondary["updated_at"])
    if p_updated and s_updated and p_updated > s_updated:
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
