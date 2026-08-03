from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.security import authenticate_local, get_or_create_csrf, validate_csrf
from app.services.ad import ActiveDirectoryService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
def login_page(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "csrf": get_or_create_csrf(request),
            "error": "",
            "auth_mode": settings.auth_mode,
        },
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    username = username.strip()
    current = None

    if settings.auth_mode in {"local", "hybrid"}:
        current = authenticate_local(db, username, password)

    if current is None and settings.auth_mode in {"ad", "hybrid"} and settings.ad_login_enabled:
        if ActiveDirectoryService(settings).authenticate_operator(username, password):
            current = type("AuthResult", (), {"username": username, "role": "operator", "source": "ad"})()

    if current is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf": get_or_create_csrf(request),
                "error": "Неверный логин или пароль",
                "auth_mode": settings.auth_mode,
            },
            status_code=401,
        )

    request.session["user"] = {
        "username": current.username,
        "role": current.role,
        "source": current.source,
    }
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    validate_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
