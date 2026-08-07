from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.config import Settings
from sqlalchemy.orm import Session
from app.services.onec_imap import OneCAttachment, OneCImapService
from app.services.hr_registry import HRRegistryService
from app.services.onec_xlsx import parse_onec_xlsx, worker_snapshot


class OneCImportService:
    """Получение и анализ выгрузки 1С без внешних изменений."""

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

    def analyze_latest(self) -> dict:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        attachment = OneCImapService(
            self.settings
        ).find_latest_attachment()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = (
            self.archive_dir
            / f"{timestamp}_{attachment.filename}"
        )
        archive_path.write_bytes(attachment.payload)
        self.current_file.write_bytes(attachment.payload)

        workbook = parse_onec_xlsx(
            self.current_file,
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

        report = {
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
            "registry": None,
            "dry_run": True,
            "external_actions": {
                "ad": False,
                "zimbra": False,
                "mail": False,
                "itinvent": False,
            },
        }

        if self.db is not None:
            report["registry"] = HRRegistryService(self.settings, self.db).sync_and_reconcile(workbook)

        self.snapshot_file.write_text(
            json.dumps(
                current_snapshot,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.report_file.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        return report

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
        placements=[{"department":str(p.get("department") or ""),"position":str(p.get("position") or "")} for p in (value.get("placements") or [])]
        placements.sort(key=lambda item:(item["department"].casefold(),item["position"].casefold()))
        return {"worker_key":value.get("worker_key") or "","fio":value.get("fio") or "","email":value.get("email") or None,"login":value.get("login") or None,"placements":placements}

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
            if cls._meaningful_worker_snapshot(previous.get(key) or {}) != cls._meaningful_worker_snapshot(current.get(key) or {})
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
