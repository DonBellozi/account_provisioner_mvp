from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LocalUser, UserRole

password_hash = PasswordHash.recommended()


class CSRFMismatchError(Exception):
    """Сессионный CSRF-токен отсутствует или не совпадает с токеном формы."""


@dataclass(frozen=True)
class CurrentUser:
    username: str
    role: str
    source: str


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return password_hash.verify(password, hashed)
    except Exception:
        return False


def ensure_bootstrap_admin(db: Session, settings: Settings) -> None:
    existing = db.scalar(select(LocalUser).where(LocalUser.username == settings.bootstrap_admin_username))
    if existing:
        if (
            not settings.dry_run
            and settings.auth_mode in {"local", "hybrid"}
            and verify_password("ChangeMeNow!123", existing.password_hash)
        ):
            raise RuntimeError("Измените стандартный пароль локального администратора перед рабочим запуском")
        return
    if not settings.dry_run and settings.bootstrap_admin_password == "ChangeMeNow!123":
        raise RuntimeError("Замените стандартный BOOTSTRAP_ADMIN_PASSWORD перед рабочим запуском")
    if len(settings.bootstrap_admin_password) < 12:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD должен быть не короче 12 символов")
    db.add(
        LocalUser(
            username=settings.bootstrap_admin_username,
            password_hash=hash_password(settings.bootstrap_admin_password),
            role=UserRole.ADMIN,
        )
    )
    db.commit()


def authenticate_local(db: Session, username: str, password: str) -> CurrentUser | None:
    user = db.scalar(select(LocalUser).where(LocalUser.username == username, LocalUser.is_active.is_(True)))
    if not user or not verify_password(password, user.password_hash):
        return None
    return CurrentUser(username=user.username, role=user.role.value, source="local")


def get_current_user(request: Request) -> CurrentUser:
    data: dict[str, Any] | None = request.session.get("user")
    if not data:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return CurrentUser(
        username=str(data["username"]),
        role=str(data.get("role", "operator")),
        source=str(data.get("source", "local")),
    )


def get_or_create_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


def validate_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("csrf")
    if not expected or not secrets.compare_digest(str(expected), submitted):
        raise CSRFMismatchError
