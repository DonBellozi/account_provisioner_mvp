from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import paramiko

from app.config import Settings


@dataclass(frozen=True)
class ZimbraCreateResult:
    primary_email: str
    aliases: tuple[str, ...]


class ZimbraService:
    def __init__(self, settings: Settings):
        self.settings = settings

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
        # Передаем команду через stdin, чтобы пароль создаваемого ящика
        # не попадал в аргументы удаленного процесса и список процессов.
        stdin.write(shlex.join(args) + "\n")
        stdin.channel.shutdown_write()

        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        combined = f"{err}\n{out}"

        if code != 0 and not (
            allow_not_found
            and ("NO_SUCH_ACCOUNT" in combined or "account.NO_SUCH_ACCOUNT" in combined)
        ):
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

    def address_exists(self, email: str) -> bool:
        if not self.settings.zimbra_check_enabled:
            return False
        try:
            self._run_zmprov(["ga", email, "zimbraId"], mutating=False)
            return True
        except RuntimeError as exc:
            if self._is_not_found_error(exc):
                return False
            raise

    def login_exists_any_domain(self, login: str) -> bool:
        if not self.settings.zimbra_check_enabled:
            return False
        if self.settings.zimbra_backend == "disabled":
            raise RuntimeError("Zimbra backend отключен")

        # Для всех доменов одного логина используем одно SSH-подключение.
        # Раньше для каждого домена выполнялась отдельная SSH-аутентификация.
        client = self._client()
        try:
            for domain in self.settings.zimbra_domains:
                email = f"{login}@{domain}"
                try:
                    self._execute_zmprov(client, ["ga", email, "zimbraId"])
                    return True
                except RuntimeError as exc:
                    if self._is_not_found_error(exc):
                        continue
                    raise
            return False
        finally:
            client.close()

    def create_account(
        self,
        login: str,
        domain: str,
        password: str,
        last_name: str,
        first_name: str,
        middle_name: str,
    ) -> ZimbraCreateResult:
        primary_domain = self.settings.zimbra_primary_domain if self.settings.zimbra_domain_mode == "primary_alias" else domain
        primary_email = f"{login}@{primary_domain}"
        display_name = " ".join(part for part in [last_name, first_name, middle_name] if part)

        args = [
            "ca", primary_email, password,
            "displayName", display_name,
            "givenName", first_name,
            "sn", last_name,
        ]
        if self.settings.zimbra_cos_id:
            args.extend(["zimbraCOSId", self.settings.zimbra_cos_id])
        self._run_zmprov(args)

        aliases: list[str] = []
        if self.settings.zimbra_domain_mode == "primary_alias" and self.settings.zimbra_create_aliases:
            for alias_domain in self.settings.zimbra_domains:
                alias = f"{login}@{alias_domain}"
                if alias.lower() == primary_email.lower():
                    continue
                self._run_zmprov(["aaa", primary_email, alias])
                aliases.append(alias)
        return ZimbraCreateResult(primary_email=primary_email, aliases=tuple(aliases))

    def delete_account(self, email: str) -> None:
        self._run_zmprov(["da", email])

    def set_dismissal_note(self, email: str, dismissal_date: date) -> None:
        self._run_zmprov(["ma", email, "zimbraNotes", dismissal_date.strftime("%d.%m.%Y")])
