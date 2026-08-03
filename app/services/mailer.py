from __future__ import annotations

import smtplib
import ssl
import time
from email.message import EmailMessage

from app.config import Settings


class CredentialMailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _send_once(self, message: EmailMessage) -> None:
        if self.settings.smtp_ssl:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
            )
        try:
            client.ehlo()
            if self.settings.smtp_starttls and not self.settings.smtp_ssl:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self.settings.smtp_username:
                client.login(self.settings.smtp_username, self.settings.smtp_password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except smtplib.SMTPException:
                client.close()

    def _send(self, recipient: str, subject: str, body: str) -> None:
        if self.settings.dry_run:
            return
        if not self.settings.smtp_host or not self.settings.smtp_from:
            raise RuntimeError("Не заполнены настройки SMTP")

        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)

        last_error: Exception | None = None
        attempts = max(1, self.settings.smtp_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                self._send_once(message)
                return
            except (OSError, smtplib.SMTPException) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(max(0.0, self.settings.smtp_retry_delay_seconds))
        raise RuntimeError(f"SMTP не отправил письмо после {attempts} попыток: {last_error}")

    def send_mail_credentials(
        self,
        personal_email: str,
        full_name: str,
        corporate_email: str,
        mail_password: str,
    ) -> None:
        body = f"""Здравствуйте, {full_name}!

Для Вас создана корпоративная электронная почта.

Логин: {corporate_email}
Пароль: {mail_password}

После входа в корпоративную почту Вы получите отдельное письмо с реквизитами учетной записи для входа в компьютер.
"""
        self._send(personal_email, "Реквизиты корпоративной электронной почты", body)

    def send_ad_credentials(
        self,
        corporate_email: str,
        full_name: str,
        ad_login: str,
        ad_password: str,
    ) -> None:
        body = f"""Здравствуйте, {full_name}!

Для Вас создана доменная учетная запись.

Логин: {ad_login}
Пароль: {ad_password}

При первом входе система может потребовать сменить временный пароль.
"""
        self._send(corporate_email, "Реквизиты доменной учетной записи", body)
