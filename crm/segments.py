from datetime import UTC, datetime, timedelta


def campaign_exclusion_clause(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    email = f"lower(trim(COALESCE({prefix}email, '')))"
    local_part = f"substr({email}, 1, instr({email} || '@', '@') - 1)"
    domain = f"substr({email}, instr({email}, '@') + 1)"
    full_name = f"lower(trim(COALESCE({prefix}first_name, '') || ' ' || COALESCE({prefix}last_name, '')))"
    return (
        f"({local_part} LIKE '%seaview%' "
        f"OR {domain} IN ('freshline.io', 'seaviewcrab.com') "
        f"OR {local_part} IN ('support', 'service') "
        f"OR {full_name} = 'freshline support' "
        f"OR {full_name} LIKE '%seaview crab%test%' "
        f"OR {email} IN ('nathanbaneking@gmail.com', 'sccpayables@gmail.com', 'scckeeping@gmail.com', 'sccwholesale@gmail.com'))"
    )


def segment_definitions() -> dict:
    recent_cutoff = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
    lapsed_cutoff = (datetime.now(UTC) - timedelta(days=45)).replace(microsecond=0).isoformat()
    signup_cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
    contact_clause = "(COALESCE(email, '') <> '' OR length(COALESCE(phone, '')) = 10)"
    named_clause = "(COALESCE(first_name, '') <> '' OR COALESCE(last_name, '') <> '')"
    freshline_clause = f"source_system = 'freshline_customer_export' AND NOT {campaign_exclusion_clause()}"
    full_name_expr = "lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')))"
    duplicate_name_clause = f"""
        {full_name_expr} IN (
            SELECT lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')))
            FROM customers
            WHERE source_system = 'freshline_customer_export'
              AND NOT {campaign_exclusion_clause()}
              AND COALESCE(first_name, '') <> ''
              AND COALESCE(last_name, '') <> ''
              AND trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) <> ''
              AND COALESCE(email, '') <> ''
            GROUP BY lower(trim(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')))
            HAVING COUNT(DISTINCT lower(email)) > 1
        )
    """

    return {
        "email_ready": {
            "label": "Ready to Email",
            "description": "Freshline customers with a usable email, excluding internal records.",
            "recommended_channel": "Email",
            "where": f"{freshline_clause} AND COALESCE(email, '') <> ''",
            "params": (),
        },
        "sms_ready": {
            "label": "Ready to Text",
            "description": "Freshline customers with a valid 10-digit phone, excluding internal records.",
            "recommended_channel": "SMS",
            "where": f"{freshline_clause} AND length(COALESCE(phone, '')) = 10",
            "params": (),
        },
        "clean_campaign_ready": {
            "label": "Clean & Campaign-Ready",
            "description": "Freshline customers with email and a verified 10-digit phone, excluding internal records.",
            "recommended_channel": "Email or SMS",
            "where": f"""
                {freshline_clause}
                AND COALESCE(email, '') <> ''
                AND length(COALESCE(phone, '')) = 10
            """,
            "params": (),
        },
        "needs_attention": {
            "label": "Needs Attention",
            "description": "Freshline records with invalid phones or duplicate names that should be fixed before outreach.",
            "recommended_channel": "Cleanup",
            "where": f"""
                {freshline_clause}
                AND (
                    (COALESCE(phone, '') <> '' AND length(COALESCE(phone, '')) <> 10)
                    OR ({duplicate_name_clause})
                )
            """,
            "params": (),
        },
        "recent_buyers": {
            "label": "Recent buyers",
            "description": "Customers who purchased in the last 30 days and should get a follow-up deal.",
            "recommended_channel": "Email",
            "where": f"last_purchase_at IS NOT NULL AND last_purchase_at >= ? AND {contact_clause}",
            "params": (recent_cutoff,),
        },
        "lapsed_buyers": {
            "label": "Lapsed buyers",
            "description": "Customers who bought before but have gone quiet and need a win-back offer.",
            "recommended_channel": "SMS",
            "where": f"last_purchase_at IS NOT NULL AND last_purchase_at < ? AND total_spent > 0 AND {contact_clause}",
            "params": (lapsed_cutoff,),
        },
        "vip_customers": {
            "label": "VIP customers",
            "description": "Top spenders who should get first access to premium or seasonal inventory.",
            "recommended_channel": "Email",
            "where": f"total_spent >= 250 AND {contact_clause}",
            "params": (),
        },
        "newsletter_prospects": {
            "label": "Newsletter prospects",
            "description": "Reachable contacts with little or no purchase history who need nurturing.",
            "recommended_channel": "Email",
            "where": "COALESCE(email, '') <> '' AND total_spent = 0",
            "params": (),
        },
        "wholesale_accounts": {
            "label": "Wholesale accounts",
            "description": "Customers tagged as wholesale for B2B follow-up and restock reminders.",
            "recommended_channel": "Either",
            "where": "lower(COALESCE(tags, '')) LIKE '%wholesale%'",
            "params": (),
        },
        "new_signups": {
            "label": "New signups this week",
            "description": "Fresh website or in-person captures that should get a welcome offer fast.",
            "recommended_channel": "Email or SMS",
            "where": "acquisition_source IN ('website_homepage', 'online_order_flow', 'in_store_qr', 'counter_conversation', 'event_booth') AND created_at >= ?",
            "params": (signup_cutoff,),
        },
        "missing_contact": {
            "label": "Missing contact info",
            "description": "Customers with no email or phone who need a capture campaign in person.",
            "recommended_channel": "In-store",
            "where": "COALESCE(email, '') = '' AND length(COALESCE(phone, '')) <> 10",
            "params": (),
        },
    }
