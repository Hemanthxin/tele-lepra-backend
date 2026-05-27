"""WhatsApp Cloud API client.

Sends template messages to patients via Meta's WhatsApp Business Cloud API.
Disabled when WA_TOKEN / WA_PHONE_ID are not configured — the caller still
records the notification in Firestore so we don't lose it.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

import httpx

from ..core.config import settings

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppError(Exception):
    """Raised when the Cloud API returns a non-2xx response."""


def is_configured() -> bool:
    return bool(settings.wa_token and settings.wa_phone_id)


def _to_e164(phone: str | None) -> str | None:
    """Normalize a phone number to E.164 digits (no '+').

    The Cloud API expects digits only, e.g. '919876543210' for +91 98765 43210.
    Returns None if the input is empty or has fewer than 10 digits after
    stripping non-digits.
    """
    if not phone:
        return None
    digits = re.sub(r"\D+", "", phone)
    if len(digits) < 10:
        return None
    # If the number is 10 digits, assume India (+91). Otherwise trust the
    # country code already encoded.
    if len(digits) == 10:
        digits = "91" + digits
    return digits


def send_template(
    to: str,
    template_name: str,
    params: Iterable[str],
    *,
    language: str = "en",
) -> dict:
    """Send an approved WhatsApp template message.

    `to` should be the patient's phone in any common format; it is normalized
    to E.164 internally. `params` populates the {{1}}, {{2}}, … placeholders
    in the template body, in order.
    """
    if not is_configured():
        log.info("WhatsApp not configured — would have sent %s to %s", template_name, to)
        return {"skipped": True, "reason": "not_configured"}

    normalized = _to_e164(to)
    if not normalized:
        log.warning("WhatsApp: invalid recipient phone %r — skipping", to)
        return {"skipped": True, "reason": "invalid_phone"}

    url = f"{GRAPH_BASE}/{settings.wa_phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(p)} for p in params
                    ],
                }
            ],
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.wa_token}",
        "Content-Type": "application/json",
    }

    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        log.error(
            "WhatsApp API error %s for template=%s body=%s",
            e.response.status_code,
            template_name,
            e.response.text[:500],
        )
        raise WhatsAppError(
            f"WhatsApp {e.response.status_code}: {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        log.error("WhatsApp transport error: %s", e)
        raise WhatsAppError(str(e)) from e


def send_text(to: str, body: str) -> dict:
    """Free-form text message — only valid within the 24h customer window."""
    if not is_configured():
        log.info("WhatsApp not configured — would have sent text to %s", to)
        return {"skipped": True, "reason": "not_configured"}

    normalized = _to_e164(to)
    if not normalized:
        return {"skipped": True, "reason": "invalid_phone"}

    url = f"{GRAPH_BASE}/{settings.wa_phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized,
        "type": "text",
        "text": {"body": body},
    }
    headers = {
        "Authorization": f"Bearer {settings.wa_token}",
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        log.error("WhatsApp text send failed: %s", e)
        raise WhatsAppError(str(e)) from e
