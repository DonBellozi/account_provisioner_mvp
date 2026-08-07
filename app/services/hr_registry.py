from __future__ import annotations
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import Settings
from app.models import HRPerson, HRSourceRecord
from app.services.ad import ActiveDirectoryService
from app.services.onec_xlsx import OneCWorkbook
from app.services.zimbra import ZimbraService

AD_LABELS={"enabled":"Есть, включена","disabled":"Есть, отключена","missing":"Не найдена","error":"Ошибка проверки","not_checked":"Не проверено","no_login":"Нет логина"}
ZIMBRA_LABELS={"present":"Адрес существует","missing":"Адрес не найден","error":"Ошибка проверки","not_checked":"Не проверено","no_email":"Нет email в 1С"}
RECON_LABELS={"ok":"Соответствует","issue":"Требует проверки","error":"Ошибка сверки","not_checked":"Не проверено полностью"}
def utcnow(): return datetime.now(timezone.utc)

class HRRegistryService:
    def __init__(self, settings: Settings, db: Session):
        self.settings=settings; self.db=db
        self.source_id=settings.onec_source_id.strip() or "org_com"
        self.source_name=settings.onec_source_name.strip() or self.source_id

    def sync_workbook(self, workbook: OneCWorkbook) -> dict[str,int]:
        now=utcnow(); current_keys={w.worker_key for w in workbook.workers}
        source_records=self.db.scalars(select(HRSourceRecord).where(HRSourceRecord.source_id==self.source_id)).all()
        existing={r.worker_key:r for r in source_records}
        people=self.db.scalars(select(HRPerson).where(HRPerson.worker_key.in_(current_keys))).all() if current_keys else []
        people_by_key={p.worker_key:p for p in people}; created_people=0; created_records=0
        for worker in workbook.workers:
            person=people_by_key.get(worker.worker_key)
            if person is None:
                person=HRPerson(worker_key=worker.worker_key,fio=worker.fio,first_seen_at=now,last_seen_at=now); self.db.add(person); people_by_key[worker.worker_key]=person; created_people+=1
            else:
                person.fio=worker.fio; person.last_seen_at=now
            placements_json=json.dumps([{"department":p.department or "","position":p.position or ""} for p in worker.placements],ensure_ascii=False,sort_keys=True)
            record=existing.get(worker.worker_key)
            if record is None:
                record=HRSourceRecord(worker_key=worker.worker_key,source_id=self.source_id,source_name=self.source_name,fio=worker.fio,corporate_email=worker.email or "",login=worker.login or "",placements_json=placements_json,is_present=True,first_seen_at=now,last_seen_at=now)
                self.db.add(record); existing[worker.worker_key]=record; created_records+=1
            else:
                record.source_name=self.source_name; record.fio=worker.fio; record.corporate_email=worker.email or ""; record.login=worker.login or ""; record.placements_json=placements_json; record.is_present=True; record.last_seen_at=now
        missing=0
        for key,record in existing.items():
            if key not in current_keys and record.is_present: record.is_present=False; missing+=1
        self.db.commit()
        return {"created_people":created_people,"created_source_records":created_records,"marked_missing":missing}

    def reconcile_current(self) -> dict[str,int|str]:
        records=self.db.scalars(select(HRSourceRecord).where(HRSourceRecord.source_id==self.source_id,HRSourceRecord.is_present.is_(True))).all()
        logins=sorted({r.login.lower() for r in records if r.login}); emails=sorted({r.corporate_email.lower() for r in records if r.corporate_email})
        ad_users={}; ad_error=""
        if self.settings.ad_check_enabled and logins:
            try: ad_users=ActiveDirectoryService(self.settings).users_by_logins(logins)
            except Exception as exc: ad_error=str(exc)
        z_addresses=set(); z_error=""
        if self.settings.zimbra_check_enabled and emails:
            try: z_addresses=ZimbraService(self.settings).addresses_exist(emails)
            except Exception as exc: z_error=str(exc)
        now=utcnow()
        for r in records:
            errors=[]; login=r.login.strip().lower(); email=r.corporate_email.strip().lower()
            if not login: r.ad_status="no_login"
            elif not self.settings.ad_check_enabled: r.ad_status="not_checked"
            elif ad_error: r.ad_status="error"; errors.append(f"AD: {ad_error}")
            else:
                user=ad_users.get(login); r.ad_status="missing" if user is None else ("enabled" if user.is_enabled else "disabled")
            if not email: r.zimbra_status="no_email"
            elif not self.settings.zimbra_check_enabled: r.zimbra_status="not_checked"
            elif z_error: r.zimbra_status="error"; errors.append(f"Zimbra: {z_error}")
            else: r.zimbra_status="present" if email in z_addresses else "missing"
            if r.ad_status=="error" or r.zimbra_status=="error": r.reconciliation_status="error"
            elif r.ad_status in {"missing","disabled","no_login"} or r.zimbra_status in {"missing","no_email"}: r.reconciliation_status="issue"
            elif r.ad_status=="not_checked" or r.zimbra_status=="not_checked": r.reconciliation_status="not_checked"
            else: r.reconciliation_status="ok"
            r.reconciliation_error="\n".join(errors); r.reconciled_at=now
        self.db.commit(); return self.summary()

    def sync_and_reconcile(self, workbook): return {"sync":self.sync_workbook(workbook),"reconciliation":self.reconcile_current()}

    def summary(self):
        records=self.db.scalars(select(HRSourceRecord).where(HRSourceRecord.source_id==self.source_id,HRSourceRecord.is_present.is_(True))).all()
        s={"source_id":self.source_id,"source_name":self.source_name,"total":len(records),"ok":0,"issues":0,"errors":0,"not_checked":0,"ad_missing":0,"ad_disabled":0,"zimbra_missing":0,"no_email":0}
        for r in records:
            if r.reconciliation_status=="ok": s["ok"]+=1
            elif r.reconciliation_status=="issue": s["issues"]+=1
            elif r.reconciliation_status=="error": s["errors"]+=1
            else: s["not_checked"]+=1
            if r.ad_status=="missing": s["ad_missing"]+=1
            elif r.ad_status=="disabled": s["ad_disabled"]+=1
            if r.zimbra_status=="missing": s["zimbra_missing"]+=1
            elif r.zimbra_status=="no_email": s["no_email"]+=1
        return s

    def list_rows(self, *, query="", status="all", limit=1000):
        records=self.db.scalars(select(HRSourceRecord).where(HRSourceRecord.source_id==self.source_id,HRSourceRecord.is_present.is_(True)).order_by(HRSourceRecord.fio)).all(); q=query.strip().casefold(); rows=[]
        for r in records:
            if q and q not in " ".join([r.fio,r.login,r.corporate_email]).casefold(): continue
            if status=="issues" and r.reconciliation_status not in {"issue","error"}: continue
            if status=="ok" and r.reconciliation_status!="ok": continue
            if status=="not_checked" and r.reconciliation_status!="not_checked": continue
            try: placements=json.loads(r.placements_json or "[]")
            except json.JSONDecodeError: placements=[]
            labels=[]
            for p in placements:
                d=str(p.get("department") or "").strip(); pos=str(p.get("position") or "").strip(); label=" / ".join(x for x in [d,pos] if x)
                if label: labels.append(label)
            create_url=""
            if r.ad_status=="missing" and r.zimbra_status=="missing": create_url="/employees/new?"+urlencode({"fio":r.fio})
            rows.append({"id":r.id,"fio":r.fio,"source_name":r.source_name,"email":r.corporate_email,"login":r.login,"placements":labels,"ad_status":r.ad_status,"ad_label":AD_LABELS.get(r.ad_status,r.ad_status),"zimbra_status":r.zimbra_status,"zimbra_label":ZIMBRA_LABELS.get(r.zimbra_status,r.zimbra_status),"reconciliation_status":r.reconciliation_status,"reconciliation_label":RECON_LABELS.get(r.reconciliation_status,r.reconciliation_status),"error":r.reconciliation_error,"reconciled_at":r.reconciled_at,"create_url":create_url})
            if len(rows)>=limit: break
        return rows
