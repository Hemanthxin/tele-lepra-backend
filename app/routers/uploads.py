import uuid

from fastapi import APIRouter, Depends, File, UploadFile

from ..core.firebase import upload_public
from ..core.security import (
    ROLE_ADMIN,
    ROLE_AGENT,
    CurrentUser,
    get_current_user,
    require_roles,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post(
    "/image",
    dependencies=[Depends(require_roles(ROLE_AGENT, ROLE_ADMIN))],
)
async def upload_image(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Uploads to Firebase Storage and returns an openable download URL."""
    ext = (file.filename or "img").rsplit(".", 1)[-1].lower()
    key = f"lesions/{user.uid}/{uuid.uuid4().hex}.{ext}"
    content = await file.read()
    url = upload_public(content, key, file.content_type or "application/octet-stream")
    return {"url": url, "key": key}
