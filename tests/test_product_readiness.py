import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="seaview-crm-tests-"))
os.environ.setdefault("SEAVIEW_SESSION_SECRET", "test-secret")

import app  # noqa: E402
import crm.ai  # noqa: E402
import crm.imports  # noqa: E402


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

    def test_import_run_pending_index_migrates_after_column(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = app.db_connection()
        try:
            conn.execute(
                """
                CREATE TABLE import_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_system TEXT NOT NULL,
                    filename TEXT,
                    rows_received INTEGER NOT NULL,
                    customers_created INTEGER NOT NULL,
                    customers_updated INTEGER NOT NULL,
                    purchase_events_created INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        app.init_db()
        app.ensure_runtime_schema()

        conn = app.db_connection()
        try:
            import_columns = {row["name"] for row in conn.execute("PRAGMA table_info(import_runs)").fetchall()}
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list(import_runs)").fetchall()}
            self.assertIn("pending_import_id", import_columns)
            self.assertIn("idx_import_runs_pending_import_id", indexes)
        finally:
            conn.close()

    def test_healthcheck_does_not_wait_on_database(self):
        original_db_connection = app.db_connection

        def blocked_db_connection():
            raise AssertionError("healthcheck should not open SQLite")

        app.db_connection = blocked_db_connection
        try:
            status, payload = app.healthcheck_response()
        finally:
            app.db_connection = original_db_connection

        self.assertEqual(app.HTTPStatus.OK, status)
        self.assertIn(b'"database": "not_checked"', payload)

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

    def test_public_qr_capture_is_simple_and_saves_customer(self):
        html = app.render_public_capture(
            "/join/qr/counter",
            message="",
        )
        self.assertIn(b"Join Seaview updates", html)
        self.assertIn(b'data-qr-form', html)
        self.assertIn(b'name="source_label"', html)
        self.assertIn(b'name="location_tag"', html)
        self.assertIn(b"Email or phone is required", html)
        self.assertNotIn(b"Scan for weekly seafood specials", html)
        self.assertNotIn(b"Included", html)
        self.assertNotIn(b"capture-hero", html)
        self.assertNotIn(b"wide-grid", html)

        capture = app.create_touchpoint_capture(
            {
                "first_name": "QR",
                "last_name": "Guest",
                "email": "qr-guest@example.com",
                "phone": "",
                "consent_email": "1",
                "consent_sms": "1",
                "preferred_channel": "sms",
                "touchpoint_type": "in_store_qr",
                "source_label": "Front Counter QR",
                "location_tag": "qr_counter",
            },
            public_signup=True,
        )
        self.assertIsNone(capture["error"])
        conn = app.db_connection()
        try:
            customer = conn.execute(
                "SELECT first_name, last_name, email, marketing_consent FROM customers WHERE email = ?",
                ("qr-guest@example.com",),
            ).fetchone()
            touchpoint = conn.execute(
                "SELECT touchpoint_type, scan_location FROM touchpoints WHERE customer_id = ?",
                (capture["customer_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("QR", customer["first_name"])
        self.assertEqual("Guest", customer["last_name"])
        self.assertEqual(1, customer["marketing_consent"])
        self.assertEqual("in_store_qr", touchpoint["touchpoint_type"])
        self.assertEqual("counter", touchpoint["scan_location"])

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

    def test_import_preview_contains_wide_sample_rows(self):
        row = {
            "Customer ID": "123",
            "First Name": "Wide",
            "Last Name": "Preview",
            "Phone Number": "555-555-5555",
            "Email Address": "wide-preview@example.com",
            "Address Line 1": "123 Seaview Way",
            "Address Line 2": "",
            "Address Line 3": "",
            "City": "Raleigh",
            "State / Province": "NC",
            "Postal / Zip Code": "27601",
            "Country": "US",
            "Customer Since": "2026-04-09",
            "Marketing Allowed": "No",
            "Additional Address Info": "Door by the loading area",
        }
        analysis = app.analyze_import_rows("seaview_customer_export", [row])
        app.save_pending_import(
            "wide-preview-test",
            source_system="seaview_customer_export",
            filename="wide-preview.csv",
            rows=[row],
            analysis=analysis,
        )

        html = app.render_import_preview("wide-preview-test", user={"username": "seaview", "role": "admin"})
        self.assertIn(b"import-preview-sample-panel", html)
        self.assertIn(b"scrollable-table import-preview-sample-table", html)
        self.assertIn(b"Scroll sideways to inspect additional columns", html)
        self.assertIn(b"Additional Address Info", html)

    def test_import_analysis_uses_cached_customer_matching(self):
        seed_row = {
            "First Name": "Cached",
            "Last Name": "Buyer",
            "Email": "cached-buyer@example.com",
            "Marketing Consent": "yes",
        }
        result = app.import_rows("legacy_csv", "cache-seed.csv", [seed_row])
        self.assertIsNone(result["error_message"])

        original = crm.imports.import_row_decision_with_conn

        def fail_if_row_by_row_db_matching_is_used(*_args, **_kwargs):
            raise AssertionError("analysis should use the import match cache")

        try:
            crm.imports.import_row_decision_with_conn = fail_if_row_by_row_db_matching_is_used
            analysis = app.analyze_import_rows(
                "legacy_csv",
                [
                    seed_row,
                    {
                        "First Name": "New",
                        "Last Name": "Buyer",
                        "Email": "new-buyer@example.com",
                        "Marketing Consent": "yes",
                    },
                ],
            )
        finally:
            crm.imports.import_row_decision_with_conn = original

        self.assertEqual(1, analysis["merge_rows"])
        self.assertEqual(1, analysis["create_rows"])

    def test_import_job_creation_does_not_process_rows_inline(self):
        rows = [
            {
                "First Name": "Async",
                "Last Name": "One",
                "Email": "async-one@example.com",
                "Marketing Consent": "yes",
            },
            {
                "First Name": "Async",
                "Last Name": "Two",
                "Email": "async-two@example.com",
                "Marketing Consent": "yes",
            },
        ]
        analysis = app.analyze_import_rows("legacy_csv", rows)
        app.save_pending_import(
            "async-job-test",
            source_system="legacy_csv",
            filename="async.csv",
            rows=rows,
            analysis=analysis,
        )

        before = self._count("SELECT COUNT(*) AS count FROM customers WHERE email LIKE 'async-%@example.com'")
        job = app.create_import_job_from_pending("async-job-test")
        after = self._count("SELECT COUNT(*) AS count FROM customers WHERE email LIKE 'async-%@example.com'")

        self.assertIsNone(job["error"])
        self.assertEqual(before, after)
        run = app.get_import_run(job["import_run_id"])
        self.assertEqual("queued", run["status"])
        self.assertEqual(0, run["rows_processed"])
        self.assertEqual("queued", run["progress_stage"])
        status_html = app.render_import_run_status(job["import_run_id"], user={"username": "seaview", "role": "admin"})
        self.assertIn(b"Importing Seaview customer data", status_html)
        self.assertIn(b"Estimated time remaining", status_html)
        self.assertIn(b"Checking duplicate records", status_html)
        self.assertIn(b"data-import-run-status", status_html)
        self.assertIn(b"/imports/runs/", status_html)
        self.assertIn(b"/status.json", status_html)
        self.assertNotIn(b"window.location.reload", status_html)
        status_payload = app.import_run_status_payload(job["import_run_id"])
        self.assertTrue(status_payload["is_active"])
        self.assertEqual("queued", status_payload["status"])
        self.assertIn("Checking duplicate records", status_payload["stage_html"])

    def test_import_job_processor_completes_and_records_progress(self):
        rows = [
            {
                "First Name": "Worker",
                "Last Name": "One",
                "Email": "worker-one@example.com",
                "Marketing Consent": "yes",
            },
            {
                "First Name": "Worker",
                "Last Name": "Two",
                "Email": "worker-two@example.com",
                "Marketing Consent": "yes",
            },
        ]
        analysis = app.analyze_import_rows("legacy_csv", rows)
        app.save_pending_import(
            "worker-job-test",
            source_system="legacy_csv",
            filename="worker.csv",
            rows=rows,
            analysis=analysis,
        )
        job = app.create_import_job_from_pending("worker-job-test")
        result = app.process_import_job(job["import_run_id"], batch_size=1)

        self.assertIsNone(result["error_message"])
        run = app.get_import_run(job["import_run_id"])
        self.assertEqual("completed", run["status"])
        self.assertEqual(2, run["rows_processed"])
        self.assertEqual(2, run["customers_created"])
        self.assertEqual("complete", run["progress_stage"])
        self.assertTrue(run["completed_at"])
        self.assertIsNone(app.load_pending_import("worker-job-test"))
        status_html = app.render_import_run_status(job["import_run_id"], user={"username": "seaview", "role": "admin"})
        self.assertIn(b"Your Seaview customer data is ready to use", status_html)
        self.assertIn(b"Generate AI brief", status_html)
        status_payload = app.import_run_status_payload(job["import_run_id"])
        self.assertFalse(status_payload["is_active"])
        self.assertEqual(100, status_payload["percent"])

    def test_import_job_failure_records_error(self):
        rows = [
            {
                "First Name": "Missing",
                "Last Name": "Source",
                "Email": "missing-source@example.com",
            }
        ]
        analysis = app.analyze_import_rows("legacy_csv", rows)
        app.save_pending_import(
            "missing-source-job-test",
            source_system="legacy_csv",
            filename="missing-source.csv",
            rows=rows,
            analysis=analysis,
        )
        job = app.create_import_job_from_pending("missing-source-job-test")
        app.delete_pending_import("missing-source-job-test")

        result = app.process_import_job(job["import_run_id"], batch_size=1)

        self.assertIsNotNone(result["error_message"])
        run = app.get_import_run(job["import_run_id"])
        self.assertEqual("failed", run["status"])
        self.assertIn("source data", run["error_message"])

    def test_ai_weekly_brief_render_is_structured(self):
        html = app.render_ai_weekly_brief(
            {
                "headline": "Weekly customer file is ready for manager review",
                "executive_summary": "The CRM is ready for focused follow-up. Review cleanup items before exporting a list.",
                "key_metrics": [
                    {"label": "Campaign-ready", "value": "42", "context": "Reachable and consented customers."},
                    {"label": "Duplicate review", "value": "3", "context": "Potential repeated outreach risk."},
                    {"label": "Capture gap", "value": "12", "context": "Customers missing email or phone."},
                ],
                "sections": {
                    "key_customer_insights": ["Recent buyers should be prioritized for timely seafood specials."],
                    "data_quality_issues": ["Duplicate candidates need review before export."],
                    "campaign_opportunities": ["Use the reachable audience for a weekend offer."],
                    "risks_or_missing_information": ["Missing consent limits campaign reach."],
                },
                "actions": [
                    {
                        "title": "Review duplicate risks",
                        "reason": "Protect customers from repeated messages.",
                        "cta": "Open duplicate review",
                        "owner": "Manager",
                        "timing": "Before export",
                    }
                ],
            },
            user={"username": "seaview", "role": "admin"},
        )
        for label in (
            b"Executive Summary",
            b"Key Customer Insights",
            b"Data Quality Issues",
            b"Campaign Opportunities",
            b"Recommended Next Actions",
            b"Risks or Missing Information",
        ):
            self.assertIn(label, html)
        self.assertIn(b"brief-metric-card", html)

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

    def test_import_ai_brief_can_be_saved_with_import_history(self):
        result = app.import_rows(
            "legacy_csv",
            "brief.csv",
            [
                {
                    "First Name": "Brief",
                    "Last Name": "Tester",
                    "Email": "brief-tester@example.com",
                    "Marketing Consent": "yes",
                    "Order Total": "55",
                    "Purchased At": "2026-05-01",
                }
            ],
        )
        self.assertIsNone(result["error_message"])
        self.assertTrue(result["import_run_id"])

        app.set_setting("openai_api_key", "sk-test")
        original = app.generate_import_brief

        def fake_import_brief(**_kwargs):
            return {
                "headline": "Import readout ready",
                "summary": "The upload added a reachable customer profile.",
                "takeaways": ["One new reachable customer was added."],
                "actions": [{"title": "Send welcome note", "reason": "New reachable contact", "cta": "Draft the message"}],
            }

        try:
            app.generate_import_brief = fake_import_brief
            brief, error = app.generate_saved_import_ai_brief(result["import_run_id"])
        finally:
            app.generate_import_brief = original

        self.assertIsNone(error)
        self.assertEqual("Import readout ready", brief["headline"])

        conn = app.db_connection()
        try:
            row = conn.execute("SELECT ai_brief_json, ai_brief_created_at FROM import_runs WHERE id = ?", (result["import_run_id"],)).fetchone()
        finally:
            conn.close()
        self.assertIn("Import readout ready", row["ai_brief_json"])
        self.assertTrue(row["ai_brief_created_at"])

    def test_import_ai_brief_generic_failure_is_non_blocking(self):
        result = app.import_rows(
            "legacy_csv",
            "brief-failure.csv",
            [
                {
                    "First Name": "Brief",
                    "Last Name": "Failure",
                    "Email": "brief-failure@example.com",
                    "Marketing Consent": "yes",
                }
            ],
        )
        self.assertIsNone(result["error_message"])
        self.assertTrue(result["import_run_id"])

        app.set_setting("openai_api_key", "sk-test")
        original = app.generate_import_brief

        def fake_import_brief(**_kwargs):
            raise RuntimeError("model returned malformed JSON")

        try:
            app.generate_import_brief = fake_import_brief
            brief, error = app.generate_saved_import_ai_brief(result["import_run_id"])
        finally:
            app.generate_import_brief = original

        self.assertIsNone(brief)
        self.assertIn("model returned malformed JSON", error)

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
