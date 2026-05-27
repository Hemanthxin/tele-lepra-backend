import uuid

from fastapi import APIRouter, Depends, File, UploadFile

from ..core.firebase import get_bucket
from ..core.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_PATIENT,
    CurrentUser,
    get_current_user,
    require_roles,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post(
    "/image",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_ADMIN, ROLE_PATIENT))],
)
async def upload_image(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Uploads to Firebase Storage and returns a public-ish download URL."""
    bucket = get_bucket()
    ext = (file.filename or "img").rsplit(".", 1)[-1].lower()
    key = f"lesions/{user.uid}/{uuid.uuid4().hex}.{ext}"
    blob = bucket.blob(key)
    content = await file.read()
    blob.upload_from_string(content, content_type=file.content_type)
    blob.make_public()
    return {"url": blob.public_url, "key": key}
