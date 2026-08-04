from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import AuditLog, DomainAccessUser, UserRole
from app.security import (
    get_or_create_csrf,
    normalize_username,
    require_admin,
    validate_csrf,
)
from app.services.ad import ActiveDirectoryService

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

ALLOWED_MANAGED_ROLES = {
    UserRole.OPERATOR.value: UserRole.OPERATOR,
    UserRole.ADMIN.value: UserRole.ADMIN,
}


def _role_from_form(value: str) -> UserRole:
    role = ALLOWED_MANAGED_ROLES.get(value.strip().lower())
    if role is None:
        raise ValueError("Неизвестная роль")
    return role


def _redirect(message: str = "", error: str = "") -> RedirectResponse:
    params: list[str] = []
    if message:
        params.append(f"message={quote_plus(message)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(f"/admin/access{suffix}", status_code=303)


@router.get("")
def admin_root(request: Request):
    require_admin(request)
    return RedirectResponse("/admin/access", status_code=303)


@router.get("/access")
def access_management(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    current = require_admin(request)
    query = q.strip()
    search_results = []
    search_error = ""

    assigned = db.scalars(
        select(DomainAccessUser).order_by(
            DomainAccessUser.is_active.desc(),
            DomainAccessUser.display_name,
            DomainAccessUser.username,
        )
    ).all()
    assigned_by_username = {item.username: item for item in assigned}

    if query:
        try:
            search_results = ActiveDirectoryService(settings).search_users(query, limit=20)
        except Exception as exc:
            search_error = str(exc)

    return templates.TemplateResponse(
        request,
        "admin_access.html",
        {
            "user": current,
            "csrf": get_or_create_csrf(request),
            "bootstrap_admin_username": normalize_username(settings.bootstrap_admin_username),
            "assigned_users": assigned,
            "assigned_by_username": assigned_by_username,
            "search_query": query,
            "search_results": search_results,
            "search_error": search_error,
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/access/users")
def add_access_user(
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)

    try:
        normalized = normalize_username(username)
        selected_role = _role_from_form(role)
        directory_user = ActiveDirectoryService(settings).get_user(normalized)
        if directory_user is None:
            raise ValueError("Пользователь не найден в Active Directory")
        if not directory_user.is_enabled:
            raise ValueError("Нельзя назначить доступ отключенной учетной записи AD")

        access = db.scalar(
            select(DomainAccessUser).where(DomainAccessUser.username == normalized)
        )
        action = "admin_access_update" if access else "admin_access_add"

        if access is None:
            access = DomainAccessUser(
                username=normalized,
                created_by=current.username,
            )
            db.add(access)

        access.display_name = directory_user.display_name
        access.email = directory_user.email
        access.role = selected_role
        access.is_active = True

        db.add(
            AuditLog(
                actor=current.username,
                action=action,
                target=normalized,
                result="success",
                details=f"role={selected_role.value}; active=true",
            )
        )
        db.commit()
        return _redirect(message=f"Доступ для {normalized} назначен")
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/access/users/{access_id}/update")
def update_access_user(
    access_id: int,
    request: Request,
    role: str = Form(...),
    is_active: str | None = Form(None),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)

    try:
        access = db.get(DomainAccessUser, access_id)
        if access is None:
            raise ValueError("Запись доступа не найдена")

        selected_role = _role_from_form(role)
        active = is_active == "on"
        access.role = selected_role
        access.is_active = active

        db.add(
            AuditLog(
                actor=current.username,
                action="admin_access_update",
                target=access.username,
                result="success",
                details=f"role={selected_role.value}; active={str(active).lower()}",
            )
        )
        db.commit()
        return _redirect(message=f"Права для {access.username} обновлены")
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))


@router.post("/access/users/{access_id}/delete")
def delete_access_user(
    access_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    current = require_admin(request)

    try:
        access = db.get(DomainAccessUser, access_id)
        if access is None:
            raise ValueError("Запись доступа не найдена")

        username = access.username
        db.delete(access)
        db.add(
            AuditLog(
                actor=current.username,
                action="admin_access_delete",
                target=username,
                result="success",
            )
        )
        db.commit()
        return _redirect(message=f"Доступ для {username} удален")
    except Exception as exc:
        db.rollback()
        return _redirect(error=str(exc))
