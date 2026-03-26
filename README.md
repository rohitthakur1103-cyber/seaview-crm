# Seaview Crabshack CRM System

This project is a lightweight internal CRM for Seaview Crab Company. It is designed around two core needs:

1. give the owners a unified, contextual customer record across fragmented systems
2. help them market more systematically and on time every week
3. keep the system restricted to Seaview staff access

## What the current prototype does

- Centralizes customer records in SQLite
- Stores purchase history tied to each customer
- Imports CSV exports from legacy and fragmented systems
- Tracks touchpoints from website and in-person capture
- Creates a marketing hub with audience segments and campaign planning
- Exports audience CSVs so Seaview can send deals through its current marketing tools

## Core product idea

This is not a POS replacement. It is a CRM plus marketing operations layer that sits on top of Seaview's existing systems.

The CRM side provides:

- one customer profile per person
- source and acquisition context
- purchase history
- tags and notes
- capture and interaction history

The marketing side provides:

- weekly campaign planning
- lead capture from website and in-store interactions
- targeted audience segments
- exportable lists for email or SMS outreach

## Run the app

```bash
python3 app.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

The app creates its SQLite database automatically at `data/seaview_crm.db`.

## Render deployment

This repo includes a project-specific Render config in `render.yaml` for `seaview-crm`.

If you deploy from Render:

- Service name: `seaview-crm`
- Build command: `echo 'No additional build step required for seaview-crm'`
- Start command: `python3 app.py`

The app now reads `PORT` from the environment, so it works on Render without using generic WSGI placeholder names.

## Key pages

- `/` dashboard for CRM and marketing overview
- `/login` staff login gate
- `/customers` unified customer records
- `/marketing` campaign planning, capture, and weekly marketing workflow
- `/imports` legacy data imports
- `/capture` internal lead capture page for staff use

## Demo access

The current build includes a login screen so the app presents as staff-only software.

- For this demo, any username and password will enter the app.
- Later, this should be replaced with real staff authentication.

## Importing fragmented data

Use the Imports page to upload CSV exports from:

- Clover
- Constant Contact
- legacy Shopify
- website signup tools
- spreadsheets

Best-supported fields:

- `name` or `first_name` / `last_name`
- `email`
- `phone`
- `customer_id`
- `tags`
- `notes`
- `preferred_channel`
- `consent`
- `item_name`
- `quantity`
- `order_total`
- `purchase_date`

Matching logic:

- first match by email when present
- otherwise match by source customer ID
- add purchase rows when order data exists

## Weekly operating model

This prototype is built around a simple repeatable workflow:

1. Import the latest data exports at the start of the week.
2. Capture new leads from the website, QR codes, and counter interactions.
3. Review segments like recent buyers, VIPs, lapsed buyers, and missing-contact customers.
4. Save one or more weekly campaigns.
5. Export the audience list and send the deal through the current delivery tool.

## Suggested next steps

1. Add authentication before using this beyond demos.
2. Add real integrations for Clover, Constant Contact, and web forms.
3. Add campaign send logging and customer last-contacted tracking.
4. Add manual customer editing and staff notes.
5. Add analytics for conversion, retention, and signup rate by touchpoint.
