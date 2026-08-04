from __future__ import annotations

import json
import re
import time
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import AuditLog, DismissalSchedule, ProvisioningOperation
from app.security import get_current_user, get_or_create_csrf, validate_csrf
from app.services.ad import ActiveDirectoryService
from app.services.names import build_login_candidates, parse_two_line_input, validate_person_name
from app.services.provisioning import ProvisioningInput, ProvisioningService
from app.services.zimbra import ZimbraService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
LOGIN_RE = re.compile(r"^[a-z][a-z0-9.-]{0,19}$")
MAX_BACKGROUND_CANDIDATES = 12


def _context(request: Request, **kwargs):
    user = get_current_user(request)
    return {"user": user, "csrf": get_or_create_csrf(request), **kwargs}


def _domains(settings: Settings) -> list[str]:
    return settings.zimbra_domains or (
        [settings.zimbra_primary_domain] if settings.zimbra_primary_domain else []
    )


def _login_candidates(last_name: str, first_name: str, middle_name: str) -> list[str]:
    try:
        return build_login_candidates(last_name, first_name, middle_name)[:MAX_BACKGROUND_CANDIDATES]
    except Exception:
        return []


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    operations = db.scalars(
        select(ProvisioningOperation).order_by(desc(ProvisioningOperation.created_at)).limit(20)
    ).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(request, operations=operations, dry_run=settings.dry_run),
    )


