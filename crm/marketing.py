import csv
import io
import sqlite3
from datetime import UTC, date, datetime, timedelta

from crm.config import CAMPAIGN_STATUSES, PUBLIC_CAPTURE_TOUCHPOINTS
from crm.customers import (
    duplicate_candidate_rows_with_conn,
    get_customer_record,
    probable_duplicate_snapshot_with_conn,
    upsert_customer_record,
)
from crm.db import db_connection
from crm.intelligence import capture_gap_with_conn
from crm.imports import public_capture_pages
from crm.labels import acquisition_label, source_system_label, touchpoint_label
from crm.segments import campaign_exclusion_clause, segment_definitions
from crm.tasks import list_tasks_with_conn, task_counts_with_conn
from crm.utils import (
    display_name,
    display_timestamp,
    infer_preferred_channel,
    merge_list_text,
    merge_notes,
    parse_datetime,
    parsed_timestamp,
    utc_now,
    validate_email,
    yes_no,
)


def create_touchpoint_capture(fields: dict, *, public_signup: bool = False) -> dict:
    email = fields.get("email", "").strip()
    phone = fields.get("phone", "").strip()
    first_name = fields.get("first_name", "").strip()
    last_name = fields.get("last_name", "").strip()
    if public_signup and not email and not phone:
        return {"error": "Capture at least an email or phone number so Seaview can follow up."}
    if not public_signup and not any([email, phone, first_name, last_name]):
        return {"error": "Add at least a name, email, or phone number before saving the capture."}
    if email and not validate_email(email):
        return {"error": "Enter a valid email address before saving the capture."}

    touchpoint_type = fields.get("touchpoint_type", "").strip() or (
        "website_homepage" if public_signup else "counter_conversation"
    )
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
        now = utc_now()
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
                now,
            ),
        )
        conn.execute(
            """
            UPDATE customers
            SET last_contacted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, customer_id),
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
    conn: sqlite3.Connection,
    segment_key: str,
    touched_at: str,
    segments: dict | None = None,
) -> int:
    active_segments = segments or segment_definitions()
    segment = active_segments.get(segment_key)
    if not segment:
        return 0
    result = conn.execute(
        f"""
        UPDATE customers
        SET last_contacted_at = ?, updated_at = ?
        WHERE ({segment['where']}) AND NOT {campaign_exclusion_clause()}
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
    conn: sqlite3.Connection,
    segment_key: str,
    segments: dict | None = None,
) -> int:
    active_segments = segments or segment_definitions()
    if segment_key not in active_segments:
        return 0
    segment = active_segments[segment_key]
    return conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE ({segment['where']}) AND NOT {campaign_exclusion_clause()}",
        segment["params"],
    ).fetchone()["count"]


def segment_counts_with_conn(conn: sqlite3.Connection, segments: dict | None = None) -> dict[str, int]:
    active_segments = segments or segment_definitions()
    return {
        key: count_segment_rows_with_conn(conn, key, active_segments)
        for key in active_segments
    }


