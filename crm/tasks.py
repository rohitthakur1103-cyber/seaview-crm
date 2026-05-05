import json
import logging
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from crm.config import TASK_TYPES
from crm.db import db_connection
from crm.utils import parse_datetime, parsed_timestamp, utc_now

logger = logging.getLogger(__name__)

PRIORITIES = {"high", "medium", "low"}
GENERATED_SOURCES = {"ai", "rule"}


def list_tasks_with_conn(
    conn: sqlite3.Connection,
    *,
    status: str = "open",
    limit: int = 20,
    customer_id: int | None = None,
    source: str | None = None,
) -> list[sqlite3.Row]:
    where_clauses: list[str] = []
    params: list = []
    if status and status != "all":
        where_clauses.append("t.status = ?")
        params.append(status)
    if customer_id is not None:
        where_clauses.append("t.customer_id = ?")
        params.append(customer_id)
    if source in {"manual", "ai", "rule"}:
        where_clauses.append("COALESCE(t.source, 'manual') = ?")
        params.append(source)
    params.append(limit)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return conn.execute(
        f"""
        SELECT
            t.*,
            c.first_name,
            c.last_name,
            c.email
        FROM tasks t
        LEFT JOIN customers c ON c.id = t.customer_id
        {where_sql}
        ORDER BY
            CASE WHEN t.status = 'open' THEN 0 ELSE 1 END,
            COALESCE(t.priority_score, 50) DESC,
            CASE WHEN t.due_at IS NULL OR t.due_at = '' THEN 1 ELSE 0 END,
            t.due_at ASC,
            t.created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()


def list_tasks(
    *,
    status: str = "open",
    limit: int = 20,
    customer_id: int | None = None,
    source: str | None = None,
) -> list[sqlite3.Row]:
    conn = db_connection()
    try:
        return list_tasks_with_conn(conn, status=status, limit=limit, customer_id=customer_id, source=source)
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
                customer_id, title, details, task_type, due_at, status,
                priority, priority_score, source, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, 'open', 'medium', 50, 'manual', ?, NULL)
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


def latest_task_refresh_run_with_conn(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM task_refresh_runs
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()["count"] or 0)


def _week_cutoff(days: int = 7) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _parse_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_task_context(trigger_event: str) -> dict:
    conn = db_connection()
    try:
        week_cutoff = _week_cutoff(7)
        recent_cutoff = _week_cutoff(30)
        lapsed_cutoff = _week_cutoff(45)
        last_import = conn.execute(
            """
            SELECT *
            FROM import_runs
            WHERE status = 'completed'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        recent_import_summary = None
        import_warnings: list[str] = []
        if last_import:
            summary = _parse_json_object(last_import["intelligence_summary"])
            recent_import_summary = {
                "source_system": last_import["source_system"],
                "rows_received": last_import["rows_received"],
                "customers_created": last_import["customers_created"],
                "customers_updated": last_import["customers_updated"],
                "review_needed_rows": last_import["review_needed_rows"],
                "skipped_rows": last_import["skipped_rows"],
            }
            if summary.get("headline"):
                recent_import_summary["headline"] = str(summary["headline"])[:180]
            if isinstance(summary.get("risks"), list):
                import_warnings = [str(item)[:180] for item in summary["risks"][:4]]
            elif isinstance(summary.get("warnings"), list):
                import_warnings = [str(item)[:180] for item in summary["warnings"][:4]]

        top_source_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(source_system, ''), 'unknown') AS source_system, COUNT(*) AS count
            FROM customers
            GROUP BY COALESCE(NULLIF(source_system, ''), 'unknown')
            ORDER BY count DESC, source_system
            LIMIT 5
            """
        ).fetchall()
        settings = conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
        open_counts = task_counts_with_conn(conn)
        return {
            "trigger_event": trigger_event,
            "total_customers": _count(conn, "SELECT COUNT(*) AS count FROM customers"),
            "new_customers_this_week": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE created_at >= ?", (week_cutoff,)),
            "last_import_date": last_import["created_at"] if last_import else None,
            "recent_import_summary": recent_import_summary,
            "reachable_email_customers": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') <> ''"),
            "reachable_sms_customers": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE length(COALESCE(phone, '')) = 10"),
            "missing_email_count": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE COALESCE(email, '') = ''"),
            "missing_phone_count": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE length(COALESCE(phone, '')) <> 10"),
            "no_marketing_consent_count": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE marketing_consent = 0"),
            "recent_buyers_count": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE last_purchase_at >= ?", (recent_cutoff,)),
            "lapsed_buyers_count": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE total_spent > 0 AND last_purchase_at < ?", (lapsed_cutoff,)),
            "vip_or_high_value_customers_count": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE total_spent >= 250 OR lower(COALESCE(tags, '')) LIKE '%vip%' OR lower(COALESCE(tags, '')) LIKE '%wholesale%'"),
            "duplicate_review_count": _count(conn, "SELECT COUNT(*) AS count FROM duplicate_reviews WHERE decision = 'pending'"),
            "open_task_count": open_counts["open"],
            "high_priority_open_task_count": _count(conn, "SELECT COUNT(*) AS count FROM tasks WHERE status = 'open' AND priority = 'high'"),
            "scheduled_campaign_count": _count(conn, "SELECT COUNT(*) AS count FROM campaigns WHERE status = 'scheduled'"),
            "draft_campaign_count": _count(conn, "SELECT COUNT(*) AS count FROM campaigns WHERE status = 'draft'"),
            "recent_capture_count": _count(conn, "SELECT COUNT(*) AS count FROM touchpoints WHERE created_at >= ?", (week_cutoff,)),
            "recent_qr_capture_count": _count(conn, "SELECT COUNT(*) AS count FROM touchpoints WHERE created_at >= ? AND (touchpoint_type IN ('in_store_qr', 'receipt_qr') OR COALESCE(scan_location, '') <> '')", (week_cutoff,)),
            "top_source_systems": [{"source_system": row["source_system"], "count": row["count"]} for row in top_source_rows],
            "import_warnings": import_warnings,
            "campaign_ready_customer_count": _count(conn, "SELECT COUNT(*) AS count FROM customers WHERE marketing_consent = 1 AND (COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10)"),
            "current_weekday": datetime.now(UTC).strftime("%A"),
            "weekly_outreach_day": settings["weekly_outreach_day"] if settings else None,
        }
    finally:
        conn.close()


def _due_in(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).date().isoformat()


def _meaningfully_high(count: int, total: int, *, min_count: int = 10, ratio: float = 0.2) -> bool:
    return count >= min_count and (total == 0 or (count / total) >= ratio)


def generate_rule_based_tasks(context: dict) -> list[dict]:
    now = utc_now()
    total = int(context.get("total_customers") or 0)
    tasks: list[dict] = []

    last_import_dt = parse_datetime(context.get("last_import_date"))
    if not last_import_dt or last_import_dt < datetime.now(UTC) - timedelta(days=7):
        tasks.append({
            "title": "Import latest customer data before planning this week's outreach",
            "details": "The CRM has not recorded a completed import in the last 7 days. Bring in the latest Clover, Freshline, or customer export before choosing this week's campaign audience.",
            "task_type": "import",
            "priority": "high",
            "priority_score": 94,
            "due_at": _due_in(1),
            "source": "rule",
            "ai_reason": None,
            "related_metric": "No completed import in the last 7 days",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    duplicates = int(context.get("duplicate_review_count") or 0)
    if duplicates > 0:
        tasks.append({
            "title": "Review duplicate customer risks before campaign export",
            "details": f"Review {duplicates} duplicate customer risk{'s' if duplicates != 1 else ''} before exporting a campaign list so guests do not receive repeated messages.",
            "task_type": "general",
            "priority": "high",
            "priority_score": min(98, 82 + duplicates),
            "due_at": _due_in(1),
            "source": "rule",
            "ai_reason": None,
            "related_metric": f"{duplicates} duplicate review item{'s' if duplicates != 1 else ''}",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    ready = int(context.get("campaign_ready_customer_count") or 0)
    if ready > 0 and int(context.get("scheduled_campaign_count") or 0) == 0:
        tasks.append({
            "title": "Plan this week's campaign for reachable customers",
            "details": f"Plan a weekly offer for {ready} campaign-ready customer{'s' if ready != 1 else ''} with contact info and marketing consent.",
            "task_type": "campaign",
            "priority": "high",
            "priority_score": min(96, 78 + ready // 25),
            "due_at": _due_in(2),
            "source": "rule",
            "ai_reason": None,
            "related_metric": f"{ready} campaign-ready customers",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    missing_email = int(context.get("missing_email_count") or 0)
    missing_phone = int(context.get("missing_phone_count") or 0)
    if _meaningfully_high(max(missing_email, missing_phone), total):
        tasks.append({
            "title": "Clean up missing customer contact information",
            "details": f"Prioritize records missing contact routes: {missing_email} customers need email and {missing_phone} need a usable phone number before email or SMS outreach can scale.",
            "task_type": "general",
            "priority": "medium",
            "priority_score": 68,
            "due_at": _due_in(5),
            "source": "rule",
            "ai_reason": None,
            "related_metric": f"{missing_email} missing email, {missing_phone} missing phone",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    consent_gap = int(context.get("no_marketing_consent_count") or 0)
    if _meaningfully_high(consent_gap, total):
        tasks.append({
            "title": "Improve consent capture before email or SMS outreach",
            "details": f"{consent_gap} reachable or partial customer records do not have marketing consent. Tighten QR, checkout, and website opt-in prompts before the next export.",
            "task_type": "general",
            "priority": "medium",
            "priority_score": 64,
            "due_at": _due_in(6),
            "source": "rule",
            "ai_reason": None,
            "related_metric": f"{consent_gap} customers without marketing consent",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    qr_count = int(context.get("recent_qr_capture_count") or 0)
    if qr_count > 0:
        tasks.append({
            "title": "Follow up with new QR signups from recent capture activity",
            "details": f"{qr_count} recent QR signup{'s' if qr_count != 1 else ''} came in this week. Review them and decide whether they should receive the next specials campaign.",
            "task_type": "follow_up",
            "priority": "medium",
            "priority_score": min(86, 62 + qr_count * 2),
            "due_at": _due_in(2),
            "source": "rule",
            "ai_reason": None,
            "related_metric": f"{qr_count} recent QR captures",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    lapsed = int(context.get("lapsed_buyers_count") or 0)
    if _meaningfully_high(lapsed, total, min_count=5, ratio=0.08):
        tasks.append({
            "title": "Build a win-back campaign for lapsed customers",
            "details": f"{lapsed} customers have purchase history but no recent buying activity. Create a practical win-back offer before they drift further from Seaview.",
            "task_type": "campaign",
            "priority": "medium",
            "priority_score": min(88, 66 + lapsed // 10),
            "due_at": _due_in(4),
            "source": "rule",
            "ai_reason": None,
            "related_metric": f"{lapsed} lapsed buyers",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    high_value = int(context.get("vip_or_high_value_customers_count") or 0)
    if high_value > 0:
        tasks.append({
            "title": "Review high-value customers for targeted follow-up",
            "details": f"{high_value} VIP, wholesale, or high-spend customer{'s' if high_value != 1 else ''} may deserve a more personal follow-up than a broad campaign export.",
            "task_type": "follow_up",
            "priority": "medium",
            "priority_score": min(84, 60 + high_value // 3),
            "due_at": _due_in(3),
            "source": "rule",
            "ai_reason": None,
            "related_metric": f"{high_value} high-value customers",
            "generated_from_event": context["trigger_event"],
            "refreshed_at": now,
        })

    return tasks


def _system_setting(key: str, default: str = "") -> str:
    conn = db_connection()
    try:
        row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default
    except sqlite3.Error:
        return default
    finally:
        conn.close()


def _open_generated_task_summary() -> list[dict]:
    conn = db_connection()
    try:
        rows = conn.execute(
            """
            SELECT title, task_type, priority, related_metric, source
            FROM tasks
            WHERE status = 'open'
              AND COALESCE(source, 'manual') IN ('ai', 'rule')
            ORDER BY COALESCE(priority_score, 50) DESC, created_at DESC
            LIMIT 12
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def generate_ai_task_recommendations(trigger_event: str, context: dict) -> list[dict]:
    from crm.ai import AIError, api_key_from_settings, call_openai_json, model_from_settings

    api_key = api_key_from_settings(_system_setting)
    if not api_key:
        return []
    instructions = (
        "You are a CRM and marketing operations assistant for Seaview Crab Company, a small seafood business. "
        "Convert aggregated CRM context into a short prioritized task list for the owner and staff. "
        "Focus on weekly actions that improve repeat customer outreach, data quality, consent capture, campaign readiness, and revenue opportunity. "
        "Do not invent customer records. Do not recommend unavailable integrations. Keep tasks specific, practical, and measurable. "
        "Return only valid JSON as an object with a tasks array."
    )
    prompt = json.dumps(
        {
            "trigger_event": trigger_event,
            "aggregated_crm_context": context,
            "existing_open_ai_or_rule_tasks": _open_generated_task_summary(),
            "production_note": "This pilot uses file-based imports and export-based outreach. Do not recommend direct send integrations.",
            "expected_task_shape": {
                "title": "Review duplicate customer risks before campaign export",
                "details": "There are 14 possible duplicate records. Review these before exporting a campaign list so customers do not receive repeated messages.",
                "task_type": "general",
                "priority": "high",
                "priority_score": 92,
                "due_at": datetime.now(UTC).date().isoformat(),
                "ai_reason": "Duplicate risk can make campaign exports unreliable and hurt customer trust.",
                "related_metric": "14 duplicate review items",
                "generated_from_event": trigger_event,
            },
            "limits": "Return 3 to 7 tasks. Avoid vague titles like Improve marketing.",
        },
        indent=2,
        sort_keys=True,
    )
    try:
        response = call_openai_json(
            api_key=api_key,
            model=model_from_settings(_system_setting),
            instructions=instructions,
            prompt=prompt,
            max_output_tokens=1400,
        )
    except AIError:
        raise
    raw_tasks = response.get("tasks", [])
    if not isinstance(raw_tasks, list):
        return []
    return [_task for item in raw_tasks if (_task := sanitize_generated_task(item, "ai", trigger_event))]


