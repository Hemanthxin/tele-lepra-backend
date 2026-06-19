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


def _post_template(
    url: str,
    headers: dict,
    normalized: str,
    template_name: str,
    params: Iterable[str],
    language: str,
    document_url: str | None = None,
    document_filename: str | None = None,
):
    components: list[dict] = []
    if document_url:
        # Template header is TEXT type with {{1}} = the report URL as a plain-text link.
        # WhatsApp rejects _ in text parameters (parses as italic formatting — error 132007).
        # Percent-encode bare underscores; %5F is equivalent and browsers resolve it fine.
        safe_url = document_url.replace("_", "%5F")
        components.append({
            "type": "header",
            "parameters": [{"type": "text", "text": safe_url}],
        })
    components.append({
        "type": "body",
        "parameters": [
            {"type": "text", "text": str(p)} for p in params
        ],
    })
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }
    return httpx.post(url, json=payload, headers=headers, timeout=10.0)


def send_template(
    to: str,
    template_name: str,
    params: Iterable[str],
    *,
    language: str = "en",
    document_url: str | None = None,
    document_filename: str | None = None,
) -> dict:
    """Send an approved WhatsApp template message.

    Tries the configured `language` first; if Meta returns 132001 (template
    not found in that locale), falls back through the common English variants.
    """
    if not is_configured():
        log.info("WhatsApp not configured — would have sent %s to %s", template_name, to)
        return {"skipped": True, "reason": "not_configured"}

    normalized = _to_e164(to)
    if not normalized:
        log.warning("WhatsApp: invalid recipient phone %r — skipping", to)
        return {"skipped": True, "reason": "invalid_phone"}

    url = f"{GRAPH_BASE}/{settings.wa_phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.wa_token}",
        "Content-Type": "application/json",
    }

    # Try the configured locale first, then common English variants. De-dup
    # while preserving order.
    candidates: list[str] = []
    for c in (language, "en_US", "en", "en_GB"):
        if c and c not in candidates:
            candidates.append(c)

    last_err: httpx.HTTPStatusError | None = None
    for code in candidates:
        try:
            r = _post_template(
                url, headers, normalized, template_name, params, code,
                document_url=document_url, document_filename=document_filename,
            )
            r.raise_for_status()
            if code != language:
                log.info(
                    "WhatsApp template %s sent via fallback locale %s (configured was %s)",
                    template_name, code, language,
                )
            return r.json()
        except httpx.HTTPStatusError as e:
            last_err = e
            body = e.response.text or ""
            # 132001 = template not found in this locale — try next candidate.
            if "132001" in body or e.response.status_code == 404:
                continue
            # 132012 = parameter format mismatch — retrying other locales won't help.
            if "132012" in body:
                log.warning(
                    "WhatsApp template %s: parameter format mismatch (132012). "
                    "The header/body definition in Meta Business Manager does not match "
                    "what the code sent (check header type and parameter count). Body: %s",
                    template_name, body[:400],
                )
                break
            # Any other error — fail fast.
            break
        except httpx.HTTPError as e:
            log.error("WhatsApp transport error: %s", e)
            raise WhatsAppError(str(e)) from e

    if last_err is not None:
        body = last_err.response.text or ""
        if "132012" in body:
            raise WhatsAppError(
                f"Template {template_name!r} parameter format mismatch (132012) — "
                "check the template's header type and parameter count in Meta Business Manager."
            ) from last_err
        log.warning(
            "WhatsApp template %s not configured in any of %s — skipping. Body: %s",
            template_name, candidates, body[:200],
        )
        raise WhatsAppError(
            f"Template {template_name!r} not configured in WhatsApp Business (tried {candidates})."
        ) from last_err
    return {"skipped": True, "reason": "template_missing"}


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
