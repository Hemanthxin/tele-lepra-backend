"""WhatsApp Cloud API webhook endpoint.

Two methods on the same URL ``/webhook`` as Meta requires:

* ``GET``  — verification handshake. Meta sends
  ``?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=<nonce>``
  and expects the endpoint to echo ``hub.challenge`` (as plain text /
  ``text/plain``) with HTTP 200 *only when* the verify token matches
  the value configured on the server side. Mismatch must return 403.

* ``POST`` — event delivery. Meta posts message-status updates and
  inbound messages here. We acknowledge with 200 quickly and record
  the payload in Firestore for audit/troubleshooting.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..core.config import settings
from ..core.firebase import get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])


@router.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta verification handshake.

    Spec: https://developers.facebook.com/docs/graph-api/webhooks/getting-started#verification-requests
    """
    expected = settings.wa_verify_token
    if not expected:
        log.error("WhatsApp webhook verify failed: WA_VERIFY_TOKEN is not set on the server")
        raise HTTPException(500, "Server missing WA_VERIFY_TOKEN")

    if hub_mode != "subscribe" or hub_verify_token != expected:
        log.warning(
            "WhatsApp webhook verify rejected (mode=%r token_match=%s)",
            hub_mode,
            hub_verify_token == expected,
        )
        raise HTTPException(403, "Verification failed")

    # Echo the challenge as plain text — this is what Meta expects.
    return hub_challenge or ""


@router.post("/webhook")
async def receive_webhook(request: Request):
    """Receive message-status updates and inbound messages from Meta.

    We always return 200 quickly — if we return 4xx/5xx Meta will retry
    with exponential backoff and eventually disable the subscription.
    """
    raw = await request.body()

    # Optional: validate X-Hub-Signature-256 if WA_APP_SECRET is configured.
    if settings.wa_app_secret:
        sig_header = request.headers.get("x-hub-signature-256", "")
        if not sig_header.startswith("sha256="):
            log.warning("WhatsApp webhook missing X-Hub-Signature-256 header")
            raise HTTPException(401, "Missing signature")
        expected = (
            "sha256="
            + hmac.new(
                settings.wa_app_secret.encode(),
                raw,
                hashlib.sha256,
            ).hexdigest()
        )
        if not hmac.compare_digest(expected, sig_header):
            log.warning("WhatsApp webhook signature mismatch")
            raise HTTPException(401, "Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        payload = {"_raw_text": raw.decode("utf-8", errors="replace")}

    # Persist for audit. Use a fire-and-forget pattern; never let storage
    # errors propagate and cause Meta to retry.
    try:
        db = get_db()
        db.collection("whatsapp_events").add(
            {
                "received_at": datetime.now(timezone.utc),
                "object": payload.get("object") if isinstance(payload, dict) else None,
                "payload": payload,
            }
        )
    except Exception as e:
        log.warning("Failed to persist webhook event (continuing with 200): %s", e)

    return {"status": "received"}
