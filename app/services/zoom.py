"""
Zoom Meeting SDK signature generation (Meeting SDK Auth / JWT App).

The front-end Meeting SDK needs a short-lived signature minted server-side
so the SDK key/secret never leave the backend. Real meeting creation can
use Zoom REST API; for MVP we mint an ad-hoc meeting number per case.
"""
import base64
import hashlib
import hmac
import time
from typing import Literal

from ..core.config import settings


def generate_signature(meeting_number: str, role: Literal[0, 1]) -> str:
    """role=0 attendee (patient), role=1 host (MO)."""
    if not settings.zoom_sdk_key or not settings.zoom_sdk_secret:
        # Dev fallback so the UI flow is testable without real creds.
        return f"dev-signature-for-{meeting_number}-role{role}"

    ts = int(round(time.time() * 1000)) - 30000
    msg = f"{settings.zoom_sdk_key}{meeting_number}{ts}{role}".encode()
    secret = settings.zoom_sdk_secret.encode()
    digest = base64.b64encode(hmac.new(secret, msg, hashlib.sha256).digest()).decode()
    raw = f"{settings.zoom_sdk_key}.{meeting_number}.{ts}.{role}.{digest}".encode()
    return base64.b64encode(raw).decode().rstrip("=")


def create_meeting_stub(case_id: str) -> dict:
    """
    MVP stub: deterministic meeting id derived from the case id.

    Production should call POST https://api.zoom.us/v2/users/me/meetings
    with a Server-to-Server OAuth token and persist the returned id.
    """
    meeting_number = str(abs(hash(case_id)) % 10_000_000_000).zfill(10)
    return {
        "meeting_number": meeting_number,
        "topic": f"Tele-Leprosy consult {case_id[:8]}",
        "join_url": f"https://zoom.us/j/{meeting_number}",
    }
