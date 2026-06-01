"""Zoom integration: Server-to-Server OAuth + Meeting SDK signatures.

Two independent flows live here, and the env vars for each are independent:

  * ``create_meeting(...)``  uses ZOOM_ACCOUNT_ID + ZOOM_CLIENT_ID +
    ZOOM_CLIENT_SECRET (Server-to-Server OAuth app) to create real Zoom
    meetings via the REST API. Returns the real meeting number,
    ``join_url`` (for participants), and ``start_url`` (host-only,
    short-lived).

  * ``generate_signature(...)``  uses ZOOM_SDK_KEY + ZOOM_SDK_SECRET
    (Meeting SDK app) to mint short-lived JWTs that authorise the
    front-end Meeting SDK embed. Only needed if you embed Zoom in-app
    instead of opening the join_url in a new tab.

If either set of creds is blank, the corresponding function degrades to
a deterministic stub so the dev flow still works.
"""
from __future__ import annotations

import base64
import logging
import secrets
import time
from datetime import datetime
from typing import Literal, Optional

import httpx
import jwt as pyjwt

from ..core.config import settings
from .ids import display_id


def _generate_passcode() -> str:
    """6-digit numeric passcode — easy to copy/paste and Zoom-compatible.
    Zoom passcodes allow [a-zA-Z0-9@_*-]; we stick to digits for typability.
    """
    return "".join(str(secrets.randbelow(10)) for _ in range(6))

log = logging.getLogger(__name__)

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"

# Simple in-process cache. Each gunicorn/uvicorn worker has its own copy;
# tokens last ~1h so this is fine in practice.
_token_cache: dict = {"token": None, "expires_at": 0.0}


class ZoomError(Exception):
    pass


def _s2s_configured() -> bool:
    return bool(
        settings.zoom_account_id
        and settings.zoom_client_id
        and settings.zoom_client_secret
    )


def _get_access_token() -> Optional[str]:
    """Fetch a Server-to-Server OAuth access token, with caching."""
    if not _s2s_configured():
        return None

    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    basic = base64.b64encode(
        f"{settings.zoom_client_id}:{settings.zoom_client_secret}".encode()
    ).decode()
    try:
        r = httpx.post(
            ZOOM_OAUTH_URL,
            params={
                "grant_type": "account_credentials",
                "account_id": settings.zoom_account_id,
            },
            headers={"Authorization": f"Basic {basic}"},
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("Zoom OAuth failed: %s", e)
        raise ZoomError(f"Zoom OAuth failed: {e}") from e

    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["token"]


def create_meeting(
    case_id: str,
    *,
    topic: Optional[str] = None,
    scheduled_at: Optional[datetime] = None,
    duration_minutes: int = 30,
) -> dict:
    """Create a Zoom meeting. Falls back to a stub if S2S OAuth is disabled.

    Returns at minimum: ``meeting_number``, ``topic``, ``join_url``.
    When real, also returns ``start_url`` (host-only) and ``password``.
    """
    if not _s2s_configured():
        return _create_meeting_stub(case_id)

    token = _get_access_token()
    passcode = _generate_passcode()
    payload = {
        "topic": topic or f"Tele-Leprosy consult {display_id(case_id)}",
        "type": 2 if scheduled_at else 1,  # 1 = instant, 2 = scheduled
        "duration": duration_minutes,
        "password": passcode,  # explicit so we always know it; embedded in join_url by Zoom
        "settings": {
            "host_video": True,
            "participant_video": True,
            "join_before_host": True,
            "jbh_time": 0,
            "mute_upon_entry": False,
            "approval_type": 2,
            "waiting_room": False,
            "audio": "both",
            "auto_recording": "none",
        },
    }
    if scheduled_at is not None:
        # Zoom expects ISO-8601 UTC, e.g. 2026-05-27T10:00:00Z
        if hasattr(scheduled_at, "isoformat"):
            iso = scheduled_at.isoformat()
            if iso.endswith("+00:00"):
                iso = iso[:-6] + "Z"
            payload["start_time"] = iso
        else:
            payload["start_time"] = str(scheduled_at)

    try:
        r = httpx.post(
            f"{ZOOM_API_BASE}/users/me/meetings",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.error("Zoom create meeting %s: %s", e.response.status_code, e.response.text[:500])
        raise ZoomError(f"Zoom {e.response.status_code}: {e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        log.error("Zoom create meeting transport error: %s", e)
        raise ZoomError(str(e)) from e

    data = r.json()
    # Prefer the passcode we sent — Zoom occasionally omits it from the
    # response payload depending on account-level security settings, but
    # the meeting is created with the value we passed.
    return {
        "meeting_number": str(data["id"]),
        "topic": data.get("topic"),
        "join_url": data.get("join_url"),
        "start_url": data.get("start_url"),
        "password": data.get("password") or passcode,
    }


def _create_meeting_stub(case_id: str) -> dict:
    """Dev fallback: deterministic meeting id derived from the case id."""
    meeting_number = str(abs(hash(case_id)) % 10_000_000_000).zfill(10)
    return {
        "meeting_number": meeting_number,
        "topic": f"Tele-Leprosy consult {display_id(case_id)}",
        "join_url": f"https://zoom.us/j/{meeting_number}",
        "start_url": None,
        "password": "",
    }


def generate_signature(meeting_number: str, role: Literal[0, 1]) -> str:
    """Mint a Meeting SDK JWT.

    ``role=0`` attendee (patient), ``role=1`` host (MO).
    Spec: https://developers.zoom.us/docs/meeting-sdk/auth/
    """
    if not settings.zoom_sdk_key or not settings.zoom_sdk_secret:
        return f"dev-signature-for-{meeting_number}-role{role}"

    now = int(time.time())
    exp = now + 60 * 60 * 2  # 2 hours
    payload = {
        "sdkKey": settings.zoom_sdk_key,
        "appKey": settings.zoom_sdk_key,
        "mn": str(meeting_number),
        "role": role,
        "iat": now,
        "exp": exp,
        "tokenExp": exp,
    }
    return pyjwt.encode(payload, settings.zoom_sdk_secret, algorithm="HS256")
