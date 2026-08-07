from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.db import get_db
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.ad import ActiveDirectoryService
from app.services.hr_registry import HRRegistryService
from app.services.mailer import CredentialMailer
from app.services.onec_import import OneCImportService
from app.services.zimbra import ZimbraService
from sqlalchemy.orm import Session

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _yes_no(value: bool) -> str:
    return "Да" if value else "Нет"


def _set_not_set(value: str) -> str:
    return "задан" if str(value or "").strip() else "не задан"


def _file_state(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return "не задан"
    return "найден" if Path(path).is_file() else "не найден"


def _integration_overview(settings: Settings) -> dict[str, dict[str, object]]:
    ad_configured = bool(
        settings.ad_server
        and settings.ad_bind_dn
        and settings.ad_bind_password
        and settings.ad_base_dn
    )
    zimbra_auth = settings.zimbra_ssh_auth
    zimbra_secret_present = bool(
        settings.zimbra_ssh_password
        or settings.zimbra_ssh_password_file
        or settings.zimbra_ssh_private_key
    )
    zimbra_configured = bool(
        settings.zimbra_ssh_host
        and settings.zimbra_ssh_user
        and zimbra_secret_present
        and settings.zimbra_backend != "disabled"
    )
    smtp_configured = bool(settings.smtp_host)
    onec_configured = bool(
        settings.onec_imap_host
        and settings.onec_imap_username
        and settings.onec_imap_password
        and settings.onec_attachment_filename
    )

    if settings.smtp_ssl:
        smtp_mode = "SSL/TLS"
    elif settings.smtp_starttls:
        smtp_mode = "STARTTLS"
    else:
        smtp_mode = "Без шифрования"

    return {
        "ad": {
            "configured": ad_configured,
            "badge": "Используется" if ad_configured else "Не настроено",
            "server": settings.ad_server or "–",
            "port": settings.ad_port,
            "ssl": _yes_no(settings.ad_use_ssl),
            "verify_tls": _yes_no(settings.ad_verify_tls),
            "base_dn": settings.ad_base_dn or "–",
            "users_ou": settings.ad_users_ou or "–",
            "bind_dn": settings.ad_bind_dn or "–",
            "password": _set_not_set(settings.ad_bind_password),
            "ca_file": _file_state(settings.ad_ca_cert_file),
            "check_enabled": _yes_no(settings.ad_check_enabled),
        },
        "zimbra": {
            "configured": zimbra_configured,
            "badge": "Используется" if zimbra_configured else "Не настроено",
            "server": settings.zimbra_ssh_host or "–",
            "port": settings.zimbra_ssh_port,
            "user": settings.zimbra_ssh_user or "–",
            "auth": zimbra_auth,
            "password": _set_not_set(
                settings.zimbra_ssh_password
                or settings.zimbra_ssh_password_file
            ),
            "private_key": _file_state(settings.zimbra_ssh_private_key)
            if zimbra_auth in {"key", "auto"}
            else "не используется",
            "known_hosts": _file_state(settings.zimbra_ssh_known_hosts),
            "backend": settings.zimbra_backend,
            "domains": ", ".join(settings.zimbra_domains) or "–",
            "check_enabled": _yes_no(settings.zimbra_check_enabled),
        },
        "smtp": {
            "configured": smtp_configured,
            "badge": "Используется" if smtp_configured else "Не настроено",
            "server": settings.smtp_host or "–",
            "port": settings.smtp_port,
            "mode": smtp_mode,
            "username": settings.smtp_username or "–",
            "password": _set_not_set(settings.smtp_password),
            "timeout": f"{settings.smtp_timeout_seconds} сек.",
            "retries": settings.smtp_retry_attempts,
        },
        "onec": {
            "configured": onec_configured,
            "badge": "Настроено" if onec_configured else "Не настроено",
            "server": settings.onec_imap_host or "–",
            "port": settings.onec_imap_port,
            "ssl": _yes_no(settings.onec_imap_ssl),
            "username": settings.onec_imap_username or "–",
            "password": _set_not_set(settings.onec_imap_password),
            "folder": settings.onec_imap_folder or "INBOX",
            "sender": settings.onec_imap_from_contains or "без фильтра",
            "lookback": f"{settings.onec_imap_lookback_days} дн.",
            "filename": settings.onec_attachment_filename or "–",
            "data_dir": settings.onec_data_dir,
            "hash_secret": (
                "ONEC_WORKER_HASH_SECRET"
                if settings.onec_worker_hash_secret.strip()
                else "APP_SECRET_KEY (временно)"
            ),
            "source_id": settings.onec_source_id,
            "source_name": settings.onec_source_name,
        },
    }


@router.get("/settings")
def settings_overview(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    current = require_admin(request)
    onec_service = OneCImportService(settings, db)
    registry = HRRegistryService(settings, db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": current,
            "csrf": get_or_create_csrf(request),
            "integrations": _integration_overview(settings),
            "onec_last_report": onec_service.load_last_report(),
            "onec_registry_summary": registry.summary(),
        },
    )


@router.post("/settings/test-integration/{integration}")
def test_integration(
    integration: str,
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)

    try:
        if integration == "ad":
            message = ActiveDirectoryService(settings).test_connection()
        elif integration == "zimbra":
            message = ZimbraService(settings).test_connection()
        elif integration == "smtp":
            message = CredentialMailer(settings).test_connection()
        elif integration == "onec":
            message = OneCImportService(settings).test_connection()
        else:
            return JSONResponse(
                {"ok": False, "error": "Неизвестная интеграция"},
                status_code=404,
            )

        return {"ok": True, "message": message}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )



@router.post("/settings/onec/find-latest")
def onec_find_latest(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    require_admin(request)

    try:
        result = OneCImportService(settings).find_latest()
        return {"ok": True, "mail": result}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/onec/analyze-latest")
def onec_analyze_latest(
    request: Request,
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    validate_csrf(request, csrf)
    require_admin(request)

    try:
        report = OneCImportService(settings, db).analyze_latest()
        return {"ok": True, "report": report}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=503,
        )


@router.post("/settings/onec/reconcile")
def onec_reconcile(request: Request, csrf: str = Form(...), settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    validate_csrf(request, csrf); require_admin(request)
    try:
        return {"ok": True, "summary": HRRegistryService(settings, db).reconcile_current()}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
