import json
import sqlite3
from datetime import UTC, datetime

from crm.segments import campaign_exclusion_clause, segment_definitions
from crm.utils import display_name, display_timestamp, parse_datetime


BUSINESS_SEGMENT_KEYS = ("email_ready", "sms_ready", "clean_campaign_ready", "needs_attention")
CLOVER_SOURCE_SYSTEMS = ("clover",)
FRESHLINE_SOURCE_SYSTEM = "freshline_customer_export"


def _contact_clause() -> str:
    return "(COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10)"


def _captured_contact_clause() -> str:
    return "(COALESCE(email, '') <> '' OR COALESCE(phone, '') <> '')"


def _named_clause() -> str:
    return "(COALESCE(first_name, '') <> '' OR COALESCE(last_name, '') <> '')"


def _customer_state(row: sqlite3.Row | dict | None) -> dict:
    if not row:
        return {
            "has_email": False,
            "has_phone": False,
            "has_name": False,
            "reachable": False,
            "campaign_ready": False,
            "marketing_consent": False,
        }

    def get(key: str):
        return row[key] if isinstance(row, sqlite3.Row) else row.get(key)

    has_email = bool((get("email") or "").strip())
    has_phone = len((get("phone") or "").strip()) == 10
    has_name = bool((get("first_name") or "").strip() or (get("last_name") or "").strip())
    reachable = has_email or has_phone
    marketing_consent = bool(get("marketing_consent"))
    return {
        "has_email": has_email,
        "has_phone": has_phone,
        "has_name": has_name,
        "reachable": reachable,
        "campaign_ready": reachable and marketing_consent,
        "marketing_consent": marketing_consent,
    }


def customer_contact_state(row: sqlite3.Row | dict | None) -> dict:
    return _customer_state(row)


def customer_file_snapshot_with_conn(conn: sqlite3.Connection) -> dict:
    contact_clause = _contact_clause()
    named_clause = _named_clause()
    total = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
    reachable = conn.execute(f"SELECT COUNT(*) AS count FROM customers WHERE {contact_clause}").fetchone()["count"]
    campaign_ready = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE marketing_consent = 1 AND {contact_clause}"
    ).fetchone()["count"]
    email_count = conn.execute(
        "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> ''"
    ).fetchone()["count"]
    phone_count = conn.execute(
        "SELECT COUNT(*) AS count FROM customers WHERE length(COALESCE(phone, '')) = 10"
    ).fetchone()["count"]
    missing_both = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE NOT {contact_clause}"
    ).fetchone()["count"]
    reachable_needs_consent = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {contact_clause} AND marketing_consent = 0"
    ).fetchone()["count"]
    missing_name = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE NOT {named_clause}"
    ).fetchone()["count"]
    return {
        "total": total,
        "reachable": reachable,
        "campaign_ready": campaign_ready,
        "email_count": email_count,
        "phone_count": phone_count,
        "missing_both": missing_both,
        "reachable_needs_consent": reachable_needs_consent,
        "missing_name": missing_name,
    }