def data_quality_snapshot_with_conn(conn: sqlite3.Connection) -> dict:
    contact_clause = "(COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10)"
    named_clause = "(COALESCE(first_name, '') <> '' OR COALESCE(last_name, '') <> '')"
    total = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
    reachable = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {contact_clause}"
    ).fetchone()["count"]
    marketing_allowed = conn.execute(
        "SELECT COUNT(*) AS count FROM customers WHERE marketing_consent = 1"
    ).fetchone()["count"]
    email_count = conn.execute(
        "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> ''"
    ).fetchone()["count"]
    phone_count = conn.execute(
        "SELECT COUNT(*) AS count FROM customers WHERE length(COALESCE(phone, '')) = 10"
    ).fetchone()["count"]
    named_count = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {named_clause}"
    ).fetchone()["count"]
    campaign_ready = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE marketing_consent = 1 AND {contact_clause}"
    ).fetchone()["count"]
    named_unreachable = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {named_clause} AND NOT {contact_clause}"
    ).fetchone()["count"]
    anonymous_rows = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE NOT {named_clause}"
    ).fetchone()["count"]
    reachable_needs_consent = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {contact_clause} AND marketing_consent = 0"
    ).fetchone()["count"]
    allowed_missing_channel = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM customers
        WHERE marketing_consent = 1
          AND COALESCE(preferred_channel, '') = ''
        """
    ).fetchone()["count"]
    cleanup_records = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE COALESCE(email, '') = ''
           OR length(COALESCE(phone, '')) <> 10
           OR NOT {named_clause}
           OR ({contact_clause} AND marketing_consent = 0)
        """
    ).fetchone()["count"]
    imported_customer_export = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM customers
        WHERE source_system IN ('seaview_customer_export', 'freshline_customer_export', 'clover', 'legacy_csv')
        """
    ).fetchone()["count"]
    duplicate_email_values = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM (
            SELECT lower(email)
            FROM customers
            WHERE COALESCE(email, '') <> ''
            GROUP BY lower(email)
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()["count"]
    duplicate_phone_values = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM (
            SELECT phone
            FROM customers
            WHERE COALESCE(phone, '') <> ''
            GROUP BY phone
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()["count"]

    def rate(value: int) -> float:
        return round((value / total) * 100, 1) if total else 0.0

    missing_email = max(total - email_count, 0)
    missing_phone = max(total - phone_count, 0)
    missing_name = max(total - named_count, 0)
    completeness_score = round(((named_count + reachable + campaign_ready) / (total * 3)) * 100, 1) if total else 0.0

    return {
        "total": total,
        "reachable": reachable,
        "unreachable": max(total - reachable, 0),
        "marketing_allowed": marketing_allowed,
        "email_count": email_count,
        "phone_count": phone_count,
        "missing_email": missing_email,
        "missing_phone": missing_phone,
        "missing_name": missing_name,
        "campaign_ready": campaign_ready,
        "named_unreachable": named_unreachable,
        "anonymous_rows": anonymous_rows,
        "cleanup_records": cleanup_records,
        "reachable_needs_consent": reachable_needs_consent,
        "allowed_missing_channel": allowed_missing_channel,
        "imported_customer_export": imported_customer_export,
        "duplicate_email_values": duplicate_email_values,
        "duplicate_phone_values": duplicate_phone_values,
        "duplicate_contact_values": duplicate_email_values + duplicate_phone_values,
        "reachable_rate": rate(reachable),
        "consent_rate": rate(marketing_allowed),
        "campaign_ready_rate": rate(campaign_ready),
        "data_completeness_score": completeness_score,
    }


def fetch_segment_rows_with_conn(
    conn: sqlite3.Connection,
    segment_key: str,
    segments: dict | None = None,
) -> list[sqlite3.Row]:
    active_segments = segments or segment_definitions()
    if segment_key not in active_segments:
        return []
    segment = active_segments[segment_key]
    return conn.execute(
        f"""
        SELECT id, first_name, last_name, email, phone, preferred_channel, marketing_consent,
               acquisition_source, tags, total_spent, last_purchase_at, customer_since, created_at
        FROM customers
        WHERE ({segment['where']}) AND NOT {campaign_exclusion_clause()}
        ORDER BY total_spent DESC, updated_at DESC
        """,
        segment["params"],
    ).fetchall()


def fetch_segment_rows(segment_key: str) -> list[sqlite3.Row]:
    conn = db_connection()
    try:
        return fetch_segment_rows_with_conn(conn, segment_key)
    finally:
        conn.close()


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
        audience_count = (
            campaign["audience_count"]
            if campaign
            else count_segment_rows_with_conn(conn, segment_key, segments)
        )
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
            conn,
            campaign["target_segment"],
            sent_at,
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
    if duplicate_candidate_rows_with_conn(conn, limit=1):
        return "Review duplicate candidates before exporting an audience list."
    return None


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
        """,
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
          AND (COALESCE(c.email, '') <> '' OR length(COALESCE(c.phone, '')) = 10)
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
          AND (COALESCE(c.email, '') <> '' OR length(COALESCE(c.phone, '')) = 10)
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
    qr_location_keys = ("counter", "receipt", "table", "event")
    qr_by_location: dict[str, int] = {}
    for loc_key in qr_location_keys:
        try:
            qr_by_location[loc_key] = conn.execute(
                "SELECT COUNT(*) AS count FROM touchpoints WHERE scan_location = ?",
                (loc_key,),
            ).fetchone()["count"]
        except Exception:
            qr_by_location[loc_key] = 0
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
        "top_source": sources[0] if sources else None,
        "sources": sources,
        "source_map": source_map,
        "qr_by_location": qr_by_location,
    }


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
          AND (COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10)
        ORDER BY total_spent DESC, last_purchase_at ASC
        LIMIT 2
        """,
        ((datetime.now(UTC) - timedelta(days=45)).replace(microsecond=0).isoformat(),),
    ).fetchall()
    for row in lapsed_rows:
        actions.append({
            "title": f"Win back {display_name(row)}",
            "body": f"No purchase since {display_timestamp(row['last_purchase_at'], include_time=False)}. Prior spend is ${(row['total_spent'] or 0):.2f}.",
            "href": f"/customers/{row['id']}",
            "label": "Open profile",
            "tone": "retention",
        })

    missing_contact_rows = conn.execute(
        """
        SELECT id, first_name, last_name, tags, acquisition_source
        FROM customers
        WHERE COALESCE(email, '') = '' AND length(COALESCE(phone, '')) <> 10
        ORDER BY updated_at DESC
        LIMIT 2
        """
    ).fetchall()
    for row in missing_contact_rows:
        actions.append({
            "title": f"Capture contact for {display_name(row)}",
            "body": f"Currently unreachable. Source: {acquisition_label(row['acquisition_source'])}.",
            "href": f"/customers/{row['id']}",
            "label": "Add contact",
            "tone": "capture",
        })

    if duplicate_snapshot["candidate_groups"]:
        actions.append({
            "title": "Review duplicate customer candidates",
            "body": f"{duplicate_snapshot['candidate_groups']} likely duplicate groups need cleanup before the next campaign export.",
            "href": "/duplicates",
            "label": "Open duplicate review",
            "tone": "data",
        })

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
        actions.append({
            "title": "Resolve the latest import issue",
            "body": f"{source_system_label(recent_failed_import['source_system'])} import on {display_timestamp(recent_failed_import['created_at'])} needs review.",
            "href": "/imports",
            "label": "Open imports",
            "tone": "data",
        })

    if segment_counts["recent_buyers"]:
        actions.append({
            "title": "Send a recent-buyer follow-up",
            "body": f"{segment_counts['recent_buyers']} customers are still warm from the last 30 days.",
            "href": "/marketing",
            "label": "Build campaign",
            "tone": "retention",
        })

    if metrics["open_tasks"]:
        actions.append({
            "title": "Clear the open follow-up queue",
            "body": f"{metrics['open_tasks']} tasks are still open. Finish the nearest due follow-ups first.",
            "href": "/tasks",
            "label": "Open tasks",
            "tone": "ops",
        })

    if not actions:
        actions.append({
            "title": "CRM is up to date",
            "body": "No urgent issues were detected. Use this week to capture more leads and plan the next offer.",
            "href": "/capture",
            "label": "Capture lead",
            "tone": "ops",
        })

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
    data_quality = data_quality_snapshot_with_conn(conn)
    metrics = {
        "customers": conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"],
        "imports": conn.execute("SELECT COUNT(*) AS count FROM import_runs").fetchone()["count"],
        "revenue": conn.execute("SELECT COALESCE(SUM(total_spent), 0) AS total FROM customers").fetchone()["total"],
        "contactable": conn.execute(
            "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10"
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
        "data_quality": data_quality,
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


def marketing_focus(snapshot: dict) -> list[dict]:
    segment_counts = snapshot["segment_counts"]
    return [
        {"title": "Recent buyers", "body": f"{segment_counts['recent_buyers']} customers are still warm from the last 30 days."},
        {"title": "Lapsed buyers", "body": f"{segment_counts['lapsed_buyers']} customers need a win-back message."},
        {"title": "New signups", "body": f"{segment_counts['new_signups']} new signups are ready for a welcome offer."},
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
    segment_rows = []
    for key, segment in active_segments.items():
        count = active_segment_counts[key]
        segment_rows.append({
            "key": key,
            "label": segment["label"],
            "description": segment["description"],
            "recommended_channel": segment["recommended_channel"],
            "count": count,
        })

    week_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    return {
        "segments": segment_rows,
        "segment_counts": active_segment_counts,
        "playbook": weekly_playbook(active_segment_counts),
        "contactable": conn.execute(
            "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10"
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


def marketing_snapshot() -> dict:
    conn = db_connection()
    try:
        return marketing_snapshot_with_conn(conn)
    finally:
        conn.close()


ROI_DEFAULTS = {
    "avg_order_value": 35.00,
    "avg_visits_per_year": 4.0,
    "avg_customer_lifespan_years": 3.0,
    "slow_season_months": "1,2,11,12",
    "peak_season_months": "5,6,7,8,9",
}


def _current_week_start(today: date | None = None) -> date:
    current_day = today or datetime.now(UTC).date()
    return current_day - timedelta(days=current_day.weekday())


def _week_start_sql(column: str = "created_at") -> str:
    # Keep Mondays in their own week. SQLite's weekday modifiers push Monday
    # rows backward, which makes the Results chart and weekly delta misleading.
    return (
        f"date({column}, '-' || "
        f"((CAST(strftime('%w', {column}) AS integer) + 6) % 7) || ' days')"
    )


def _build_weekly_series(
    counts_by_week: dict[str, int], *, weeks: int, end_week: date | None = None
) -> list[dict]:
    final_week = end_week or _current_week_start()
    start_week = final_week - timedelta(weeks=weeks - 1)
    series: list[dict] = []
    for offset in range(weeks):
        current_week = start_week + timedelta(weeks=offset)
        week_key = current_week.isoformat()
        series.append(
            {
                "week_start": week_key,
                "new_customers": int(counts_by_week.get(week_key, 0) or 0),
            }
        )
    return series


def record_weekly_snapshot(conn: sqlite3.Connection) -> None:
    week_start = _current_week_start().isoformat()
    total_customers = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
    reachable_customers = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM customers
        WHERE COALESCE(email, '') <> '' OR COALESCE(phone, '') <> ''
        """
    ).fetchone()["count"]
    campaign_ready = count_segment_rows_with_conn(conn, "clean_campaign_ready")
    qr_captures = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM touchpoints
        WHERE scan_location IS NOT NULL
          AND date(created_at) >= ?
        """,
        (week_start,),
    ).fetchone()["count"]
    new_this_week = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM customers
        WHERE date(created_at) >= ?
        """,
        (week_start,),
    ).fetchone()["count"]
    conn.execute(
        """
        INSERT OR REPLACE INTO file_growth_snapshots (
            week_start, total_customers, reachable_customers, campaign_ready,
            qr_captures, new_this_week, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            week_start,
            total_customers,
            reachable_customers,
            campaign_ready,
            qr_captures,
            new_this_week,
            utc_now(),
        ),
    )


def backfill_file_growth(conn: sqlite3.Connection) -> None:
    current_week = _current_week_start()
    window_start = (current_week - timedelta(weeks=23)).isoformat()
    week_sql = _week_start_sql()
    signup_rows = conn.execute(
        f"""
        SELECT
            {week_sql} AS week_start,
            COUNT(*) AS new_customers
        FROM customers
        WHERE date(created_at) >= ?
        GROUP BY week_start
        ORDER BY week_start
        """,
        (window_start,),
    ).fetchall()

    current_total = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
    current_reachable = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM customers
        WHERE COALESCE(email, '') <> '' OR COALESCE(phone, '') <> ''
        """
    ).fetchone()["count"]
    current_campaign_ready = count_segment_rows_with_conn(conn, "clean_campaign_ready")
    reachable_ratio = (current_reachable / current_total) if current_total else 0.987
    campaign_ratio = (current_campaign_ready / current_total) if current_total else 0.987

    earliest_week = window_start
    baseline_total = conn.execute(
        "SELECT COUNT(*) AS count FROM customers WHERE date(created_at) < ?",
        (earliest_week,),
    ).fetchone()["count"]
    running_total = baseline_total
    created_at = utc_now()
    counts_by_week = {str(row["week_start"]): int(row["new_customers"] or 0) for row in signup_rows}
    weekly_series = _build_weekly_series(counts_by_week, weeks=24, end_week=current_week)

    for row in weekly_series:
        weekly_new = int(row["new_customers"] or 0)
        running_total += weekly_new
        reachable_estimate = int(round(running_total * reachable_ratio))
        campaign_ready_estimate = int(round(running_total * campaign_ratio))
        conn.execute(
            """
            INSERT OR REPLACE INTO file_growth_snapshots (
                week_start, total_customers, reachable_customers, campaign_ready,
                qr_captures, new_this_week, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["week_start"],
                running_total,
                reachable_estimate,
                campaign_ready_estimate,
                0,
                weekly_new,
                created_at,
            ),
        )


def run_campaign_attribution(conn: sqlite3.Connection) -> None:
    segments = segment_definitions()
    sent_campaigns = conn.execute(
        """
        SELECT *
        FROM campaigns
        WHERE status = 'sent'
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    for campaign in sent_campaigns:
        segment = segments.get(campaign["target_segment"])
        if not segment:
            continue
        sent_event = conn.execute(
            """
            SELECT created_at
            FROM outreach_history
            WHERE campaign_id = ?
              AND event_type = 'sent'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (campaign["id"],),
        ).fetchone()
        sent_at = (
            sent_event["created_at"]
            if sent_event
            else (campaign["scheduled_for"] or campaign["created_at"])
        )
        attributed_rows = conn.execute(
            f"""
            SELECT id
            FROM customers
            WHERE ({segment['where']})
              AND NOT {campaign_exclusion_clause()}
              AND COALESCE(updated_at, created_at) > ?
            """,
            (*segment["params"], sent_at),
        ).fetchall()
        for row in attributed_rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO campaign_attribution (
                    campaign_id, customer_id, attributed_at, attribution_type
                ) VALUES (?, ?, ?, 'import_match')
                """,
                (campaign["id"], row["id"], utc_now()),
            )


def results_snapshot_with_conn(conn: sqlite3.Connection) -> dict:
    roi = conn.execute("SELECT * FROM roi_settings WHERE id = 1").fetchone()
    if not roi:
        conn.execute(
            """
            INSERT OR IGNORE INTO roi_settings (
                id, avg_order_value, avg_visits_per_year, avg_customer_lifespan_years,
                slow_season_months, peak_season_months, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                ROI_DEFAULTS["avg_order_value"],
                ROI_DEFAULTS["avg_visits_per_year"],
                ROI_DEFAULTS["avg_customer_lifespan_years"],
                ROI_DEFAULTS["slow_season_months"],
                ROI_DEFAULTS["peak_season_months"],
                utc_now(),
            ),
        )
        conn.commit()
        roi = conn.execute("SELECT * FROM roi_settings WHERE id = 1").fetchone()

    segment_counts = segment_counts_with_conn(conn)
    capture_gap = capture_gap_with_conn(conn)
    campaign_ready = segment_counts["clean_campaign_ready"]
    clv = (
        float(roi["avg_order_value"])
        * float(roi["avg_visits_per_year"])
        * float(roi["avg_customer_lifespan_years"])
    )
    dark_clover = capture_gap["dark_customers"]
    reachable_value = clv * campaign_ready
    capture_gap_value = clv * (dark_clover * 0.05)
    avg_order_value_actual = conn.execute(
        """
        SELECT AVG(total_spent) AS avg_value
        FROM customers
        WHERE total_spent > 0
        """
    ).fetchone()["avg_value"] or 0.0

    current_week = _current_week_start()
    week_sql = _week_start_sql()
    weekly_signups_rows = conn.execute(
        f"""
        SELECT
            {week_sql} AS week_start,
            COUNT(*) AS new_customers
        FROM customers
        WHERE date(created_at) >= ?
        GROUP BY week_start
        ORDER BY week_start
        """,
        ((current_week - timedelta(weeks=23)).isoformat(),),
    ).fetchall()
    weekly_signups = _build_weekly_series(
        {str(row["week_start"]): int(row["new_customers"] or 0) for row in weekly_signups_rows},
        weeks=24,
        end_week=current_week,
    )

    snapshot_rows = conn.execute(
        """
        SELECT *
        FROM file_growth_snapshots
        WHERE week_start >= ?
        ORDER BY week_start
        """,
        ((current_week - timedelta(weeks=11)).isoformat(),),
    ).fetchall()
    snapshot_map = {
        str(row["week_start"]): {
            "week_start": row["week_start"],
            "new_customers": int(row["new_this_week"] or 0),
            "total_customers": int(row["total_customers"] or 0),
            "reachable_customers": int(row["reachable_customers"] or 0),
            "campaign_ready": int(row["campaign_ready"] or 0),
        }
        for row in snapshot_rows
    }
    weekly_growth: list[dict] = []
    for row in weekly_signups[-12:]:
        weekly_growth.append(snapshot_map.get(row["week_start"], dict(row)))

    qr_leaderboard = [
        {
            "location": row["location"],
            "total_captures": int(row["total_captures"] or 0),
            "this_week": int(row["this_week"] or 0),
            "this_month": int(row["this_month"] or 0),
        }
        for row in conn.execute(
            """
            SELECT
                COALESCE(scan_location, touchpoint_type) AS location,
                COUNT(*) AS total_captures,
                COUNT(CASE WHEN created_at >= date('now', '-7 days')
                      THEN 1 END) AS this_week,
                COUNT(CASE WHEN created_at >= date('now', '-30 days')
                      THEN 1 END) AS this_month
            FROM touchpoints
            WHERE touchpoint_type IN (
                'in_store_qr', 'receipt_qr', 'event_booth',
                'website_homepage', 'wholesale_inquiry'
            )
            GROUP BY location
            ORDER BY total_captures DESC
            """
        ).fetchall()
    ]
    source_breakdown = [
        {
            "acquisition_source": row["acquisition_source"],
            "count": int(row["count"] or 0),
            "pct": float(row["pct"] or 0.0),
        }
        for row in conn.execute(
            """
            SELECT
                COALESCE(acquisition_source, 'unknown') AS acquisition_source,
                COUNT(*) AS count,
                ROUND(COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*) FROM customers), 0), 1) AS pct
            FROM customers
            GROUP BY acquisition_source
            ORDER BY count DESC
            LIMIT 8
            """
        ).fetchall()
    ]
    attribution_results = [
        {
            "id": row["id"],
            "title": row["title"],
            "channel": row["channel"],
            "audience_count": int(row["audience_count"] or 0),
            "target_segment": row["target_segment"],
            "scheduled_for": row["scheduled_for"],
            "attributed_returns": int(row["attributed_returns"] or 0),
            "return_rate_pct": float(row["return_rate_pct"] or 0.0),
        }
        for row in conn.execute(
            """
            SELECT
                c.id,
                c.title,
                c.channel,
                c.audience_count,
                c.target_segment,
                c.scheduled_for,
                COUNT(ca.customer_id) AS attributed_returns,
                ROUND(
                    COUNT(ca.customer_id) * 100.0 / NULLIF(c.audience_count, 0),
                    1
                ) AS return_rate_pct
            FROM campaigns c
            LEFT JOIN campaign_attribution ca ON ca.campaign_id = c.id
            WHERE c.status = 'sent'
            GROUP BY c.id
            ORDER BY
                CASE WHEN COALESCE(c.scheduled_for, '') = '' THEN 1 ELSE 0 END,
                c.scheduled_for DESC,
                c.created_at DESC
            LIMIT 10
            """
        ).fetchall()
    ]

    new_this_week = int(weekly_growth[-1]["new_customers"] if weekly_growth else 0)
    new_last_week = int(weekly_growth[-2]["new_customers"] if len(weekly_growth) > 1 else 0)
    wow_change = new_this_week - new_last_week
    wow_pct = round((wow_change / max(new_last_week, 1)) * 100, 1)

    return {
        "roi": dict(roi),
        "clv": clv,
        "campaign_ready": campaign_ready,
        "reachable_value": reachable_value,
        "capture_gap_value": capture_gap_value,
        "avg_order_value_actual": avg_order_value_actual,
        "dark_clover": dark_clover,
        "capture_gap": capture_gap,
        "weekly_growth": weekly_growth,
        "weekly_signups": weekly_signups,
        "qr_leaderboard": qr_leaderboard,
        "source_breakdown": source_breakdown,
        "attribution_results": attribution_results,
        "new_this_week": new_this_week,
        "new_last_week": new_last_week,
        "wow_change": wow_change,
        "wow_pct": wow_pct,
        "top_capture_source": qr_leaderboard[0] if qr_leaderboard else None,
    }


def export_segment_csv(segment_key: str) -> bytes:
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


def reporting_snapshot() -> dict:
    conn = db_connection()
    try:
        segment_counts = segment_counts_with_conn(conn)
        lead_capture = lead_capture_snapshot_with_conn(conn)
        data_quality = data_quality_snapshot_with_conn(conn)
        total_customers = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
        reachable_customers = conn.execute(
            "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10"
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

    def bucket_week(rows: list, value_key: str) -> list[dict]:
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
        "data_quality": data_quality,
        "segment_counts": segment_counts,
        "campaign_counts": campaign_counts,
        "lead_sources": lead_sources,
        "import_rows": import_rows,
        "lead_trend": bucket_week(
            [{"created_at": row["created_at"], "count": 1} for row in lead_rows],
            "count",
        ),
        "import_trend": bucket_week(import_rows, "rows_received"),
        "outreach_trend": bucket_week(outreach_rows, "audience_count"),
    }