def _normalize_priority(value: Any, score: int) -> str:
    priority = str(value or "").strip().lower()
    if priority in PRIORITIES:
        return priority
    if score >= 80:
        return "high"
    if score <= 35:
        return "low"
    return "medium"


def _clamp_score(value: Any) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = 50
    return max(0, min(100, score))


def sanitize_generated_task(item: Any, source: str, trigger_event: str) -> dict | None:
    if not isinstance(item, dict) or source not in GENERATED_SOURCES:
        return None
    title = re.sub(r"\s+", " ", str(item.get("title") or "").strip())
    details = re.sub(r"\s+", " ", str(item.get("details") or "").strip())
    if len(title) < 8 or len(details) < 20:
        return None
    task_type = str(item.get("task_type") or "general").strip()
    if task_type not in {value for value, _label in TASK_TYPES}:
        task_type = "general"
    score = _clamp_score(item.get("priority_score"))
    due_at = str(item.get("due_at") or "").strip()
    if due_at:
        parsed_due = parsed_timestamp(due_at)
        due_at = parsed_due[:10] if parsed_due else ""
    return {
        "title": title[:160],
        "details": details[:700],
        "task_type": task_type,
        "priority": _normalize_priority(item.get("priority"), score),
        "priority_score": score,
        "due_at": due_at or None,
        "source": source,
        "ai_reason": (str(item.get("ai_reason") or "").strip()[:500] or None) if source == "ai" else None,
        "related_metric": str(item.get("related_metric") or "").strip()[:180] or None,
        "generated_from_event": str(item.get("generated_from_event") or trigger_event).strip()[:80],
        "refreshed_at": utc_now(),
    }