def build_import_intelligence_summary(
    *,
    source_system: str,
    filename: str,
    import_result: dict,
    before_snapshot: dict,
    after_snapshot: dict,
    touched_before: dict[int, dict],
    touched_after: dict[int, sqlite3.Row],
) -> dict:
    email_added = phone_added = name_added = consent_added = 0
    reachable_unlocked = campaign_ready_unlocked = 0

    for customer_id, after_row in touched_after.items():
        before_state = touched_before.get(customer_id, _customer_state(None))
        after_state = _customer_state(after_row)
        if after_state["has_email"] and not before_state["has_email"]:
            email_added += 1
        if after_state["has_phone"] and not before_state["has_phone"]:
            phone_added += 1
        if after_state["has_name"] and not before_state["has_name"]:
            name_added += 1
        if after_state["marketing_consent"] and not before_state["marketing_consent"]:
            consent_added += 1
        if after_state["reachable"] and not before_state["reachable"]:
            reachable_unlocked += 1
        if after_state["campaign_ready"] and not before_state["campaign_ready"]:
            campaign_ready_unlocked += 1

    total_delta = after_snapshot["total"] - before_snapshot["total"]
    reachable_delta = after_snapshot["reachable"] - before_snapshot["reachable"]
    campaign_ready_delta = after_snapshot["campaign_ready"] - before_snapshot["campaign_ready"]
    total_touched = import_result["customers_created"] + import_result["customers_updated"]

    if campaign_ready_unlocked:
        headline = f"You gained {campaign_ready_unlocked} campaign-ready customer{'s' if campaign_ready_unlocked != 1 else ''} this upload."
    elif reachable_unlocked:
        headline = f"You made {reachable_unlocked} customer{'s' if reachable_unlocked != 1 else ''} reachable this upload."
    elif total_delta:
        headline = f"You added {total_delta} customer record{'s' if total_delta != 1 else ''}, but the campaign audience did not grow yet."
    else:
        headline = "This upload refreshed the file, but did not unlock a new campaign audience yet."

    improvements = []
    if email_added:
        improvements.append(f"{email_added} record{'s' if email_added != 1 else ''} now have email that did not before.")
    if phone_added:
        improvements.append(f"{phone_added} record{'s' if phone_added != 1 else ''} now have a usable phone number.")
    if consent_added:
        improvements.append(f"{consent_added} reachable record{'s' if consent_added != 1 else ''} now have campaign permission.")
    if name_added:
        improvements.append(f"{name_added} record{'s' if name_added != 1 else ''} are easier for staff to recognize by name.")
    if not improvements:
        improvements.append("No new contact fields were unlocked; treat this file as a history refresh.")

    broken = []
    if after_snapshot["missing_both"]:
        broken.append(f"{after_snapshot['missing_both']} record{'s' if after_snapshot['missing_both'] != 1 else ''} still missing both email and phone.")
    if after_snapshot["reachable_needs_consent"]:
        broken.append(f"{after_snapshot['reachable_needs_consent']} reachable customer{'s' if after_snapshot['reachable_needs_consent'] != 1 else ''} still need marketing permission.")
    if after_snapshot["missing_name"]:
        broken.append(f"{after_snapshot['missing_name']} record{'s' if after_snapshot['missing_name'] != 1 else ''} still need a name for staff cleanup.")
    if not broken:
        broken.append("No major contact blockers remain in the current customer file.")

    return {
        "source_system": source_system,
        "filename": filename,
        "headline": headline,
        "records_imported": import_result["rows_received"],
        "records_previously_on_file": before_snapshot["total"],
        "records_after_import": after_snapshot["total"],
        "customers_touched": total_touched,
        "net_changes": {
            "total_contacts": total_delta,
            "reachable_customers": reachable_delta,
            "campaign_ready_customers": campaign_ready_delta,
        },
        "improvements": improvements,
        "newly_usable": {
            "reachable": reachable_unlocked,
            "campaign_ready": campaign_ready_unlocked,
        },
        "still_broken": broken,
        "remaining_unusable": {
            "missing_contact": after_snapshot["missing_both"],
            "needs_consent": after_snapshot["reachable_needs_consent"],
            "missing_name": after_snapshot["missing_name"],
        },
    }


def import_summary_from_row(row: sqlite3.Row | None) -> dict | None:
    if not row or "intelligence_summary" not in row.keys() or not row["intelligence_summary"]:
        return None
    try:
        return json.loads(row["intelligence_summary"])
    except json.JSONDecodeError:
        return None


def record_import_summary(summary: dict) -> str:
    return json.dumps(summary, separators=(",", ":"), sort_keys=True)


def _freshline_clause(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}source_system = '{FRESHLINE_SOURCE_SYSTEM}' AND NOT {campaign_exclusion_clause(alias)}"


def _clover_clause(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    quoted_sources = ", ".join(f"'{source}'" for source in CLOVER_SOURCE_SYSTEMS)
    return f"{prefix}source_system IN ({quoted_sources}) AND COALESCE({prefix}external_id, '') NOT LIKE 'CL-%'"


def _duplicate_name_groups_with_conn(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) AS match_name,
               COUNT(*) AS count,
               COUNT(DISTINCT lower(email)) AS email_count
        FROM customers
        WHERE source_system = ?
          AND COALESCE(first_name, '') <> ''
          AND COALESCE(last_name, '') <> ''
          AND trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) <> ''
          AND COALESCE(email, '') <> ''
        GROUP BY lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')))
        HAVING COUNT(DISTINCT lower(email)) > 1
        ORDER BY
          CASE WHEN match_name IN ('ross wade', 'nathan king') THEN 0 ELSE 1 END,
          COUNT(*) DESC,
          match_name
        LIMIT ?
        """,
        (FRESHLINE_SOURCE_SYSTEM, limit,),
    ).fetchall()
    groups = []
    for row in rows:
        customers = conn.execute(
            f"""
            SELECT id, first_name, last_name, email, phone, updated_at
            FROM customers
            WHERE source_system = ?
              AND COALESCE(first_name, '') <> ''
              AND COALESCE(last_name, '') <> ''
              AND lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (FRESHLINE_SOURCE_SYSTEM, row["match_name"],),
        ).fetchall()
        groups.append({
            "name": row["match_name"].title(),
            "count": row["count"],
            "customers": [
                {
                    "id": customer["id"],
                    "name": display_name(customer),
                    "email": customer["email"] or "",
                    "phone": customer["phone"] or "",
                    "href": f"/customers/{customer['id']}",
                }
                for customer in customers
            ],
        })
    return groups


