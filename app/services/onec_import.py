from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import OneCImportRun
from app.services.hr_registry import HRRegistryService
from app.services.onec_imap import OneCAttachment, OneCImapService
from app.services.onec_xlsx import parse_onec_xlsx, worker_snapshot


SUCCESSFUL_IMPORT_STATUSES = {"success", "partial"}

TRIGGER_LABELS = {
    "manual": "Вручную",
    "scheduled": "По расписанию",
    "startup": "После запуска",
}
STATUS_LABELS = {
    "running": "Выполняется",
    "success": "Успешно",
    "partial": "Принят, сверка с ошибкой",
    "duplicate": "Без изменений",
    "failed": "Ошибка",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OneCImportService:
    """Получение, история и безопасное обновление кадровой выгрузки 1С."""

    _analysis_lock = threading.Lock()

    def __init__(self, settings: Settings, db: Session | None = None):
        self.settings = settings
        self.db = db
        self.data_dir = Path(settings.onec_data_dir)
        self.archive_dir = self.data_dir / "archive"
        self.current_file = self.data_dir / "current.xlsx"
        self.snapshot_file = self.data_dir / "current_snapshot.json"
        self.report_file = self.data_dir / "last_analysis.json"

    @property
    def hash_secret(self) -> str:
        return (
            self.settings.onec_worker_hash_secret.strip()
            or self.settings.app_secret_key
        )

    @property
    def hash_secret_source(self) -> str:
        return (
            "ONEC_WORKER_HASH_SECRET"
            if self.settings.onec_worker_hash_secret.strip()
            else "APP_SECRET_KEY (временно)"
        )

    def test_connection(self) -> str:
        return OneCImapService(self.settings).test_connection()

    def find_latest(self) -> dict:
        attachment = OneCImapService(
            self.settings
        ).find_latest_attachment()
        return self._mail_payload(attachment)

    def load_last_report(self) -> dict | None:
        if not self.report_file.is_file():
            return None
        try:
            return json.loads(self.report_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def history(self, limit: int = 20) -> list[dict]:
        if self.db is None:
            return []

        rows = self.db.scalars(
            select(OneCImportRun)
            .order_by(desc(OneCImportRun.started_at), desc(OneCImportRun.id))
            .limit(max(1, min(limit, 100)))
        ).all()

        return [
            {
                "id": row.id,
                "trigger": row.trigger,
                "trigger_label": TRIGGER_LABELS.get(row.trigger, row.trigger),
                "status": row.status,
                "status_label": STATUS_LABELS.get(row.status, row.status),
                "source_id": row.source_id,
                "filename": row.filename,
                "file_hash": row.file_hash,
                "hash_short": row.file_hash[:12] if row.file_hash else "",
                "workers_count": row.workers_count,
                "placements_count": row.placements_count,
                "new_workers": row.new_workers,
                "missing_workers": row.missing_workers,
                "changed_workers": row.changed_workers,
                "registry_ok": row.registry_ok,
                "registry_issues": row.registry_issues,
                "registry_errors": row.registry_errors,
                "message": row.message,
                "error_message": row.error_message,
                "started_at": row.started_at,
                "completed_at": row.completed_at,
            }
            for row in rows
        ]

    def analyze_latest(
        self,
        *,
        trigger: str = "manual",
    ) -> dict:
        trigger = trigger.strip().lower()
        if trigger not in TRIGGER_LABELS:
            raise ValueError("Неизвестный тип запуска импорта 1С")

        if not self._analysis_lock.acquire(blocking=False):
            raise RuntimeError(
                "Импорт 1С уже выполняется. Дождитесь завершения текущей операции."
            )

        run_id: int | None = None
        incoming_path: Path | None = None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.archive_dir.mkdir(parents=True, exist_ok=True)

            run_id = self._start_run(trigger)
            attachment = OneCImapService(
                self.settings
            ).find_latest_attachment()
            self._update_run_mail(run_id, attachment)

            duplicate = self._find_successful_hash(
                attachment.file_hash,
                exclude_run_id=run_id,
            )
            baseline_duplicate = (
                duplicate is None
                and self._current_file_hash() == attachment.file_hash
                and self.load_last_report() is not None
            )

            if duplicate is not None or baseline_duplicate:
                source = (
                    f"импорт № {duplicate.id}"
                    if duplicate is not None
                    else "текущий успешный снимок"
                )
                message = (
                    "Вложение уже обработано: SHA-256 совпадает с "
                    f"{source}. Повторный анализ не выполнялся."
                )
                self._finish_run(
                    run_id,
                    status="duplicate",
                    message=message,
                    source_id=(
                        duplicate.source_id
                        if duplicate is not None
                        else self.settings.onec_source_domain.strip().lower()
                    ),
                )
                report = dict(self.load_last_report() or {})
                report["mail"] = self._mail_payload(attachment)
                report["import_status"] = "duplicate"
                report["import_message"] = message
                report["import_run_id"] = run_id
                return report

            incoming_path = self._write_incoming(attachment.payload)
            workbook = parse_onec_xlsx(
                incoming_path,
                hash_secret=self.hash_secret,
                header_search_rows=self.settings.onec_header_search_rows,
            )

            current_snapshot = {
                worker.worker_key: worker_snapshot(worker)
                for worker in workbook.workers
            }
            previous_snapshot = self._load_snapshot()
            comparison = self._compare(
                previous_snapshot,
                current_snapshot,
            )

            registry_payload = None
            registry_warning = ""
            source_id = self.settings.onec_source_domain.strip().lower()

            if self.db is not None:
                registry = HRRegistryService(self.settings, self.db)
                try:
                    sync_summary = registry.sync_workbook(workbook)
                except Exception:
                    self.db.rollback()
                    raise

                source_id = registry.source_id
                try:
                    reconciliation_summary = registry.reconcile_current()
                    registry_payload = {
                        "sync": sync_summary,
                        "reconciliation": reconciliation_summary,
                    }
                except Exception as exc:
                    # Кадровая выгрузка уже валидна и синхронизирована локально.
                    # Недоступность AD/Zimbra не должна терять новый файл 1С.
                    self.db.rollback()
                    registry_warning = str(exc)
                    registry_payload = {
                        "sync": sync_summary,
                        "reconciliation": None,
                        "reconciliation_error": registry_warning,
                    }

            report = self._build_report(
                attachment=attachment,
                workbook=workbook,
                comparison=comparison,
                previous_snapshot=previous_snapshot,
                registry_payload=registry_payload,
                registry_warning=registry_warning,
            )

            # Только после успешного разбора XLSX и локальной синхронизации
            # заменяем рабочий снимок. Все замены выполняются атомарно.
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_filename = Path(attachment.filename).name
            archive_filename = (
                f"{timestamp}_{attachment.file_hash[:12]}_{safe_filename}"
            )
            archive_path = self.archive_dir / archive_filename

            self._atomic_write_bytes(archive_path, attachment.payload)
            self._atomic_write_bytes(self.current_file, attachment.payload)
            self._atomic_write_json(self.snapshot_file, current_snapshot)
            self._atomic_write_json(self.report_file, report)

            status = "partial" if registry_warning else "success"
            message = (
                "Выгрузка принята, но сверка AD/Zimbra завершилась с ошибкой."
                if registry_warning
                else "Выгрузка успешно получена и обработана."
            )

            report["import_status"] = status
            report["import_message"] = message
            report["import_run_id"] = run_id

            reconciliation = (
                registry_payload.get("reconciliation")
                if isinstance(registry_payload, dict)
                else None
            )
            self._finish_run(
                run_id,
                status=status,
                message=message,
                error_message=registry_warning,
                source_id=source_id,
                archive_filename=archive_filename,
                workers_count=len(workbook.workers),
                placements_count=workbook.placements_count,
                new_workers=int(comparison["new_workers"]),
                missing_workers=int(comparison["missing_workers"]),
                changed_workers=int(comparison["changed_workers"]),
                registry_ok=int((reconciliation or {}).get("ok", 0)),
                registry_issues=int((reconciliation or {}).get("issues", 0)),
                registry_errors=int((reconciliation or {}).get("errors", 0)),
            )
            return report

        except Exception as exc:
            if self.db is not None:
                self.db.rollback()
            self._finish_run(
                run_id,
                status="failed",
                message="Импорт не выполнен. Предыдущий успешный снимок сохранен.",
                error_message=str(exc),
            )
            raise
        finally:
            if incoming_path is not None:
                incoming_path.unlink(missing_ok=True)
            self._analysis_lock.release()

    def _start_run(self, trigger: str) -> int | None:
        if self.db is None:
            return None
        run = OneCImportRun(
            trigger=trigger,
            status="running",
            source_id=self.settings.onec_source_domain.strip().lower(),
            started_at=utcnow(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run.id

    def _update_run_mail(
        self,
        run_id: int | None,
        attachment: OneCAttachment,
    ) -> None:
        if self.db is None or run_id is None:
            return
        run = self.db.get(OneCImportRun, run_id)
        if run is None:
            return
        run.mail_uid = attachment.uid
        run.message_date = attachment.message_date
        run.sender = attachment.sender
        run.subject = attachment.subject
        run.filename = attachment.filename
        run.file_hash = attachment.file_hash
        self.db.commit()

    def _finish_run(
        self,
        run_id: int | None,
        *,
        status: str,
        message: str = "",
        error_message: str = "",
        source_id: str | None = None,
        archive_filename: str = "",
        workers_count: int = 0,
        placements_count: int = 0,
        new_workers: int = 0,
        missing_workers: int = 0,
        changed_workers: int = 0,
        registry_ok: int = 0,
        registry_issues: int = 0,
        registry_errors: int = 0,
    ) -> None:
        if self.db is None or run_id is None:
            return
        try:
            run = self.db.get(OneCImportRun, run_id)
            if run is None:
                return
            run.status = status
            if source_id is not None:
                run.source_id = source_id
            run.archive_filename = archive_filename
            run.workers_count = workers_count
            run.placements_count = placements_count
            run.new_workers = new_workers
            run.missing_workers = missing_workers
            run.changed_workers = changed_workers
            run.registry_ok = registry_ok
            run.registry_issues = registry_issues
            run.registry_errors = registry_errors
            run.message = message[:4000]
            run.error_message = error_message[:4000]
            run.completed_at = utcnow()
            self.db.commit()
        except Exception:
            self.db.rollback()

    def _find_successful_hash(
        self,
        file_hash: str,
        *,
        exclude_run_id: int | None = None,
    ) -> OneCImportRun | None:
        if self.db is None or not file_hash:
            return None
        query = (
            select(OneCImportRun)
            .where(
                OneCImportRun.file_hash == file_hash,
                OneCImportRun.status.in_(SUCCESSFUL_IMPORT_STATUSES),
            )
            .order_by(desc(OneCImportRun.id))
        )
        if exclude_run_id is not None:
            query = query.where(OneCImportRun.id != exclude_run_id)
        return self.db.scalars(query.limit(1)).first()

    def _current_file_hash(self) -> str:
        if not self.current_file.is_file():
            return ""
        try:
            digest = hashlib.sha256()
            with self.current_file.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    def _write_incoming(self, payload: bytes) -> Path:
        handle = tempfile.NamedTemporaryFile(
            prefix=".incoming_",
            suffix=".xlsx",
            dir=self.data_dir,
            delete=False,
        )
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            return Path(handle.name)
        finally:
            handle.close()

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(temp_path, path)
        finally:
            try:
                handle.close()
            except Exception:
                pass
            temp_path.unlink(missing_ok=True)

    @classmethod
    def _atomic_write_json(cls, path: Path, value: dict) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        cls._atomic_write_bytes(path, payload)

    def _build_report(
        self,
        *,
        attachment: OneCAttachment,
        workbook,
        comparison: dict,
        previous_snapshot: dict,
        registry_payload: dict | None,
        registry_warning: str,
    ) -> dict:
        return {
            "analyzed_at": datetime.now().replace(
                microsecond=0
            ).isoformat(),
            "mail": self._mail_payload(attachment),
            "workers_count": len(workbook.workers),
            "placements_count": workbook.placements_count,
            "multiple_placements_count": (
                workbook.multiple_placements_count
            ),
            "missing_email_count": workbook.missing_email_count,
            "header_row": workbook.header_row,
            "headers": list(workbook.headers),
            "detected_columns": workbook.detected_columns,
            "potential_dismissal_columns": list(
                workbook.potential_dismissal_columns
            ),
            "dismissal_calculation_available": bool(
                workbook.potential_dismissal_columns
            ),
            "dismissal_note": (
                "В XLSX найдено поле, похожее на поле увольнения. "
                "Оно пока только обнаружено и не участвует в автоматических действиях."
                if workbook.potential_dismissal_columns
                else
                "Поле даты увольнения в текущей выгрузке не найдено. "
                "Окончательные увольнения не рассчитываются."
            ),
            "comparison": comparison,
            "snapshot_baseline_available": bool(
                previous_snapshot
            ),
            "hash_secret_source": self.hash_secret_source,
            "registry": registry_payload,
            "registry_warning": registry_warning,
            "dry_run": True,
            "external_actions": {
                "ad": False,
                "zimbra": False,
                "mail": False,
                "itinvent": False,
            },
        }

    def _load_snapshot(self) -> dict[str, dict]:
        if not self.snapshot_file.is_file():
            return {}
        try:
            data = json.loads(
                self.snapshot_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _meaningful_worker_snapshot(value: dict) -> dict:
        placements = [
            {
                "department": str(p.get("department") or ""),
                "position": str(p.get("position") or ""),
            }
            for p in (value.get("placements") or [])
        ]
        placements.sort(
            key=lambda item: (
                item["department"].casefold(),
                item["position"].casefold(),
            )
        )
        return {
            "worker_key": value.get("worker_key") or "",
            "fio": value.get("fio") or "",
            "email": value.get("email") or None,
            "login": value.get("login") or None,
            "placements": placements,
        }

    @classmethod
    def _compare(
        cls,
        previous: dict[str, dict],
        current: dict[str, dict],
    ) -> dict:
        previous_keys = set(previous)
        current_keys = set(current)

        new_keys = current_keys - previous_keys
        missing_keys = previous_keys - current_keys
        common = previous_keys & current_keys

        changed_keys = {
            key
            for key in common
            if cls._meaningful_worker_snapshot(previous.get(key) or {})
            != cls._meaningful_worker_snapshot(current.get(key) or {})
        }

        def samples(keys: set[str], source: dict[str, dict]) -> list[dict]:
            rows = [
                {
                    "fio": source[key].get("fio") or "",
                    "email": source[key].get("email") or "",
                    "login": source[key].get("login") or "",
                }
                for key in keys
                if key in source
            ]
            rows.sort(
                key=lambda item: (
                    item["fio"].casefold(),
                    item["login"].casefold(),
                )
            )
            return rows[:20]

        return {
            "new_workers": len(new_keys),
            "missing_workers": len(missing_keys),
            "changed_workers": len(changed_keys),
            "new_samples": samples(new_keys, current),
            "missing_samples": samples(missing_keys, previous),
            "changed_samples": samples(changed_keys, current),
            "missing_is_dismissal": False,
            "note": (
                "Исчезновение работника из выгрузки считается только сигналом "
                "для проверки и никогда не трактуется как увольнение."
            ),
        }

    @staticmethod
    def _mail_payload(attachment: OneCAttachment) -> dict:
        return {
            "uid": attachment.uid,
            "message_date": attachment.message_date,
            "sender": attachment.sender,
            "subject": attachment.subject,
            "filename": attachment.filename,
            "file_hash": attachment.file_hash,
        }