def _title_signature(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    stop = {"the", "a", "an", "this", "that", "with", "for", "and", "or", "to", "of", "before"}
    return " ".join(word for word in words if word not in stop)[:90]


def _find_generated_duplicate(conn: sqlite3.Connection, task: dict) -> sqlite3.Row | None:
    source = task["source"]
    signature = _title_signature(task["title"])
    rows = conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE status = 'open'
          AND COALESCE(source, 'manual') = ?
        ORDER BY refreshed_at DESC, created_at DESC
        LIMIT 80
        """,
        (source,),
    ).fetchall()
    task_words = set(signature.split())
    for row in rows:
        if row["task_type"] == task["task_type"] and row["generated_from_event"] == task["generated_from_event"]:
            return row
        existing_words = set(_title_signature(row["title"]).split())
        if task_words and existing_words:
            overlap = len(task_words & existing_words) / max(len(task_words), len(existing_words))
            if overlap >= 0.72:
                return row
    return None


def _upsert_generated_tasks(conn: sqlite3.Connection, tasks: list[dict]) -> tuple[int, int]:
    created = updated = 0
    for task in tasks:
        duplicate = _find_generated_duplicate(conn, task)
        if duplicate:
            conn.execute(
                """
                UPDATE tasks
                SET title = ?, details = ?, task_type = ?, due_at = ?,
                    priority = ?, priority_score = ?, ai_reason = ?,
                    related_metric = ?, generated_from_event = ?, refreshed_at = ?
                WHERE id = ?
                  AND COALESCE(source, 'manual') IN ('ai', 'rule')
                """,
                (
                    task["title"], task["details"], task["task_type"], task["due_at"],
                    task["priority"], task["priority_score"], task["ai_reason"],
                    task["related_metric"], task["generated_from_event"], task["refreshed_at"],
                    duplicate["id"],
                ),
            )
            updated += 1
            continue
        conn.execute(
            """
            INSERT INTO tasks (
                customer_id, title, details, task_type, due_at, status,
                priority, priority_score, source, ai_reason, related_metric,
                generated_from_event, refreshed_at, created_at, completed_at
            ) VALUES (NULL, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                task["title"], task["details"], task["task_type"], task["due_at"],
                task["priority"], task["priority_score"], task["source"], task["ai_reason"],
                task["related_metric"], task["generated_from_event"], task["refreshed_at"], utc_now(),
            ),
        )
        created += 1
    return created, updated


