import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="seaview-crm-tests-"))
os.environ.setdefault("SEAVIEW_SESSION_SECRET", "test-secret")

import app  # noqa: E402
import crm.ai  # noqa: E402


class ProductReadinessTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(os.environ["DATA_DIR"])
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        app.init_db()
        app.ensure_runtime_schema()
        app.seed_demo_data()

    def _count(self, sql, params=()):
        conn = app.db_connection()
        try:
            return conn.execute(sql, params).fetchone()["count"]
        finally:
            conn.close()

    def test_schema_migrates_task_brain_fields(self):
        conn = app.db_connection()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            self.assertTrue(
                {
                    "priority",
                    "priority_score",
                    "source",
                    "ai_reason",
                    "related_metric",
                    "generated_from_event",
                    "refreshed_at",
                }.issubset(columns)
            )
            self.assertEqual(
                "task_refresh_runs",
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='task_refresh_runs'"
                ).fetchone()["name"],
            )
        finally:
            conn.close()

    def test_manual_tasks_survive_generated_refresh_and_dedupe(self):
        manual = app.create_task(
            {
                "title": "Manual owner follow-up",
                "details": "This should stay manual.",
                "task_type": "general",
            }
        )
        first = app.refresh_task_recommendations("manual_refresh")
        second = app.refresh_task_recommendations("manual_refresh")
        conn = app.db_connection()
        try:
            row = conn.execute("SELECT title, source, status FROM tasks WHERE id = ?", (manual["task_id"],)).fetchone()
            generated = conn.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE COALESCE(source, 'manual') IN ('ai', 'rule')"
            ).fetchone()["count"]
        finally:
            conn.close()
        self.assertEqual("manual", row["source"])
        self.assertEqual("open", row["status"])
        self.assertGreaterEqual(first["tasks_created"], 1)
        self.assertEqual(0, second["tasks_created"])
        self.assertGreaterEqual(second["tasks_updated"], 1)
        self.assertGreaterEqual(generated, 1)

    def test_import_and_capture_refreshes_are_logged(self):
        result = app.import_rows(
            "legacy_csv",
            "smoke.csv",
            [
                {
                    "First Name": "Smoke",
                    "Last Name": "Buyer",
                    "Email": "smoke-buyer@example.com",
                    "Marketing Consent": "yes",
                    "Order Total": "42",
                    "Purchased At": "2026-04-30",
                }
            ],
        )
        self.assertIsNone(result["error_message"])
        app.refresh_task_recommendations("import_completed")
        capture = app.create_touchpoint_capture(
            {
                "email": "qr-test@example.com",
                "consent_email": "1",
                "touchpoint_type": "in_store_qr",
                "location_tag": "qr_counter",
            },
            public_signup=True,
        )
        self.assertIsNone(capture["error"])
        app.refresh_task_recommendations("public_capture_submitted")
        self.assertEqual(1, self._count("SELECT COUNT(*) AS count FROM task_refresh_runs WHERE trigger_event = 'import_completed'"))
        self.assertEqual(
            1,
            self._count("SELECT COUNT(*) AS count FROM task_refresh_runs WHERE trigger_event = 'public_capture_submitted'"),
        )

    def test_pending_import_preview_survives_memory_cache_loss(self):
        rows = [
            {
                "First Name": "Preview",
                "Last Name": "Tester",
                "Email": "preview@example.com",
                "Marketing Consent": "yes",
            }
        ]
        analysis = app.analyze_import_rows("legacy_csv", rows)
        app.save_pending_import(
            "pending-preview-test",
            source_system="legacy_csv",
            filename="preview.csv",
            rows=rows,
            analysis=analysis,
        )
        app.PENDING_IMPORTS.clear()

        pending = app.load_pending_import("pending-preview-test")
        self.assertIsNotNone(pending)
        self.assertEqual("legacy_csv", pending["source_system"])
        self.assertEqual(rows, pending["rows"])
        self.assertTrue(pending["analysis"]["can_import"])

        popped = app.pop_pending_import("pending-preview-test")
        self.assertIsNotNone(popped)
        self.assertIsNone(app.load_pending_import("pending-preview-test"))

    def test_ai_success_and_failure_paths_are_safe(self):
        app.set_setting("openai_api_key", "sk-test")
        original = crm.ai.call_openai_json

        def fake_success(**_kwargs):
            return {
                "tasks": [
                    {
                        "title": "Review duplicate customer risks before campaign export",
                        "details": "Review possible duplicate records before exporting this week so customers do not receive repeated outreach.",
                        "task_type": "general",
                        "priority": "high",
                        "priority_score": 91,
                        "due_at": "2026-05-03",
                        "ai_reason": "Duplicate risk can make campaign exports unreliable.",
                        "related_metric": "Duplicate review items",
                        "generated_from_event": "manual_refresh",
                    }
                ]
            }

        try:
            crm.ai.call_openai_json = fake_success
            success = app.refresh_task_recommendations("manual_refresh")
            self.assertTrue(success["used_ai"])

            def fake_failure(**_kwargs):
                raise crm.ai.AIError("forced failure")

            crm.ai.call_openai_json = fake_failure
            failure = app.refresh_task_recommendations("manual_refresh")
            self.assertTrue(failure["used_fallback"])
            self.assertIsNotNone(failure["error_message"])
        finally:
            crm.ai.call_openai_json = original

    def test_campaign_export_logging_and_render_smoke(self):
        before = self._count("SELECT COUNT(*) AS count FROM outreach_history WHERE event_type = 'exported'")
        app.log_segment_export("email_ready")
        app.refresh_task_recommendations("campaign_exported")
        after = self._count("SELECT COUNT(*) AS count FROM outreach_history WHERE event_type = 'exported'")
        self.assertEqual(before + 1, after)
        self.assertIn(b"CRM Operating Brain", app.render_dashboard(user={"username": "seaview", "role": "admin"}))
        self.assertIn(
            b"Export Preview",
            app.render_campaign_export_preview("email_ready", user={"username": "seaview", "role": "admin"}),
        )


if __name__ == "__main__":
    unittest.main()