def freshline_cleanup_with_conn(conn: sqlite3.Connection, limit: int = 25) -> dict:
    internal_rows = conn.execute(
        f"""
        SELECT id, first_name, last_name, email
        FROM customers
        WHERE source_system = ?
          AND {campaign_exclusion_clause()}
        ORDER BY email
        LIMIT ?
        """,
        (FRESHLINE_SOURCE_SYSTEM, limit),
    ).fetchall()
    internal_total = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE source_system = ?
          AND {campaign_exclusion_clause()}
        """,
        (FRESHLINE_SOURCE_SYSTEM,),
    ).fetchone()["count"]

    invalid_phone_rows = conn.execute(
        f"""
        SELECT id, first_name, last_name, email, phone
        FROM customers
        WHERE source_system = ?
          AND COALESCE(phone, '') <> ''
          AND length(COALESCE(phone, '')) <> 10
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (FRESHLINE_SOURCE_SYSTEM, limit),
    ).fetchall()
    invalid_phone_total = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE source_system = ?
          AND COALESCE(phone, '') <> ''
          AND length(COALESCE(phone, '')) <> 10
        """,
        (FRESHLINE_SOURCE_SYSTEM,),
    ).fetchone()["count"]
    invalid_campaign_phone_total = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE {_freshline_clause()}
          AND COALESCE(phone, '') <> ''
          AND length(COALESCE(phone, '')) <> 10
        """
    ).fetchone()["count"]

    first_name_rows = conn.execute(
        f"""
        SELECT id, first_name, last_name, email, phone
        FROM customers
        WHERE {_freshline_clause()}
          AND COALESCE(first_name, '') <> ''
          AND COALESCE(last_name, '') = ''
        ORDER BY first_name COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    first_name_total = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE {_freshline_clause()}
          AND COALESCE(first_name, '') <> ''
          AND COALESCE(last_name, '') = ''
        """
    ).fetchone()["count"]

    duplicate_groups = _duplicate_name_groups_with_conn(conn, limit=min(limit, 23))

    def phone_issue(phone: str) -> str:
        if not phone:
            return "Non-numeric"
        if len(phone) < 10:
            return "Missing digits"
        if len(phone) > 10:
            return "Too many digits"
        return "Phone unverified"

    return {
        "internal_total": internal_total,
        "internal_records": [
            {
                "id": row["id"],
                "name": display_name(row),
                "email": row["email"] or "",
                "href": f"/customers/{row['id']}",
            }
            for row in internal_rows
        ],
        "invalid_phone_total": invalid_phone_total,
        "invalid_campaign_phone_total": invalid_campaign_phone_total,
        "invalid_phone_records": [
            {
                "id": row["id"],
                "name": display_name(row),
                "email": row["email"] or "",
                "phone": row["phone"] or "",
                "issue": phone_issue(row["phone"] or ""),
                "is_internal": bool(conn.execute(
                    f"SELECT 1 FROM customers WHERE id = ? AND {campaign_exclusion_clause()}",
                    (row["id"],),
                ).fetchone()),
                "href": f"/customers/{row['id']}",
            }
            for row in invalid_phone_rows
        ],
        "duplicate_total": len(duplicate_groups),
        "duplicate_groups": duplicate_groups,
        "first_name_only_total": first_name_total,
        "first_name_only_records": [
            {
                "id": row["id"],
                "name": display_name(row),
                "email": row["email"] or "",
                "phone": row["phone"] or "",
                "href": f"/customers/{row['id']}",
            }
            for row in first_name_rows
        ],
    }