def refresh_task_recommendations(trigger_event: str) -> dict:
    used_ai = 0
    used_fallback = 0
    created = 0
    updated = 0
    error_message = None
    context: dict = {}
    try:
        context = build_task_context(trigger_event)
        rule_tasks = [task for task in generate_rule_based_tasks(context) if sanitize_generated_task(task, "rule", trigger_event)]
        selected_tasks = rule_tasks
        try:
            ai_tasks = generate_ai_task_recommendations(trigger_event, context)
            if ai_tasks:
                selected_tasks = ai_tasks
                used_ai = 1
            else:
                used_fallback = 1
        except Exception as exc:  # AI must never block CRM operations.
            logger.warning("AI task recommendations unavailable: %s", exc)
            used_fallback = 1
            error_message = f"AI unavailable: {str(exc)[:220]}"
        conn = db_connection()
        try:
            created, updated = _upsert_generated_tasks(conn, selected_tasks)
            conn.execute(
                """
                INSERT INTO task_refresh_runs (
                    trigger_event, used_ai, used_fallback, tasks_created,
                    tasks_updated, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (trigger_event, used_ai, used_fallback, created, updated, error_message, utc_now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.exception("Task refresh failed for trigger=%s", trigger_event)
        error_message = str(exc)[:220]
        try:
            conn = db_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO task_refresh_runs (
                        trigger_event, used_ai, used_fallback, tasks_created,
                        tasks_updated, error_message, created_at
                    ) VALUES (?, 0, 1, 0, 0, ?, ?)
                    """,
                    (trigger_event, error_message, utc_now()),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to log task refresh failure")
    return {
        "trigger_event": trigger_event,
        "used_ai": bool(used_ai),
        "used_fallback": bool(used_fallback),
        "tasks_created": created,
        "tasks_updated": updated,
        "error_message": error_message,
        "context": context,
    }


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
