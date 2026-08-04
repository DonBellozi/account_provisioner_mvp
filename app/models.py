from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum

from sqlalchemy import Boolean, Date, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class OperationStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PARTIAL = "partial"
    SUCCESS = "success"
    FAILED = "failed"


class LocalUser(Base):
    __tablename__ = "local_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.ADMIN)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainAccessUser(Base):
    """Явно разрешенный доменный пользователь и его роль в приложении."""

    __tablename__ = "domain_access_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.OPERATOR)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class DomainMailProfile(Base):
    """Почтовый отправитель и два шаблона для конкретного домена."""

    __tablename__ = "domain_mail_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    sender_name: Mapped[str] = mapped_column(String(256), default="")
    sender_email: Mapped[str] = mapped_column(String(320))
    personal_subject: Mapped[str] = mapped_column(String(512))
    personal_body_html: Mapped[str] = mapped_column(Text)
    corporate_subject: Mapped[str] = mapped_column(String(512))
    corporate_body_html: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class ProvisioningOperation(Base):
    __tablename__ = "provisioning_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operator_username: Mapped[str] = mapped_column(String(256), index=True)
    last_name: Mapped[str] = mapped_column(String(128))
    first_name: Mapped[str] = mapped_column(String(128))
    middle_name: Mapped[str] = mapped_column(String(128), default="")
    personal_email: Mapped[str] = mapped_column(String(320))
    login: Mapped[str] = mapped_column(String(64), index=True)
    corporate_email: Mapped[str] = mapped_column(String(320), index=True)
    mail_domain: Mapped[str] = mapped_column(String(255))
    status: Mapped[OperationStatus] = mapped_column(Enum(OperationStatus), default=OperationStatus.DRAFT)
    ad_created: Mapped[bool] = mapped_column(Boolean, default=False)
    ad_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    zimbra_created: Mapped[bool] = mapped_column(Boolean, default=False)
    personal_mail_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    corporate_mail_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DismissalSchedule(Base):
    __tablename__ = "dismissal_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), index=True)
    corporate_email: Mapped[str] = mapped_column(String(320), index=True)
    dismissal_date: Mapped[date] = mapped_column(Date)
    operator_username: Mapped[str] = mapped_column(String(256))
    ad_expiration_set: Mapped[bool] = mapped_column(Boolean, default=False)
    zimbra_note_set: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(256), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    target: Mapped[str] = mapped_column(String(320), default="")
    result: Mapped[str] = mapped_column(String(64), default="success")
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
