from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
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


def _context(request: Request, **kwargs):
    user = get_current_user(request)
    return {"user": user, "csrf": get_or_create_csrf(request), **kwargs}


def _domains(settings: Settings) -> list[str]:
    return settings.zimbra_domains or (
        [settings.zimbra_primary_domain] if settings.zimbra_primary_domain else []
    )


def _check_candidates(service: ProvisioningService, last_name: str, first_name: str, middle_name: str):
    """Check candidates in sequence and stop at the first free login."""
    checked: list[dict[str, object]] = []
    selected_login = ""
    for login in build_login_candidates(last_name, first_name, middle_name):
        occupied = service.check_login(login)
        free = not any(occupied.values())
        checked.append({"login": login, "occupied": occupied, "free": free})
        if free:
            selected_login = login
            break
    return checked, selected_login


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
        service = ProvisioningService(settings)
        candidates, selected_login = _check_candidates(
            service,
            parsed.last_name,
            parsed.first_name,
            parsed.middle_name,
        )
        if not selected_login:
            raise RuntimeError(
                "Не удалось подобрать свободный логин по стандартным правилам. "
                "Введите собственный вариант и выполните регистрацию."
            )
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
                domain_mode=settings.zimbra_domain_mode,
            ),
            status_code=400,
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
                candidates=[],
                error=str(exc),
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
