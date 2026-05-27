from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .core.config import settings
from .routers import admin, appointments, auth, cases, patients, uploads

app = FastAPI(title="Tele-Leprosy Triage API", version="0.1.0")

_origins = [o.strip() for o in settings.frontend_origin.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    # Match every *.vercel.app preview URL (PR previews, branch deploys, etc.)
    allow_origin_regex=r"^https://([a-z0-9-]+\.)*vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(patients.router)
app.include_router(cases.router)
app.include_router(appointments.router)
app.include_router(admin.router)
app.include_router(uploads.router)


@app.get("/")
def health():
    return {"status": "ok", "service": "tele-leprosy-api"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
