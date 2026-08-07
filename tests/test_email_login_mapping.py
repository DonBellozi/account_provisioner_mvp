import io
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Stub runtime connector modules so isolated tests do not require ldap3/paramiko.
ad_stub = types.ModuleType("app.services.ad")
class ActiveDirectoryService: pass
class ADDirectoryUser:
    def __init__(self, username, display_name="", email="", distinguished_name="", is_enabled=True, object_guid=""):
        self.username=username
        self.display_name=display_name
        self.email=email
        self.distinguished_name=distinguished_name
        self.is_enabled=is_enabled
        self.object_guid=object_guid
ad_stub.ActiveDirectoryService=ActiveDirectoryService
ad_stub.ADDirectoryUser=ADDirectoryUser
sys.modules["app.services.ad"]=ad_stub

z_stub = types.ModuleType("app.services.zimbra")
class ZimbraService: pass
class ZimbraAccountIdentity:
    def __init__(self, zimbra_id, primary_email, login, addresses):
        self.zimbra_id=zimbra_id
        self.primary_email=primary_email
        self.login=login
        self.addresses=tuple(addresses)
z_stub.ZimbraService=ZimbraService
z_stub.ZimbraAccountIdentity=ZimbraAccountIdentity
sys.modules["app.services.zimbra"]=z_stub

from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord
from app.services.email_login_mapping import EmailLoginMappingService


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.settings = SimpleNamespace(
            zimbra_domains=["org.com", "org.ru"],
            onec_source_domain="",
        )
        self.db.add(
            HRSourceRecord(
                worker_key="a"*64,
                source_id="org_com",
                source_name="Организация .com",
                fio="Иванов Иван Иванович",
                corporate_email="boss@org.com",
                login="boss",
                placements_json="[]",
                is_present=True,
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_domain_inferred_and_legacy_source_migrated(self):
        service = EmailLoginMappingService(self.settings, self.db)
        self.assertEqual(service.resolve_source_domain(), "org.com")
        row = self.db.query(HRSourceRecord).one()
        self.assertEqual(row.source_id, "org.com")

    def test_xlsx_two_columns(self):
        wb=Workbook()
        ws=wb.active
        ws.append(["Сопоставление e-mail и логина"])
        ws.append(["e-mail", "логин"])
        ws.append(["boss@org.com", "ivanov.ii"])
        data=io.BytesIO()
        wb.save(data)
        wb.close()
        rows=EmailLoginMappingService.parse_xlsx(data.getvalue())
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].email,"boss@org.com")
        self.assertEqual(rows[0].login,"ivanov.ii")

    @patch("app.services.email_login_mapping.ZimbraService")
    @patch("app.services.email_login_mapping.ActiveDirectoryService")
    def test_manual_mapping_stores_stable_ids(self, ad_cls, z_cls):
        ad_cls.return_value.get_user.return_value = ADDirectoryUser(
            "ivanov.ii",
            object_guid="11111111-1111-1111-1111-111111111111",
        )
        z_cls.return_value.account_by_address.return_value = ZimbraAccountIdentity(
            "zimbra-123",
            "boss@org.com",
            "boss",
            ("boss@org.com",),
        )
        service=EmailLoginMappingService(self.settings,self.db)
        result=service.add_manual("boss@org.com","ivanov.ii","admin")
        self.assertEqual(result["status"],"created")
        mapping=self.db.query(EmailLoginMapping).one()
        self.assertEqual(mapping.ad_object_guid,"11111111-1111-1111-1111-111111111111")
        self.assertEqual(mapping.zimbra_id,"zimbra-123")


if __name__ == "__main__":
    unittest.main()