def capture_gap_with_conn(conn: sqlite3.Connection) -> dict:
    clover_clause = _clover_clause()
    captured_contact_clause = _captured_contact_clause()
    segments = segment_definitions()
    clean_segment = segments["clean_campaign_ready"]
    clover_total = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {clover_clause}"
    ).fetchone()["count"]
    reachable_email = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {clover_clause} AND COALESCE(email, '') <> ''"
    ).fetchone()["count"]
    reachable_any = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {clover_clause} AND {captured_contact_clause}"
    ).fetchone()["count"]
    reachable_phone = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {clover_clause} AND COALESCE(phone, '') <> ''"
    ).fetchone()["count"]
    marketing_allowed = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {clover_clause} AND marketing_consent = 1"
    ).fetchone()["count"]
    opted_no_contact = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE {clover_clause}
          AND marketing_consent = 1
          AND NOT {captured_contact_clause}
        """
    ).fetchone()["count"]
    dark_customers = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {clover_clause} AND NOT {captured_contact_clause}"
    ).fetchone()["count"]
    freshline_reachable = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM customers
        WHERE {_freshline_clause()}
          AND COALESCE(email, '') <> ''
        """
    ).fetchone()["count"]
    freshline_total = conn.execute(
        "SELECT COUNT(*) AS count FROM customers WHERE source_system = ?",
        (FRESHLINE_SOURCE_SYSTEM,),
    ).fetchone()["count"]
    freshline_campaign_ready = conn.execute(
        f"SELECT COUNT(*) AS count FROM customers WHERE {clean_segment['where']}",
        clean_segment["params"],
    ).fetchone()["count"]
    capture_rate = round((reachable_email / clover_total) * 100, 1) if clover_total else 0.0
    five_percent_goal = round(clover_total * 0.05)
    ten_percent_goal = round(clover_total * 0.10)
    return {
        "clover_total": clover_total,
        "reachable_email": reachable_email,
        "reachable_any": reachable_any,
        "reachable_phone": reachable_phone,
        "marketing_allowed": marketing_allowed,
        "opted_no_contact": opted_no_contact,
        "dark_customers": dark_customers,
        "freshline_reachable": freshline_reachable,
        "freshline_total": freshline_total,
        "freshline_campaign_ready": freshline_campaign_ready,
        "capture_rate": capture_rate,
        "five_percent_goal": five_percent_goal,
        "ten_percent_goal": ten_percent_goal,
    }


def freshline_audience_snapshot_with_conn(conn: sqlite3.Connection) -> dict:
    segments = segment_definitions()
    counts = {}
    for key in BUSINESS_SEGMENT_KEYS:
        segment = segments[key]
        counts[key] = conn.execute(
            f"SELECT COUNT(*) AS count FROM customers WHERE {segment['where']}",
            segment["params"],
        ).fetchone()["count"]
    return counts


def build_source_aware_import_summary(conn: sqlite3.Connection, source_system: str, summary: dict) -> dict:
    if source_system in CLOVER_SOURCE_SYSTEMS:
        gap = capture_gap_with_conn(conn)
        summary.update({
            "source_type": "clover",
            "headline": f"You have {gap['dark_customers']:,} in-store customers with no way to reach them.",
            "business_lines": [
                f"{gap['marketing_allowed']:,} are marked marketing allowed in Clover; {gap['opted_no_contact']:,} still have no email or phone.",
                f"Your reachable audience is {gap['freshline_reachable']:,} Freshline customers only.",
                f"For every 100 in-store customers, fewer than 1 has an email captured.",
            ],
            "source_metrics": gap,
        })
        return summary

    if source_system == FRESHLINE_SOURCE_SYSTEM:
        cleanup = freshline_cleanup_with_conn(conn, limit=25)
        audience = freshline_audience_snapshot_with_conn(conn)
        summary.update({
            "source_type": "freshline",
            "headline": f"Freshline is the reachable audience: {audience['email_ready']:,} customers can be emailed after exclusions.",
            "business_lines": [
                f"{cleanup['invalid_phone_total']:,} invalid phone number{'s' if cleanup['invalid_phone_total'] != 1 else ''} need manual cleanup.",
                f"{cleanup['duplicate_total']:,} duplicate name group{'s' if cleanup['duplicate_total'] != 1 else ''} need review.",
                f"{cleanup['internal_total']:,} internal or test record{'s' if cleanup['internal_total'] != 1 else ''} are excluded automatically.",
                f"{audience['clean_campaign_ready']:,} customers are clean and campaign-ready for this file.",
            ],
            "source_metrics": {
                "audience": audience,
                "cleanup": cleanup,
            },
        })
        return summary

    summary.setdefault("source_type", "general")
    summary.setdefault("business_lines", summary.get("improvements", []))
    return summary


