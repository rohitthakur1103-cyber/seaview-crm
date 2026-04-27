import sqlite3

from crm.db import db_connection
from crm.utils import utc_now


def get_app_settings() -> sqlite3.Row:
    conn = db_connection()
    try:
        return conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
    finally:
        conn.close()


def save_app_settings(fields: dict) -> None:
    conn = db_connection()
    try:
        conn.execute(
            """
            UPDATE app_settings
            SET business_name = ?, primary_location = ?, weekly_import_owner = ?, weekly_outreach_day = ?,
                primary_offer_hook = ?, capture_prompt = ?, preferred_primary_data_source = ?,
                default_capture_cta = ?, duplicate_review_required_before_campaign_export = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                fields.get("business_name", "").strip() or "Seaview Crab Company",
                fields.get("primary_location", "").strip() or None,
                fields.get("weekly_import_owner", "").strip() or None,
                fields.get("weekly_outreach_day", "").strip() or None,
                fields.get("primary_offer_hook", "").strip() or None,
                fields.get("capture_prompt", "").strip() or None,
                fields.get("preferred_primary_data_source", "").strip() or "clover",
                fields.get("default_capture_cta", "").strip() or "Get Seaview updates",
                1 if fields.get("duplicate_review_required_before_campaign_export") else 0,
                utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
