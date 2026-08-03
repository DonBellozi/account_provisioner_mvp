from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.routers import auth, employees
from app.security import ensure_bootstrap_admin

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.dry_run and settings.app_secret_key.startswith("change-me"):
        raise RuntimeError("Замените APP_SECRET_KEY перед рабочим запуском")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_bootstrap_admin(db, settings)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    same_site="lax",
    https_only=settings.app_base_url.lower().startswith("https://"),
    max_age=8 * 60 * 60,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(employees.router)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
    )
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/health")
def health():
    return {"status": "ok", "dry_run": settings.dry_run}