def cleanup_priorities_with_conn(conn: sqlite3.Connection, limit: int = 5) -> dict:
    contact_clause = _contact_clause()
    named_clause = _named_clause()
    field_priorities = [
        {
            "field": "Email",
            "count": conn.execute("SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') = ''").fetchone()["count"],
            "blocks": conn.execute(
                "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') = '' AND length(COALESCE(phone, '')) <> 10"
            ).fetchone()["count"],
            "action": "Ask for email when there is no usable phone, or when email campaigns are the next channel.",
        },
        {
            "field": "Phone",
            "count": conn.execute(
                "SELECT COUNT(*) AS count FROM customers WHERE length(COALESCE(phone, '')) <> 10"
            ).fetchone()["count"],
            "blocks": conn.execute(
                "SELECT COUNT(*) AS count FROM customers WHERE length(COALESCE(phone, '')) <> 10 AND COALESCE(email, '') = ''"
            ).fetchone()["count"],
            "action": "Ask for phone when the customer has no email or when SMS pickup offers matter.",
        },
        {
            "field": "Name",
            "count": conn.execute(f"SELECT COUNT(*) AS count FROM customers WHERE NOT {named_clause}").fetchone()["count"],
            "blocks": 0,
            "action": "Add a name so staff can recognize, dedupe, and follow up with confidence.",
        },
        {
            "field": "Consent",
            "count": conn.execute(
                f"SELECT COUNT(*) AS count FROM customers WHERE {contact_clause} AND marketing_consent = 0"
            ).fetchone()["count"],
            "blocks": conn.execute(
                f"SELECT COUNT(*) AS count FROM customers WHERE {contact_clause} AND marketing_consent = 0"
            ).fetchone()["count"],
            "action": "Confirm permission so reachable contacts can be used in campaigns.",
        },
    ]

    rows = conn.execute(
        f"""
        SELECT id, first_name, last_name, email, phone, marketing_consent, total_spent,
               tags, source_system, acquisition_source, last_purchase_at, updated_at
        FROM customers
        WHERE COALESCE(email, '') = ''
           OR length(COALESCE(phone, '')) <> 10
           OR NOT {named_clause}
           OR ({contact_clause} AND marketing_consent = 0)
        """
    ).fetchall()

    records = []
    now = datetime.now(UTC)
    for row in rows:
        state = _customer_state(row)
        contact_blocker = not state["reachable"]
        consent_blocker = not state["marketing_consent"]
        blockers_to_campaign = (1 if contact_blocker else 0) + (1 if consent_blocker else 0)
        unlocks_campaign_ready = blockers_to_campaign == 1
        missing = []
        if not state["has_email"]:
            missing.append("email")
        if not state["has_phone"]:
            missing.append("phone")
        if not state["has_name"]:
            missing.append("name")
        if state["reachable"] and not state["marketing_consent"]:
            missing.append("consent")

        high_value_bonus = 0
        tags = (row["tags"] or "").lower()
        if any(token in tags for token in ("vip", "wholesale", "repeat")):
            high_value_bonus += 150
        if row["last_purchase_at"]:
            parsed = parse_datetime(row["last_purchase_at"])
            if parsed and (now - parsed).days <= 45:
                high_value_bonus += 75
        value_score = float(row["total_spent"] or 0) + high_value_bonus
        closeness_score = max(0, 3 - blockers_to_campaign) * 100

        if contact_blocker and not consent_blocker:
            fix = "Add email or phone"
            unlock = "Unlocks a campaign-ready customer"
        elif not contact_blocker and consent_blocker:
            fix = "Confirm campaign permission"
            unlock = "Unlocks a campaign-ready customer"
        elif state["has_phone"] and not state["has_email"]:
            fix = "Add email"
            unlock = "Adds this customer to email campaigns"
        elif state["has_email"] and not state["has_phone"]:
            fix = "Add phone"
            unlock = "Adds this customer to text-ready outreach"
        else:
            fix = "Complete the missing fields"
            unlock = "Improves staff follow-up quality"

        records.append({
            "id": row["id"],
            "name": display_name(row),
            "missing": ", ".join(missing) if missing else "profile detail",
            "fix": fix,
            "unlock": unlock,
            "href": f"/customers/{row['id']}",
            "total_spent": float(row["total_spent"] or 0),
            "value_label": f"${(row['total_spent'] or 0):.2f} on file",
            "source": row["acquisition_source"] or row["source_system"] or "customer file",
            "unlocks_campaign_ready": unlocks_campaign_ready,
            "sort_score": (
                1 if unlocks_campaign_ready else 0,
                closeness_score,
                value_score,
                parse_datetime(row["updated_at"]) or datetime.min.replace(tzinfo=UTC),
            ),
        })

    records = sorted(records, key=lambda item: item["sort_score"], reverse=True)[:limit]
    for record in records:
        record.pop("sort_score", None)

    return {
        "field_priorities": sorted(field_priorities, key=lambda item: (item["blocks"], item["count"]), reverse=True),
        "records": records,
        "campaign_unlock_count": sum(1 for record in records if record["unlocks_campaign_ready"]),
    }


