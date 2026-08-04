from __future__ import annotations

import ssl
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ldap3 import ALL, NTLM, SIMPLE, Connection, MODIFY_ADD, MODIFY_REPLACE, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.config import Settings


@dataclass(frozen=True)
class ADCreateResult:
    dn: str
    login: str
    upn: str
    accepted_password: str


class ActiveDirectoryService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _server(self) -> Server:
        tls = None
        if self.settings.ad_use_ssl:
            validate = ssl.CERT_REQUIRED if self.settings.ad_verify_tls else ssl.CERT_NONE
            tls = Tls(validate=validate, ca_certs_file=self.settings.ad_ca_cert_file or None)
        return Server(
            self.settings.ad_server,
            port=self.settings.ad_port,
            use_ssl=self.settings.ad_use_ssl,
            tls=tls,
            get_info=ALL,
            connect_timeout=10,
        )

    def _service_connection(self) -> Connection:
        if not self.settings.ad_server or not self.settings.ad_bind_dn:
            raise RuntimeError("Не заполнены настройки подключения к AD")
        conn = Connection(
            self._server(),
            user=self.settings.ad_bind_dn,
            password=self.settings.ad_bind_password,
            authentication=SIMPLE,
            auto_bind=True,
            receive_timeout=15,
        )
        return conn

    def authenticate_operator(self, username: str, password: str) -> bool:
        if not self.settings.ad_login_enabled:
            return False
        bind_user = f"{self.settings.ad_domain}\\{username}" if self.settings.ad_domain else username
        try:
            conn = Connection(
                self._server(),
                user=bind_user,
                password=password,
                authentication=NTLM if self.settings.ad_domain else SIMPLE,
                auto_bind=True,
                receive_timeout=15,
            )
            try:
                if self.settings.ad_allowed_group_dn:
                    safe_user = escape_filter_chars(username)
                    conn.search(
                        self.settings.ad_base_dn,
                        f"(&(objectClass=user)(sAMAccountName={safe_user}))",
                        attributes=["memberOf"],
                    )
                    if not conn.entries:
                        return False
                    groups = {str(item).lower() for item in conn.entries[0].memberOf.values}
                    return self.settings.ad_allowed_group_dn.lower() in groups
                return True
            finally:
                conn.unbind()
        except LDAPException:
            return False

    def logins_exist(self, logins: list[str]) -> set[str]:
        if not self.settings.ad_check_enabled:
            return set()

        normalized = list(dict.fromkeys(login.strip().lower() for login in logins if login.strip()))
        if not normalized:
            return set()

        alternatives = "".join(
            f"(sAMAccountName={escape_filter_chars(login)})"
            for login in normalized
        )
        search_filter = f"(&(objectClass=user)(|{alternatives}))"

        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                search_filter,
                attributes=["sAMAccountName"],
                size_limit=len(normalized),
            )
            return {
                str(entry.sAMAccountName.value).lower()
                for entry in conn.entries
                if getattr(entry, "sAMAccountName", None)
                and entry.sAMAccountName.value
            }

    def login_exists(self, login: str) -> bool:
        normalized = login.strip().lower()
        return normalized in self.logins_exist([normalized])


    def create_disabled_user(
        self,
        login: str,
        password_candidates: list[str],
        last_name: str,
        first_name: str,
        middle_name: str,
        corporate_email: str,
    ) -> ADCreateResult:
        upn = f"{login}@{self.settings.ad_upn_suffix}"
        display_name = " ".join(part for part in [last_name, first_name, middle_name] if part)
        # CN строится из уникального логина, иначе полный тезка в той же OU
        # не сможет быть создан. displayName при этом остается обычным ФИО.
        cn = login.replace(",", "\\,")
        dn = f"CN={cn},{self.settings.ad_users_ou}"

        if not password_candidates:
            raise ValueError("Не переданы варианты пароля AD")
        if self.settings.dry_run:
            return ADCreateResult(dn=dn, login=login, upn=upn, accepted_password=password_candidates[0])

        attributes = {
            "objectClass": ["top", "person", "organizationalPerson", "user"],
            "cn": login,
            "displayName": display_name,
            "givenName": first_name,
            "sn": last_name,
            "sAMAccountName": login,
            "userPrincipalName": upn,
            "mail": corporate_email,
            "userAccountControl": 514,  # normal account + disabled
        }
        with self._service_connection() as conn:
            if not conn.add(dn, attributes=attributes):
                raise RuntimeError(f"AD не создал пользователя: {conn.result.get('message') or conn.result}")
            accepted_password = ""
            last_password_error = ""
            for candidate in password_candidates:
                if conn.extend.microsoft.modify_password(dn, candidate):
                    accepted_password = candidate
                    break
                last_password_error = str(conn.result.get("message") or conn.result)
            if not accepted_password:
                # Не оставляем объект без рабочего пароля.
                conn.delete(dn)
                raise RuntimeError(f"AD не принял ни один сгенерированный пароль: {last_password_error}")
            if self.settings.ad_force_change_at_first_logon:
                if not conn.modify(dn, {"pwdLastSet": [(MODIFY_REPLACE, [0])]}):
                    raise RuntimeError(f"AD не установил смену пароля при первом входе: {conn.result}")
            for group_dn in self.settings.ad_default_group_dns:
                if not conn.modify(group_dn, {"member": [(MODIFY_ADD, [dn])]}):
                    raise RuntimeError(f"Не удалось добавить пользователя в группу {group_dn}: {conn.result}")
        return ADCreateResult(dn=dn, login=login, upn=upn, accepted_password=accepted_password)

    def enable_user(self, dn: str) -> None:
        if self.settings.dry_run:
            return
        with self._service_connection() as conn:
            if not conn.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [512])]}):
                raise RuntimeError(f"AD не включил пользователя: {conn.result.get('message') or conn.result}")

    def delete_user(self, dn: str) -> None:
        if self.settings.dry_run:
            return
        with self._service_connection() as conn:
            if not conn.delete(dn):
                raise RuntimeError(f"Не удалось удалить заготовку AD: {conn.result}")

    def set_account_expiration(self, login: str, dismissal_date: date) -> None:
        """Expire at 00:00 on the day after the employee's last working date."""
        if self.settings.dry_run:
            return
        safe_login = escape_filter_chars(login)
        with self._service_connection() as conn:
            conn.search(
                self.settings.ad_base_dn,
                f"(&(objectClass=user)(sAMAccountName={safe_login}))",
                attributes=["distinguishedName"],
                size_limit=1,
            )
            if not conn.entries:
                raise RuntimeError("Учетная запись AD не найдена")
            dn = str(conn.entries[0].entry_dn)
            account_expires = self._date_to_filetime(dismissal_date)
            if not conn.modify(dn, {"accountExpires": [(MODIFY_REPLACE, [account_expires])]}):
                raise RuntimeError(f"AD не установил срок действия: {conn.result}")

    def _date_to_filetime(self, last_working_date: date) -> int:
        local_tz = ZoneInfo(self.settings.app_timezone)
        local_expiration = datetime.combine(last_working_date + timedelta(days=1), time.min, tzinfo=local_tz)
        utc_expiration = local_expiration.astimezone(timezone.utc)
        windows_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        return int((utc_expiration - windows_epoch).total_seconds() * 10_000_000)
