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
from app.services.zimbra import BackgroundLoginCheckCancelled, ZimbraService

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


def _yes_no(value: bool) -> str:
    return "Да" if value else "Нет"


def _provisioning_journal_item(operation: ProvisioningOperation) -> dict[str, object]:
    full_name = " ".join(
        part
        for part in [
            operation.last_name,
            operation.first_name,
            operation.middle_name,
        ]
        if part
    )
    status_labels = {
        "draft": "Черновик",
        "running": "Выполняется",
        "partial": "Частично выполнено",
        "success": "Успешно",
        "failed": "Ошибка",
    }
    status_key = operation.status.value

    return {
        "kind": "provision",
        "record_id": operation.id,
        "created_at": operation.created_at,
        "action": "Создание учетных записей",
        "subject": full_name,
        "login": operation.login,
        "corporate_email": operation.corporate_email,
        "personal_email": operation.personal_email,
        "mail_domain": operation.mail_domain,
        "operator": operation.operator_username,
        "status_key": status_key,
        "status_label": status_labels.get(status_key, status_key),
        "details": [
            ("ФИО", full_name),
            ("Логин AD", operation.login),
            ("Корпоративная почта", operation.corporate_email),
            ("Личный адрес", operation.personal_email),
            ("Почтовый домен", operation.mail_domain),
            ("Учетная запись AD создана", _yes_no(operation.ad_created)),
            ("Учетная запись AD включена", _yes_no(operation.ad_enabled)),
            ("Ящик Zimbra создан", _yes_no(operation.zimbra_created)),
            (
                "Реквизиты почты отправлены на личный адрес",
                (
                    _yes_no(operation.personal_mail_sent)
                    if operation.personal_email
                    else "Не требуется"
                ),
            ),
            (
                "Реквизиты AD отправлены на корпоративную почту",
                _yes_no(operation.corporate_mail_sent),
            ),
        ],
        "error_message": operation.error_message,
        "completed_at": operation.completed_at,
    }


def _dismissal_journal_item(schedule: DismissalSchedule) -> dict[str, object]:
    if schedule.ad_expiration_set and schedule.zimbra_note_set:
        status_key = "success"
        status_label = "Успешно"
    elif schedule.ad_expiration_set or schedule.zimbra_note_set:
        status_key = "partial"
        status_label = "Частично выполнено"
    elif schedule.error_message:
        status_key = "failed"
        status_label = "Ошибка"
    else:
        status_key = "running"
        status_label = "Выполняется"

    return {
        "kind": "dismissal",
        "record_id": schedule.id,
        "created_at": schedule.created_at,
        "action": "Пометка на увольнение",
        "subject": schedule.corporate_email or schedule.login,
        "login": schedule.login,
        "corporate_email": schedule.corporate_email,
        "personal_email": "",
        "mail_domain": "",
        "operator": schedule.operator_username,
        "status_key": status_key,
        "status_label": status_label,
        "details": [
            ("Логин AD", schedule.login),
            ("Корпоративная почта", schedule.corporate_email),
            ("Дата увольнения", schedule.dismissal_date.strftime("%d.%m.%Y")),
            (
                "Срок действия учетной записи AD установлен",
                _yes_no(schedule.ad_expiration_set),
            ),
            (
                "Дата записана в zimbraNotes",
                _yes_no(schedule.zimbra_note_set),
            ),
        ],
        "error_message": schedule.error_message,
        "completed_at": None,
    }


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    provisioning_operations = db.scalars(
        select(ProvisioningOperation)
        .order_by(desc(ProvisioningOperation.created_at))
        .limit(50)
    ).all()
    dismissal_operations = db.scalars(
        select(DismissalSchedule)
        .order_by(desc(DismissalSchedule.created_at))
        .limit(50)
    ).all()

    journal_items = [
        *(_provisioning_journal_item(item) for item in provisioning_operations),
        *(_dismissal_journal_item(item) for item in dismissal_operations),
    ]
    journal_items.sort(key=lambda item: item["created_at"], reverse=True)
    journal_items = journal_items[:50]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            journal_items=journal_items,
            dry_run=settings.dry_run,
        ),
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
    confirm_no_personal_email: str = Form("false"),
    csrf: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    try:
        parsed = parse_two_line_input(raw_input)
        no_email_confirmed = (
            confirm_no_personal_email.strip().lower() == "true"
        )
        if not parsed.personal_email and not no_email_confirmed:
            raise ValueError(
                "ФИО введено без личного email. "
                "Подтвердите продолжение без отправки реквизитов "
                "на личный адрес."
            )

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
                no_email_confirmed=no_email_confirmed,
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
    fresh: bool = False,
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
                ZimbraService(settings).login_exists_any_domain(
                    normalized_login,
                    force_refresh=fresh,
                )
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
    force_refresh: bool = Form(False),
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
        items = ProvisioningService(settings).check_logins(
            logins,
            force_refresh=force_refresh,
            background=True,
        )
        return {
            "ok": True,
            "items": items,
            "checked": len(items),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    except BackgroundLoginCheckCancelled:
        return JSONResponse(
            {
                "ok": False,
                "cancelled": True,
                "error": "Фоновая проверка альтернатив отменена",
            },
            status_code=409,
        )
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
    personal_email: str = Form(""),
    confirm_no_personal_email: str = Form("false"),
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

        last_name, first_name, middle_name = validate_person_name(
            last_name,
            first_name,
            middle_name,
        )
        no_email_confirmed = (
            confirm_no_personal_email.strip().lower() == "true"
        )
        if personal_email:
            try:
                personal_email = validate_email(
                    personal_email,
                    check_deliverability=False,
                ).normalized
            except EmailNotValidError as exc:
                raise ValueError(
                    f"Некорректный личный email: {exc}"
                ) from exc
        elif not no_email_confirmed:
            raise ValueError(
                "Личный email не указан. Подтвердите создание "
                "учетных записей без отправки реквизитов "
                "на личный адрес."
            )

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
                no_email_confirmed=(
                    confirm_no_personal_email.strip().lower() == "true"
                ),
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
