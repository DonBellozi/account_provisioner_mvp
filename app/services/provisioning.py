from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, OperationStatus, ProvisioningOperation
from app.services.ad import ActiveDirectoryService
from app.services.mailer import CredentialMailer
from app.services.names import transliterate
from app.services.passwords import generate_ad_password, generate_mail_password
from app.services.zimbra import ZimbraService


@dataclass(frozen=True)
class ProvisioningInput:
    last_name: str
    first_name: str
    middle_name: str
    personal_email: str
    login: str
    mail_domain: str


@dataclass(frozen=True)
class ProvisioningCredentials:
    full_name: str
    corporate_email: str
    mail_password: str
    ad_login: str
    ad_password: str
    aliases: tuple[str, ...]
    operation_id: int
    dry_run: bool
    status: str
    ad_created: bool
    ad_enabled: bool
    zimbra_created: bool
    personal_mail_sent: bool
    corporate_mail_sent: bool
    warnings: tuple[str, ...]


class ProvisioningService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ad = ActiveDirectoryService(settings)
        self.zimbra = ZimbraService(settings)
        self.mailer = CredentialMailer(settings)

    def check_login(self, login: str) -> dict[str, bool]:
        # AD проверяется первым. Если логин уже занят в AD, дорогостоящая
        # SSH-проверка Zimbra для этого кандидата не нужна: кандидат в любом
        # случае не может быть выбран.
        ad_exists = self.ad.login_exists(login)
        if ad_exists:
            return {
                "ad": True,
                "zimbra": False,
            }

        return {
            "ad": False,
            "zimbra": self.zimbra.login_exists_any_domain(login),
        }

    def provision(self, db: Session, operator: str, data: ProvisioningInput) -> ProvisioningCredentials:
        availability = self.check_login(data.login)
        if availability["ad"] or availability["zimbra"]:
            occupied = ", ".join(name for name, value in availability.items() if value)
            raise RuntimeError(f"Логин уже занят: {occupied}")

        full_name = " ".join(part for part in [data.last_name, data.first_name, data.middle_name] if part)
        mail_password = generate_mail_password(
            self.settings.mail_password_length,
            self.settings.mail_password_specials,
        )
        ad_password_candidates = [
            generate_ad_password(
                transliterate(data.first_name),
                transliterate(data.last_name),
                self.settings.ad_password_min_length,
                self.settings.ad_password_max_length,
                self.settings.ad_password_specials,
            )
            for _ in range(10)
        ]
        ad_password = ad_password_candidates[0]

        primary_domain = (
            self.settings.zimbra_primary_domain
            if self.settings.zimbra_domain_mode == "primary_alias"
            else data.mail_domain
        )
        corporate_email = f"{data.login}@{primary_domain}"
        operation = ProvisioningOperation(
            operator_username=operator,
            last_name=data.last_name,
            first_name=data.first_name,
            middle_name=data.middle_name,
            personal_email=data.personal_email,
            login=data.login,
            corporate_email=corporate_email,
            mail_domain=primary_domain,
            status=OperationStatus.RUNNING,
        )
        db.add(operation)
        db.commit()
        db.refresh(operation)

        ad_dn = ""
        aliases: tuple[str, ...] = ()
        warnings: list[str] = []

        # 1. AD создается отключенным. Если Zimbra не создастся, вход в AD
        # не будет доступен.
        try:
            ad_result = self.ad.create_disabled_user(
                login=data.login,
                password_candidates=ad_password_candidates,
                last_name=data.last_name,
                first_name=data.first_name,
                middle_name=data.middle_name,
                corporate_email=corporate_email,
            )
            ad_dn = ad_result.dn
            ad_password = ad_result.accepted_password
            operation.ad_created = True
            db.commit()
        except Exception as exc:
            warnings.append(f"Учетная запись AD не создана: {exc}")

        # 2. Почтовый ящик создается только при успешном создании заготовки AD.
        if operation.ad_created:
            try:
                zimbra_result = self.zimbra.create_account(
                    login=data.login,
                    domain=data.mail_domain,
                    password=mail_password,
                    last_name=data.last_name,
                    first_name=data.first_name,
                    middle_name=data.middle_name,
                )
                corporate_email = zimbra_result.primary_email
                aliases = zimbra_result.aliases
                operation.corporate_email = corporate_email
                operation.zimbra_created = True
                db.commit()
            except Exception as exc:
                warnings.append(f"Почтовый ящик Zimbra не создан: {exc}")
                if self.settings.rollback_ad_on_zimbra_failure and ad_dn:
                    try:
                        self.ad.delete_user(ad_dn)
                        operation.ad_created = False
                        warnings.append("Отключенная заготовка AD удалена согласно настройке rollback.")
                    except Exception as rollback_exc:
                        warnings.append(f"Не удалось удалить заготовку AD: {rollback_exc}")
                    db.commit()

        # 3. AD включается только после успешного создания Zimbra.
        if operation.ad_created and operation.zimbra_created:
            try:
                self.ad.enable_user(ad_dn)
                operation.ad_enabled = True
                db.commit()
            except Exception as exc:
                warnings.append(f"Учетная запись AD создана, но осталась отключенной: {exc}")

        # 4. Отправка писем не откатывает созданные учетные записи.
        if operation.zimbra_created:
            try:
                self.mailer.send_mail_credentials(
                    personal_email=data.personal_email,
                    full_name=full_name,
                    corporate_email=corporate_email,
                    mail_password=mail_password,
                )
                operation.personal_mail_sent = True
                db.commit()
            except Exception as exc:
                warnings.append(f"Реквизиты почты не отправлены на личный адрес: {exc}")

        if operation.ad_enabled and operation.zimbra_created:
            try:
                self.mailer.send_ad_credentials(
                    corporate_email=corporate_email,
                    full_name=full_name,
                    ad_login=data.login,
                    ad_password=ad_password,
                )
                operation.corporate_mail_sent = True
                db.commit()
            except Exception as exc:
                warnings.append(f"Реквизиты AD не отправлены на корпоративную почту: {exc}")

        complete = all(
            [
                operation.ad_created,
                operation.ad_enabled,
                operation.zimbra_created,
                operation.personal_mail_sent,
                operation.corporate_mail_sent,
            ]
        )
        nothing_created = not operation.ad_created and not operation.zimbra_created
        operation.status = (
            OperationStatus.SUCCESS
            if complete
            else OperationStatus.FAILED
            if nothing_created
            else OperationStatus.PARTIAL
        )
        operation.error_message = "\n".join(warnings)[:4000]
        operation.completed_at = datetime.now(timezone.utc)
        db.add(
            AuditLog(
                actor=operator,
                action="provision",
                target=corporate_email,
                result=operation.status.value,
                details="; ".join(warnings)[:1000],
            )
        )
        db.commit()

        return ProvisioningCredentials(
            full_name=full_name,
            corporate_email=corporate_email,
            mail_password=mail_password,
            ad_login=data.login,
            ad_password=ad_password,
            aliases=aliases,
            operation_id=operation.id,
            dry_run=self.settings.dry_run,
            status=operation.status.value,
            ad_created=operation.ad_created,
            ad_enabled=operation.ad_enabled,
            zimbra_created=operation.zimbra_created,
            personal_mail_sent=operation.personal_mail_sent,
            corporate_mail_sent=operation.corporate_mail_sent,
            warnings=tuple(warnings),
        )
