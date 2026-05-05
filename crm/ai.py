from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

import certifi

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-4o-mini"


class AIError(Exception):
    """Raised when the optional OpenAI integration cannot complete a request."""


def mask_secret(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "Not configured"
    return "•" * max(len(cleaned) - 4, 4) + cleaned[-4:]


def api_key_from_settings(get_setting) -> str:
    return (os.environ.get("OPENAI_API_KEY") or get_setting("openai_api_key", "")).strip()


def model_from_settings(get_setting) -> str:
    return (os.environ.get("OPENAI_MODEL") or get_setting("openai_model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL


def is_configured(get_setting) -> bool:
    return bool(api_key_from_settings(get_setting))


def _extract_output_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts).strip()


def _coerce_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        raise AIError("OpenAI returned an empty response.")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise AIError("OpenAI did not return JSON.")
        return json.loads(match.group(0))


def call_openai_json(
    *,
    api_key: str,
    model: str,
    instructions: str,
    prompt: str,
    max_output_tokens: int = 900,
    timeout: int = 25,
) -> dict[str, Any]:
    if not api_key:
        raise AIError("OpenAI API key is not configured.")
    body = json.dumps(
        {
            "model": model,
            "instructions": instructions,
            "input": f"Return valid JSON only.\n\n{prompt}",
            "text": {"format": {"type": "json_object"}},
            "max_output_tokens": max_output_tokens,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AIError(f"OpenAI request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise AIError(f"OpenAI request failed: {exc.reason}") from exc
    payload = json.loads(raw)
    return _coerce_json(_extract_output_text(payload))


def generate_weekly_brief(*, api_key: str, model: str, context: dict[str, Any]) -> dict[str, Any]:
    instructions = (
        "You are Seaview Crab Company's CRM operating assistant. "
        "Use only the provided CRM metrics. Be specific, practical, and business-focused. "
        "Return JSON only with keys: headline, summary, actions, risks. "
        "actions must be a list of 4 objects with title, reason, cta."
    )
    prompt = json.dumps(context, indent=2, sort_keys=True)
    return call_openai_json(
        api_key=api_key,
        model=model,
        instructions=instructions,
        prompt=prompt,
        max_output_tokens=900,
    )


def generate_campaign_draft(*, api_key: str, model: str, context: dict[str, Any]) -> dict[str, Any]:
    instructions = (
        "You create concise, usable small-business seafood marketing campaigns. "
        "Use the selected audience and real CRM counts. Avoid hype. "
        "Return JSON only with keys: title, offer_details, goal. "
        "title should be under 70 characters. offer_details should be 2-4 sentences with a clear CTA."
    )
    prompt = json.dumps(context, indent=2, sort_keys=True)
    return call_openai_json(
        api_key=api_key,
        model=model,
        instructions=instructions,
        prompt=prompt,
        max_output_tokens=650,
    )


def generate_capture_copy(*, api_key: str, model: str, context: dict[str, Any]) -> dict[str, Any]:
    instructions = (
        "You write mobile-first QR signup page copy for Seaview Crab Company. "
        "Make it short enough for a customer at checkout. "
        "Return JSON only with keys: primary_offer_hook, capture_prompt, default_capture_cta. "
        "primary_offer_hook under 90 characters, capture_prompt under 180 characters, CTA under 24 characters."
    )
    prompt = json.dumps(context, indent=2, sort_keys=True)
    return call_openai_json(
        api_key=api_key,
        model=model,
        instructions=instructions,
        prompt=prompt,
        max_output_tokens=450,
    )


def generate_import_brief(*, api_key: str, model: str, context: dict[str, Any]) -> dict[str, Any]:
    instructions = (
        "You are Seaview Crab Company's customer-file operator. "
        "Think like a practical seafood owner or floor lead reviewing this week's upload before weekend service. "
        "Use only the provided import metrics and CRM context. "
        "Sound operational, local, and specific to seafood retail and in-store capture. "
        "Talk about reachable guests, weekend specials, text list growth, counter capture, duplicate cleanup, and who staff can market to right now. "
        "Do not use generic SaaS or corporate CRM language. Do not say engagement, leverage, optimize, ecosystem, funnel, or synergy. "
        "Keep the tone direct, grounded, and useful for a crab shack operator. "
        "Only use exact numeric claims that appear in the provided latest import data. Do not infer counts from example lists. "
        "If the latest import source_type is clover, focus on capture gap, reachable Freshline audience, and checkout capture. "
        "If the latest import source_type is freshline, focus on cleanup, campaign-ready audience, and exportable lists. "
        "Do not mention duplicate cleanup or invalid phone counts unless those counts are explicitly present in latest_import.source_metrics.cleanup. "
        "Return JSON only with keys: headline, summary, takeaways, actions, chart_notes. "
        "takeaways must be a list of exactly 3 short strings. "
        "actions must be a list of exactly 3 objects with keys: title, reason, cta. "
        "chart_notes must be a list of exactly 2 short strings that explain what the charts mean. "
        "Every action should sound like something staff can do this week."
    )
    prompt = json.dumps(context, indent=2, sort_keys=True)
    return call_openai_json(
        api_key=api_key,
        model=model,
        instructions=instructions,
        prompt=prompt,
        max_output_tokens=900,
    )
