import base64
import datetime
import json
import logging
import os
from functools import lru_cache

import firebase_admin
from firebase_admin import auth, credentials, firestore, storage

from .config import settings

log = logging.getLogger("firebase")


def _load_credentials() -> credentials.Certificate:
    """Resolve Firebase credentials from (in order):

    1. FIREBASE_CREDENTIALS_JSON  — raw JSON string in an env var
    2. FIREBASE_CREDENTIALS_B64   — base64-encoded JSON in an env var
    3. FIREBASE_CREDENTIALS_PATH  — path to serviceAccountKey.json on disk

    The env-var modes are how we ship Firebase creds to platforms like
    Render or Vercel that don't keep arbitrary files in the runtime FS.
    """
    raw = settings.firebase_credentials_json
    if raw:
        return credentials.Certificate(json.loads(raw))

    b64 = settings.firebase_credentials_b64
    if b64:
        return credentials.Certificate(json.loads(base64.b64decode(b64)))

    cred_path = settings.firebase_credentials_path
    if cred_path and os.path.exists(cred_path):
        return credentials.Certificate(cred_path)

    raise RuntimeError(
        "Firebase credentials not configured. Set one of "
        "FIREBASE_CREDENTIALS_JSON, FIREBASE_CREDENTIALS_B64, "
        "or FIREBASE_CREDENTIALS_PATH."
    )


@lru_cache(maxsize=1)
def init_firebase():
    if not firebase_admin._apps:
        cred = _load_credentials()
        opts = {}
        if settings.firebase_storage_bucket:
            opts["storageBucket"] = settings.firebase_storage_bucket
        firebase_admin.initialize_app(cred, opts)
    return firebase_admin.get_app()


def get_db():
    init_firebase()
    return firestore.client()


def get_bucket():
    init_firebase()
    return storage.bucket()


# Default lifetime for signed-URL fallbacks (7 days — WhatsApp needs the link
# reachable long enough for the patient to open it).
_SIGNED_URL_DAYS = 7


def upload_public(content: bytes, key: str, content_type: str) -> str:
    """Upload bytes to Storage and return a URL the recipient can open.

    Prefers a public URL (legacy buckets with per-object ACLs). Modern Firebase
    buckets enable uniform bucket-level access, where ``make_public`` raises —
    in that case we fall back to a time-limited signed URL (the service-account
    key can sign offline). Raises on a genuine upload failure (e.g. missing
    bucket) so callers can log it.
    """
    blob = get_bucket().blob(key)
    blob.upload_from_string(content, content_type=content_type)
    try:
        blob.make_public()
        return blob.public_url
    except Exception as e:  # uniform bucket-level access, or no ACL permission
        log.info("make_public failed for %s (%s) — using signed URL", key, e)
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(days=_SIGNED_URL_DAYS),
            method="GET",
        )


def get_auth():
    init_firebase()
    return auth