def business_audience_segments_with_conn(conn: sqlite3.Connection) -> list[dict]:
    segments = segment_definitions()
    rows = []
    missing_copy = {
        "email_ready": "Internal and test emails are excluded.",
        "sms_ready": "Phones with fewer or more than 10 digits are excluded.",
        "clean_campaign_ready": "Internal records and invalid phones are excluded.",
        "needs_attention": "Invalid phones and duplicate-name records need manual fixes.",
    }
    action_copy = {
        "email_ready": "Export for Constant Contact",
        "sms_ready": "Export text list",
        "clean_campaign_ready": "Export campaign list",
        "needs_attention": "Export cleanup list",
    }
    for key in BUSINESS_SEGMENT_KEYS:
        segment = segments[key]
        count = conn.execute(
            f"SELECT COUNT(*) AS count FROM customers WHERE {segment['where']}",
            segment["params"],
        ).fetchone()["count"]
        rows.append({
            "key": key,
            "label": segment["label"],
            "count": count,
            "missing": missing_copy[key],
            "action": action_copy[key],
            "export_href": f"/marketing/export?segment={key}",
        })
    return rows


def weekly_action_plan_with_conn(conn: sqlite3.Connection, offer_hook: str = "") -> list[dict]:
    offer = (offer_hook or "this week's special").strip()
    segments = {row["key"]: row for row in business_audience_segments_with_conn(conn)}
    cleanup = freshline_cleanup_with_conn(conn, limit=25)
    gap = capture_gap_with_conn(conn)
    campaign_ready = segments["clean_campaign_ready"]["count"]

    plan = []
    if campaign_ready:
        plan.append({
            "title": f"Email your {campaign_ready:,} campaign-ready Freshline customer{'s' if campaign_ready != 1 else ''} about {offer}.",
            "body": "This is the clean reachable universe after excluding internal records and invalid text numbers.",
            "href": segments["clean_campaign_ready"]["export_href"],
            "label": "Export list",
            "tone": "retention",
        })

    if cleanup["invalid_phone_total"]:
        unlockable = cleanup.get("invalid_campaign_phone_total", cleanup["invalid_phone_total"])
        plan.append({
            "title": f"Fix these {cleanup['invalid_phone_total']:,} phone number{'s' if cleanup['invalid_phone_total'] != 1 else ''} to add them to your text list.",
            "body": f"{unlockable:,} customer record{'s' if unlockable != 1 else ''} can become text-ready after internal exclusions.",
            "href": "#cleanup-priorities",
            "label": "View list",
            "tone": "capture",
        })

    if cleanup["duplicate_total"]:
        plan.append({
            "title": f"Resolve {cleanup['duplicate_total']:,} duplicate account group{'s' if cleanup['duplicate_total'] != 1 else ''}.",
            "body": "Same-name Freshline records with different emails should be reviewed before list cleanup is considered done.",
            "href": "#cleanup-priorities",
            "label": "View duplicates",
            "tone": "data",
        })

    plan.append({
        "title": "At checkout this week: ask every in-store customer for their email.",
        "body": f"Goal: capture 5% of in-store traffic from this file, or about {gap['five_percent_goal']:,} new reachable customers.",
        "href": "#capture-gap",
        "label": "See gap",
        "tone": "ops",
    })

    if not plan:
        plan.append({
            "title": "Upload the next customer file before choosing this week's offer.",
            "body": "The CRM needs fresh customer data before it can hand you a useful task list.",
            "href": "/imports",
            "label": "Import file",
            "tone": "ops",
        })

    return plan[:5]
