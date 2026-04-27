import sqlite3

from crm.config import TASK_TYPES
from crm.db import db_connection
from crm.utils import parsed_timestamp, utc_now


def list_tasks_with_conn(
    conn: sqlite3.Connection,
    *,
    status: str = "open",
    limit: int = 20,
    customer_id: int | None = None,
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


def list_tasks(
    *,
    status: str = "open",
    limit: int = 20,
    customer_id: int | None = None,
) -> list[sqlite3.Row]:
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
