# Seaview CRM Demo Handoff

## Demo Purpose

Seaview CRM is a business-facing customer intelligence demo for Seaview Crabshack. It shows how messy customer data from Clover, Freshline, Constant Contact, spreadsheets, website leads, QR captures, and staff notes can become usable customer intelligence.

The product story is:

1. Import fragmented customer files.
2. Clean and match customer records.
3. Identify duplicate or messy data.
4. Measure who can actually be reached.
5. Find campaign-ready customer segments.
6. Generate an owner-ready weekly operating brief.
7. Export audience lists for existing marketing tools.

This is a demo and pilot concept, not a final production implementation.

## What The Demo Shows

- Customer import preview with source-aware field mapping.
- Background import processing for large customer files.
- Import progress page with rows processed, percent complete, estimated timing, and completion summary.
- Customer reachability metrics for email, phone, consent, and campaign readiness.
- Duplicate review workflow for possible customer matches.
- Dashboard operating brain with what changed, what matters, and what to do next.
- Customer records with source context, contact quality, notes, purchases, and tasks.
- Capture workflows for QR, website, events, receipts, and staff-entered leads.
- Marketing audience preview and campaign export.
- AI weekly brief and AI import brief when AI is configured.
- Rule-based recommendations when AI is not configured.

## Problem It Solves

Seaview has demand and customer history, but the data is scattered across disconnected tools. That makes it hard to answer simple business questions:

- Which customers can we actually reach?
- Which records are duplicated or messy?
- Which customers are missing email, phone, or consent?
- Which customer segments are ready for a campaign?
- What should the owner or manager do this week to drive repeat business?
- Is the latest imported data ready to use inside the CRM?

The demo makes those questions visible in one operating system.

## Demo-Ready Workflows

Use these workflows for a clean presentation:

1. Open the dashboard and show the CRM Operating Brain.
2. Go to Imports and upload a realistic Seaview customer export.
3. Review the import preview: rows, usable contact, marketing allowed, campaign-ready, duplicate risk, and unmapped columns.
4. Confirm the import and keep the import status page open.
5. Watch the progress page update without full-page reloads.
6. When complete, show the import completion summary and recommended next action.
7. Open duplicate review and explain how risky matches are separated from automatic merges.
8. Open Customers and show a customer record with reachability and source context.
9. Open Marketing and preview an exportable audience.
10. Generate or open an AI brief to show the weekly operating review format.
11. Return to the dashboard and show how the system turns the import into owner-ready actions.

## Suggested Demo Script

Opening:

"Seaview has customer data, but it is fragmented across tools. This demo shows how we can turn those files into a weekly customer operating system."

Import:

"First, we upload the latest customer export. Before anything is saved, the CRM previews whether the file is usable, who is reachable, who has consent, and where duplicate risk exists."

Progress:

"Large files run as background import jobs. The owner is not left staring at a frozen screen. The page shows rows processed, percent complete, stage, timing estimate, and what the system is doing."

Customer intelligence:

"Once the file is imported, Seaview can see which customers are campaign-ready, which need cleanup, and which duplicates should be reviewed before outreach."

AI:

"AI is used for business-readable briefs and recommendations. The source of truth stays in CRM counts and data quality metrics, not raw AI guesses."

Close:

"The value is not just storing customers. The value is turning messy customer files into weekly decisions: who to reach, what to clean, and what campaign to run next."

## Supported Demo Data Sources

- Seaview customer export.
- Freshline customer export.
- Clover customer or purchase export.
- Constant Contact contact export.
- Legacy CSV or spreadsheet exports.
- Website signup and QR capture data.
- Manual staff notes and lead captures.

The current demo uses file upload and capture forms. Direct vendor integrations are intentionally future work.

## AI Usage

AI is used for:

- Weekly business brief formatting.
- Import readout interpretation.
- Campaign draft suggestions.
- Capture-page copy suggestions.
- Optional task recommendation language.

AI should receive summarized CRM metrics and business context only. It should not receive massive raw customer files or private customer lists.

## Rule-Based Logic

Rule-based logic is used for:

- Import field detection.
- Customer matching.
- Duplicate review routing.
- Contact reachability.
- Marketing consent and campaign-readiness counts.
- Dashboard metrics.
- Fallback task recommendations.
- Export blocking when duplicate review is required.

This means the CRM can still be demoed if AI is unavailable.

## What Is Not Productionized Yet

These items are intentionally out of scope for the demo:

- Direct Clover, Freshline, Constant Contact, or POS integrations.
- Production authentication and staff lifecycle policies.
- Managed production database.
- Automated backup and restore process.
- Monitoring, alerting, and operational support.
- Formal data retention and privacy process.
- Real campaign sending from inside the CRM.
- Multi-location permissions or enterprise controls.
- Customer-facing self-service portal.

The demo should be presented as a working pilot and product direction, not as a final production system.

## If Seaview Decides To Implement This

If Seaview chooses to move forward, the next build phase should include:

- Real vendor integrations for the systems Seaview actually uses.
- Production authentication and staff onboarding.
- Managed database instead of demo SQLite.
- Scheduled backups and restore testing.
- Monitoring for imports, errors, and performance.
- API key ownership under Seaview-controlled operations.
- Data privacy and consent process.
- Clear support and maintenance plan.
- A decision on whether campaign sending stays external or becomes part of the CRM.

Do not include private access details, API keys, or private setup details in demo handoff materials.

## Known Demo Limitations

- Large imports are handled as background jobs, but the hosted demo can still be affected by Render cold starts or limited instance resources.
- SQLite is acceptable for a pilot demo, but a real production rollout should use a managed database.
- Vendor integrations are represented through upload workflows, not direct sync.
- AI output depends on whether AI is configured for the demo environment.
- Campaign export creates files for external tools; it does not send messages directly.
- Duplicate review identifies likely matches, but final merge policy should be decided with Seaview.

## Final QA Checklist

Before presenting:

- Dashboard loads and shows the CRM Operating Brain.
- Imports page loads.
- A realistic customer export previews successfully.
- Confirm import starts a background job.
- Import status updates in place without full-page reload flashes.
- Completion summary appears with next-action buttons.
- Duplicate review page loads.
- Customers page and at least one customer profile load.
- Marketing audience preview loads.
- Campaign export downloads a CSV.
- Capture page and QR/customer signup flows load.
- AI brief page is readable if AI is configured.
- Rule-based fallbacks remain usable if AI is unavailable.
- Live health check responds quickly.
- No private access details, private keys, or raw customer files are committed.
