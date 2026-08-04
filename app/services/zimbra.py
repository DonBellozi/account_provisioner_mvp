from __future__ import annotations

import shlex
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import paramiko

from app.config import Settings


@dataclass(frozen=True)
class ZimbraCreateResult:
    primary_email: str
    aliases: tuple[str, ...]


class BackgroundLoginCheckCancelled(RuntimeError):
    """Фоновая проверка альтернатив остановлена перед созданием учетных записей."""


class ZimbraService:
    # Результаты коротко кэшируются, чтобы фоновая проверка списка,
    # проверка выбранного логина и повторная проверка не запускали несколько
    # одинаковых JVM-процессов zmprov подряд.
    _CACHE_TTL_SECONDS = 45.0
    _cache_lock = threading.Lock()
    _query_lock = threading.Lock()
    _background_state_lock = threading.Lock()
    _background_cancel_event: threading.Event | None = None
    _login_cache: dict[
        tuple[str, int, tuple[str, ...], str],
        tuple[float, bool],
    ] = {}

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def begin_background_check(cls) -> threading.Event:
        """Начать новую фоновую проверку, отменив предыдущую."""
        with cls._background_state_lock:
            if cls._background_cancel_event is not None:
                cls._background_cancel_event.set()
            event = threading.Event()
            cls._background_cancel_event = event
            return event

    @classmethod
    def cancel_background_checks(cls) -> None:
        """Остановить текущую проверку альтернатив перед созданием учетных записей."""
        with cls._background_state_lock:
            if cls._background_cancel_event is not None:
                cls._background_cancel_event.set()

    @classmethod
    def finish_background_check(cls, event: threading.Event) -> None:
        with cls._background_state_lock:
            if cls._background_cancel_event is event:
                cls._background_cancel_event = None

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise BackgroundLoginCheckCancelled("Фоновая проверка альтернатив отменена")

    def _read_ssh_password(self) -> str:
        if self.settings.zimbra_ssh_password_file:
            password_file = Path(self.settings.zimbra_ssh_password_file)
            if not password_file.is_file():
                raise RuntimeError("Не найден файл с SSH-паролем Zimbra")
            password = password_file.read_text(encoding="utf-8").rstrip("\r\n")
        else:
            password = self.settings.zimbra_ssh_password

        if not password:
            raise RuntimeError(
                "Не задан SSH-пароль Zimbra: укажите ZIMBRA_SSH_PASSWORD "
                "или ZIMBRA_SSH_PASSWORD_FILE"
            )
        return password

    def _resolve_ssh_auth(self) -> str:
        auth = self.settings.zimbra_ssh_auth
        if auth != "auto":
            return auth

        private_key = Path(self.settings.zimbra_ssh_private_key)
        if self.settings.zimbra_ssh_private_key and private_key.is_file():
            return "key"
        return "password"

    def _client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        known_hosts = Path(self.settings.zimbra_ssh_known_hosts)
        if not known_hosts.exists():
            raise RuntimeError("Не найден файл known_hosts для Zimbra")
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        connect_kwargs: dict[str, object] = {
            "hostname": self.settings.zimbra_ssh_host,
            "port": self.settings.zimbra_ssh_port,
            "username": self.settings.zimbra_ssh_user,
            "look_for_keys": False,
            "allow_agent": False,
            "timeout": 10,
            "banner_timeout": 10,
            "auth_timeout": 10,
        }

        auth = self._resolve_ssh_auth()
        if auth == "key":
            private_key = Path(self.settings.zimbra_ssh_private_key)
            if not private_key.is_file():
                raise RuntimeError("Не найден закрытый SSH-ключ Zimbra")
            connect_kwargs["key_filename"] = str(private_key)
        elif auth == "password":
            connect_kwargs["password"] = self._read_ssh_password()
        else:
            raise RuntimeError(f"Неизвестный режим SSH-аутентификации Zimbra: {auth}")

        try:
            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(15)
        except Exception:
            client.close()
            raise
        return client

    def _zmprov_command(self) -> str:
        if self.settings.zimbra_ssh_user.strip().lower() == "zimbra":
            return "/opt/zimbra/bin/zmprov"
        return "sudo -n -u zimbra /opt/zimbra/bin/zmprov"

    def _execute_zmprov(
        self,
        client: paramiko.SSHClient,
        args: list[str],
        allow_not_found: bool = False,
    ) -> str:
        stdin, stdout, stderr = client.exec_command(self._zmprov_command(), timeout=30)
        # Изменяющие команды передаем через stdin, чтобы пароль создаваемого
        # ящика не попадал в аргументы процесса и не был виден в ps.
        # Paramiko необходимо передавать готовые UTF-8 bytes.
        # При передаче Python str кириллица в некоторых версиях превращается
        # в младшие байты Unicode: «Тестов» -> «"5AB>2».
        payload = (shlex.join(args) + "\n").encode("utf-8")
        stdin.write(payload)
        stdin.flush()
        stdin.channel.shutdown_write()

        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        combined = f"{err}\n{out}"
        not_found = "NO_SUCH_ACCOUNT" in combined or "account.NO_SUCH_ACCOUNT" in combined

        if not_found:
            if allow_not_found:
                return out
            raise RuntimeError(err or out or "account.NO_SUCH_ACCOUNT")

        if code != 0:
            raise RuntimeError(f"zmprov завершился с кодом {code}: {err or out}")
        return out

    def _execute_zmprov_lookup(
        self,
        client: paramiko.SSHClient,
        args: list[str],
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        self._raise_if_cancelled(cancel_event)
        command = f"{self._zmprov_command()} -l {shlex.join(args)}"
        stdin, stdout, stderr = client.exec_command(command, timeout=45)
        stdin.channel.shutdown_write()

        channel = stdout.channel
        deadline = time.monotonic() + 45.0
        while not channel.exit_status_ready():
            if cancel_event is not None and cancel_event.is_set():
                channel.close()
                raise BackgroundLoginCheckCancelled(
                    "Фоновая проверка альтернатив отменена"
                )
            if time.monotonic() >= deadline:
                channel.close()
                raise RuntimeError("Превышено время ожидания ответа zmprov")
            time.sleep(0.05)

        code = channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        combined = f"{err}\n{out}"

        if "NO_SUCH_ACCOUNT" in combined or "account.NO_SUCH_ACCOUNT" in combined:
            raise RuntimeError(err or out or "account.NO_SUCH_ACCOUNT")
        if code != 0:
            raise RuntimeError(f"zmprov завершился с кодом {code}: {err or out}")
        return out

    def _run_zmprov(
        self,
        args: list[str],
        allow_not_found: bool = False,
        mutating: bool = True,
    ) -> str:
        if self.settings.zimbra_backend == "disabled":
            raise RuntimeError("Zimbra backend отключен")
        if mutating and self.settings.dry_run:
            return "DRY-RUN"

        client = self._client()
        try:
            return self._execute_zmprov(client, args, allow_not_found=allow_not_found)
        finally:
            client.close()

    @staticmethod
    def _is_not_found_error(exc: RuntimeError) -> bool:
        message = str(exc)
        return "NO_SUCH_ACCOUNT" in message or "account.NO_SUCH_ACCOUNT" in message

    @staticmethod
    def _escape_ldap_filter_value(value: str) -> str:
        return (
            value.replace("\\", r"\5c")
            .replace("*", r"\2a")
            .replace("(", r"\28")
            .replace(")", r"\29")
            .replace("\x00", r"\00")
        )

    def _cache_key(self, login: str) -> tuple[str, int, tuple[str, ...], str]:
        domains = tuple(domain.strip().lower() for domain in self.settings.zimbra_domains)
        return (
            self.settings.zimbra_ssh_host.strip().lower(),
            self.settings.zimbra_ssh_port,
            domains,
            login.strip().lower(),
        )

    def _cache_get(self, login: str) -> bool | None:
        key = self._cache_key(login)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._login_cache.get(key)
            if cached is None:
                return None
            stored_at, exists = cached
            if now - stored_at > self._CACHE_TTL_SECONDS:
                self._login_cache.pop(key, None)
                return None
            return exists

    def _cache_set(self, login: str, exists: bool) -> None:
        key = self._cache_key(login)
        with self._cache_lock:
            self._login_cache[key] = (time.monotonic(), exists)

    def _cache_remove(self, login: str) -> None:
        key = self._cache_key(login)
        with self._cache_lock:
            self._login_cache.pop(key, None)

    def _search_existing_logins(
        self,
        client: paramiko.SSHClient,
        logins: list[str],
        *,
        cancel_event: threading.Event | None = None,
    ) -> set[str]:
        """Найти все занятые логины одним запуском zmprov.

        searchAccounts выполняет один LDAP-запрос по всем первичным адресам
        и алиасам. Это заменяет до N*D отдельных запусков `zmprov -l ga`.
        """
        address_to_login: dict[str, str] = {}
        clauses: list[str] = []

        for login in logins:
            for domain in self.settings.zimbra_domains:
                email = f"{login}@{domain}".lower()
                address_to_login[email] = login
                escaped = self._escape_ldap_filter_value(email)
                clauses.extend(
                    [
                        f"(mail={escaped})",
                        f"(zimbraMailAlias={escaped})",
                        f"(zimbraMailDeliveryAddress={escaped})",
                    ]
                )

        if not clauses:
            return set()

        ldap_query = f"(|{''.join(clauses)})"
        # Число найденных объектов не может быть больше числа проверяемых
        # адресов, но небольшой запас полезен при нескольких совпадениях.
        limit = max(20, min(len(address_to_login) * 2, 500))
        output = self._execute_zmprov_lookup(
            client,
            ["sa", "-v", ldap_query, str(limit)],
            cancel_event=cancel_event,
        )

        existing: set[str] = set()
        interesting_attributes = {
            "mail",
            "zimbramailalias",
            "zimbramaildeliveryaddress",
            "name",
        }

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            value = ""
            lower_line = line.lower()
            if lower_line.startswith("# name "):
                value = line[7:].strip()
            elif ":" in line:
                attribute, candidate_value = line.split(":", 1)
                if attribute.strip().lower() in interesting_attributes:
                    value = candidate_value.strip()

            normalized_value = value.lower()
            login = address_to_login.get(normalized_value)
            if login:
                existing.add(login)

        return existing

    def _fallback_existing_logins(
        self,
        client: paramiko.SSHClient,
        logins: list[str],
        *,
        cancel_event: threading.Event | None = None,
    ) -> set[str]:
        """Совместимый запасной способ для старых сборок Zimbra."""
        existing: set[str] = set()
        for login in logins:
            self._raise_if_cancelled(cancel_event)
            for domain in self.settings.zimbra_domains:
                self._raise_if_cancelled(cancel_event)
                email = f"{login}@{domain}"
                try:
                    self._execute_zmprov_lookup(
                        client,
                        ["ga", email, "zimbraId"],
                        cancel_event=cancel_event,
                    )
                    existing.add(login)
                    break
                except RuntimeError as exc:
                    if self._is_not_found_error(exc):
                        continue
                    raise
        return existing

    def address_exists(self, email: str) -> bool:
        if not self.settings.zimbra_check_enabled:
            return False
        try:
            client = self._client()
            try:
                self._execute_zmprov_lookup(client, ["ga", email, "zimbraId"])
            finally:
                client.close()
            return True
        except RuntimeError as exc:
            if self._is_not_found_error(exc):
                return False
            raise

    def logins_exist_any_domain(
        self,
        logins: list[str],
        *,
        force_refresh: bool = False,
        background: bool = False,
    ) -> set[str]:
        if not self.settings.zimbra_check_enabled:
            return set()
        if self.settings.zimbra_backend == "disabled":
            raise RuntimeError("Zimbra backend отключен")

        normalized = list(
            dict.fromkeys(
                login.strip().lower()
                for login in logins
                if login.strip()
            )
        )
        if not normalized:
            return set()

        existing: set[str] = set()
        missing: list[str] = []

        if force_refresh:
            # Явная кнопка «Проверить снова» и итоговая проверка перед
            # созданием должны видеть внешние удаления немедленно, не ожидая
            # окончания короткого кэша Zimbra.
            missing = list(normalized)
        else:
            for login in normalized:
                cached = self._cache_get(login)
                if cached is None:
                    missing.append(login)
                elif cached:
                    existing.add(login)

        if not missing:
            return existing

        cancel_event = self.begin_background_check() if background else None

        def execute_query() -> set[str]:
            self._raise_if_cancelled(cancel_event)

            still_missing: list[str] = []
            if force_refresh:
                still_missing = list(missing)
            else:
                for login in missing:
                    cached = self._cache_get(login)
                    if cached is None:
                        still_missing.append(login)
                    elif cached:
                        existing.add(login)

            if not still_missing:
                return existing

            client = self._client()
            try:
                try:
                    found = self._search_existing_logins(
                        client,
                        still_missing,
                        cancel_event=cancel_event,
                    )
                except BackgroundLoginCheckCancelled:
                    raise
                except RuntimeError:
                    # Старые или измененные сборки Zimbra могут не принимать
                    # searchAccounts в ожидаемом виде. В этом случае сохраняем
                    # прежний надежный способ проверки.
                    found = self._fallback_existing_logins(
                        client,
                        still_missing,
                        cancel_event=cancel_event,
                    )
            finally:
                client.close()

            self._raise_if_cancelled(cancel_event)
            for login in still_missing:
                is_existing = login in found
                self._cache_set(login, is_existing)
                if is_existing:
                    existing.add(login)

            return existing

        try:
            if force_refresh and not background:
                # Финальная проверка выбранного логина имеет приоритет и не
                # ожидает завершения полного списка альтернатив.
                return execute_query()

            # Обычные параллельные запросы по-прежнему объединяем одним lock.
            while not self._query_lock.acquire(timeout=0.1):
                self._raise_if_cancelled(cancel_event)
            try:
                return execute_query()
            finally:
                self._query_lock.release()
        finally:
            if cancel_event is not None:
                self.finish_background_check(cancel_event)

    def login_exists_any_domain(
        self,
        login: str,
        *,
        force_refresh: bool = False,
    ) -> bool:
        normalized = login.strip().lower()
        return normalized in self.logins_exist_any_domain(
            [normalized],
            force_refresh=force_refresh,
            background=False,
        )

    def create_account(
        self,
        login: str,
        domain: str,
        password: str,
        last_name: str,
        first_name: str,
        middle_name: str,
    ) -> ZimbraCreateResult:
        primary_domain = (
            self.settings.zimbra_primary_domain
            if self.settings.zimbra_domain_mode == "primary_alias"
            else domain
        )
        primary_email = f"{login}@{primary_domain}"
        display_name = " ".join(
            part for part in [last_name, first_name, middle_name] if part
        )

        args = [
            "ca",
            primary_email,
            password,
            "displayName",
            display_name,
            "zimbraPrefFromDisplay",
            display_name,
            "givenName",
            first_name,
        ]
        if middle_name:
            # В учетной записи Zimbra поле Middle Name / Отчество
            # хранится в стандартном LDAP-атрибуте initials.
            args.extend(["initials", middle_name])
        args.extend(["sn", last_name])
        if self.settings.zimbra_cos_id:
            args.extend(["zimbraCOSId", self.settings.zimbra_cos_id])
        self._run_zmprov(args)

        aliases: list[str] = []
        if (
            self.settings.zimbra_domain_mode == "primary_alias"
            and self.settings.zimbra_create_aliases
        ):
            for alias_domain in self.settings.zimbra_domains:
                alias = f"{login}@{alias_domain}"
                if alias.lower() == primary_email.lower():
                    continue
                self._run_zmprov(["aaa", primary_email, alias])
                aliases.append(alias)

        if not self.settings.dry_run:
            self._cache_set(login, True)

        return ZimbraCreateResult(
            primary_email=primary_email,
            aliases=tuple(aliases),
        )

    def delete_account(self, email: str) -> None:
        self._run_zmprov(["da", email])
        login = email.split("@", 1)[0].strip().lower()
        if login:
            self._cache_remove(login)

    def set_dismissal_note(self, email: str, dismissal_date: date) -> None:
        self._run_zmprov(
            ["ma", email, "zimbraNotes", dismissal_date.strftime("%d.%m.%Y")]
        )
