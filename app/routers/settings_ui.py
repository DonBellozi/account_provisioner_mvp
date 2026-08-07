from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.security import get_or_create_csrf, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings")
def settings_overview(request: Request):
    current = require_admin(request)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": current,
            "csrf": get_or_create_csrf(request),
        },
    )