@router.get("/employees/new")
def new_employee(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(
        request,
        "employee_form.html",
        _context(
            request,
            domains=_domains(settings),
            parsed=None,
            candidates=[],
            error="",
            raw_input="",
            domain_mode=settings.zimbra_domain_mode,
        ),
    )


@router.post("/employees/parse")
def parse_employee(
    request: Request,
    raw_input: str = Form(...),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    try:
        parsed = parse_two_line_input(raw_input)
        candidates = build_login_candidates(
            parsed.last_name,
            parsed.first_name,
            parsed.middle_name,
        )[:MAX_BACKGROUND_CANDIDATES]
        if not candidates:
            raise RuntimeError("Не удалось сформировать логин из ФИО")

        # Внешние системы здесь больше не проверяются. Следующий экран
        # открывается сразу, а AD и Zimbra проверяются отдельными запросами.
        selected_login = candidates[0]

        return templates.TemplateResponse(
            request,
            "employee_form.html",
            _context(
                request,
                domains=_domains(settings),
                parsed=parsed,
                candidates=candidates,
                selected_login=selected_login,
                error="",
                raw_input=raw_input,
                domain_mode=settings.zimbra_domain_mode,
            ),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "employee_form.html",
            _context(
                request,
                domains=_domains(settings),
                parsed=None,
                candidates=[],
                error=str(exc),
                raw_input=raw_input,
                domain_mode=settings.zimbra_domain_mode,
            ),
            status_code=400,
        )


@router.get("/employees/check-login/{source}")
def check_login_source(
    source: str,
    request: Request,
    login: str,
    settings: Settings = Depends(get_settings),
):
    # Запрос доступен только авторизованному оператору.
    get_current_user(request)

    normalized_login = login.strip().lower()
    if not LOGIN_RE.fullmatch(normalized_login):
        return JSONResponse(
            {
                "ok": False,
                "source": source,
                "error": "Некорректный формат логина",
            },
            status_code=400,
        )

    started = time.perf_counter()
    try:
        if source == "ad":
            enabled = settings.ad_check_enabled
            occupied = (
                ActiveDirectoryService(settings).login_exists(normalized_login)
                if enabled
                else False
            )
            label = "Active Directory"
        elif source == "zimbra":
            enabled = settings.zimbra_check_enabled
            occupied = (
                ZimbraService(settings).login_exists_any_domain(normalized_login)
                if enabled
                else False
            )
            label = "Zimbra"
        else:
            return JSONResponse(
                {
                    "ok": False,
                    "source": source,
                    "error": "Неизвестный источник проверки",
                },
                status_code=404,
            )

        return {
            "ok": True,
            "source": source,
            "label": label,
            "login": normalized_login,
            "enabled": enabled,
            "occupied": occupied,
            "free": not occupied,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "source": source,
                "login": normalized_login,
                "error": str(exc),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            },
            status_code=503,
        )


@router.post("/employees/check-candidates")
def check_login_candidates(
    request: Request,
    logins_json: str = Form(...),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    get_current_user(request)

    try:
        raw_logins = json.loads(logins_json)
        if not isinstance(raw_logins, list):
            raise ValueError("Список кандидатов имеет неверный формат")

        logins: list[str] = []
        for value in raw_logins[:MAX_BACKGROUND_CANDIDATES]:
            login = str(value).strip().lower()
            if not LOGIN_RE.fullmatch(login):
                continue
            if login not in logins:
                logins.append(login)

        if not logins:
            raise ValueError("Не переданы корректные варианты логина")

        started = time.perf_counter()
        items = ProvisioningService(settings).check_logins(logins)
        return {
            "ok": True,
            "items": items,
            "checked": len(items),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=503,
        )


@router.post("/employees/provision")
def provision_employee(
    request: Request,
    last_name: str = Form(...),
    first_name: str = Form(...),
    middle_name: str = Form(""),
    personal_email: str = Form(...),
    login: str = Form(...),
    mail_domain: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    login = login.strip().lower()
    personal_email = personal_email.strip()

    try:
        from email_validator import EmailNotValidError, validate_email

        if not LOGIN_RE.fullmatch(login):
            raise ValueError("Логин должен начинаться с латинской буквы и содержать не более 20 символов")

        last_name, first_name, middle_name = validate_person_name(last_name, first_name, middle_name)
        try:
            personal_email = validate_email(personal_email, check_deliverability=False).normalized
        except EmailNotValidError as exc:
            raise ValueError(f"Некорректный личный email: {exc}") from exc
        if settings.zimbra_domains and mail_domain not in settings.zimbra_domains:
            raise ValueError("Выбран неизвестный почтовый домен")

        data = ProvisioningInput(
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            personal_email=personal_email,
            login=login,
            mail_domain=mail_domain,
        )
        credentials = ProvisioningService(settings).provision(db, user.username, data)
        response = templates.TemplateResponse(
            request,
            "result.html",
            _context(request, credentials=credentials, error=""),
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception as exc:
        parsed = type(
            "Parsed",
            (),
            {
                "last_name": last_name.strip(),
                "first_name": first_name.strip(),
                "middle_name": middle_name.strip(),
                "personal_email": personal_email,
            },
        )()
        return templates.TemplateResponse(
            request,
            "employee_form.html",
            _context(
                request,
                domains=_domains(settings),
                parsed=parsed,
                candidates=_login_candidates(last_name, first_name, middle_name),
                error=str(exc),
                raw_input="",
                domain_mode=settings.zimbra_domain_mode,
                selected_login=login,
                selected_domain=mail_domain,
            ),
            status_code=400,
        )


@router.get("/dismissals/new")
def dismissal_form(request: Request):
    return templates.TemplateResponse(
        request,
        "dismissal_form.html",
        _context(request, error="", success=""),
    )


@router.post("/dismissals")
def schedule_dismissal(
    request: Request,
    login: str = Form(...),
    corporate_email: str = Form(...),
    dismissal_date: date = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    user = get_current_user(request)
    schedule = DismissalSchedule(
        login=login.strip().lower(),
        corporate_email=corporate_email.strip().lower(),
        dismissal_date=dismissal_date,
        operator_username=user.username,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    try:
        ActiveDirectoryService(settings).set_account_expiration(schedule.login, schedule.dismissal_date)
        schedule.ad_expiration_set = True
        db.commit()
        ZimbraService(settings).set_dismissal_note(schedule.corporate_email, schedule.dismissal_date)
        schedule.zimbra_note_set = True
        db.add(AuditLog(actor=user.username, action="schedule_dismissal", target=schedule.corporate_email))
        db.commit()
        return templates.TemplateResponse(
            request,
            "dismissal_form.html",
            _context(request, error="", success="Срок действия AD и дата в zimbraNotes установлены"),
        )
    except Exception as exc:
        schedule.error_message = str(exc)[:4000]
        db.add(
            AuditLog(
                actor=user.username,
                action="schedule_dismissal",
                target=schedule.corporate_email,
                result="partial",
                details=str(exc)[:1000],
            )
        )
        db.commit()
        return templates.TemplateResponse(
            request,
            "dismissal_form.html",
            _context(request, error=str(exc), success=""),
            status_code=400,
        )
