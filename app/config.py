from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Регистрация учетных записей"
    app_secret_key: str = "change-me-to-a-long-secret"
    app_base_url: str = "http://localhost:8000"
    app_timezone: str = "Asia/Almaty"
    database_url: str = "sqlite:///./data/app.db"
    dry_run: bool = True

    # Для HTTP оставляем false. После перехода на HTTPS меняем на true.
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_name: str = "account_provisioner_session"

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "ChangeMeNow!123"

    auth_mode: Literal["local", "hybrid", "ad"] = "hybrid"
    ad_login_enabled: bool = False
    ad_allowed_group_dn: str = ""

    ad_server: str = ""
    ad_port: int = 636
    ad_use_ssl: bool = True
    ad_verify_tls: bool = True
    ad_ca_cert_file: str = ""
    ad_domain: str = ""
    ad_base_dn: str = ""
    ad_users_ou: str = ""
    ad_bind_dn: str = ""
    ad_bind_password: str = ""
    ad_upn_suffix: str = ""
    ad_force_change_at_first_logon: bool = True
    ad_default_group_dns: Annotated[list[str], NoDecode] = Field(default_factory=list)

    zimbra_backend: Literal["ssh_zmprov", "disabled"] = "ssh_zmprov"
    zimbra_ssh_host: str = ""
    zimbra_ssh_port: int = 22
    zimbra_ssh_user: str = "provisioner"
    zimbra_ssh_auth: Literal["key", "password", "auto"] = "key"
    zimbra_ssh_private_key: str = "/run/secrets/zimbra_ssh_key"
    zimbra_ssh_password: str = ""
    zimbra_ssh_password_file: str = ""
    zimbra_ssh_known_hosts: str = "/app/known_hosts"
    zimbra_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    zimbra_domain_mode: Literal["separate", "primary_alias"] = "separate"
    zimbra_primary_domain: str = ""
    zimbra_create_aliases: bool = True
    zimbra_cos_id: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_timeout_seconds: int = 20
    smtp_retry_attempts: int = 3
    smtp_retry_delay_seconds: float = 2.0

    mail_password_length: int = 16
    mail_password_specials: str = "!@#$%&?"
    ad_password_min_length: int = 8
    ad_password_max_length: int = 12
    ad_password_specials: str = "!@#$%&?"

    rollback_ad_on_zimbra_failure: bool = False

    @field_validator("zimbra_domains", mode="before")
    @classmethod
    def split_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("ad_default_group_dns", mode="before")
    @classmethod
    def split_group_dns(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(";") if item.strip()]
        return value

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if len(value) < 16:
            raise ValueError("APP_SECRET_KEY должен быть не короче 16 символов")
        return value

    @field_validator("session_cookie_name")
    @classmethod
    def validate_cookie_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("SESSION_COOKIE_NAME не может быть пустым")
        return value

    def ensure_runtime_directories(self) -> None:
        if self.database_url.startswith("sqlite:///"):
            path = Path(self.database_url.removeprefix("sqlite:///"))
            path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_directories()
    return settings
