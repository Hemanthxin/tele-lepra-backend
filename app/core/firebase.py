import base64
import json
import os
from functools import lru_cache

import firebase_admin
from firebase_admin import auth, credentials, firestore, storage

from .config import settings


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


def get_auth():
    init_firebase()
    return auth
