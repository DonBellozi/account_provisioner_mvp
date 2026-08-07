import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Runtime connector stubs.
ad_stub = types.ModuleType("app.services.ad")
class ActiveDirectoryService:
    pass
class ADDirectoryUser:
    def __init__(
        self,
        username,
        display_name="",
        email="",
        distinguished_name="",
        is_enabled=True,
        object_guid="",
    ):
        self.username = username
        self.display_name = display_name
        self.email = email
        self.distinguished_name = distinguished_name
        self.is_enabled = is_enabled
        self.object_guid = object_guid
ad_stub.ActiveDirectoryService = ActiveDirectoryService
ad_stub.ADDirectoryUser = ADDirectoryUser
sys.modules["app.services.ad"] = ad_stub

z_stub = types.ModuleType("app.services.zimbra")
class ZimbraService:
    pass
class ZimbraAccountIdentity:
    def __init__(self, zimbra_id, primary_email, login, addresses):
        self.zimbra_id = zimbra_id
        self.primary_email = primary_email
        self.login = login
        self.addresses = tuple(addresses)
z_stub.ZimbraService = ZimbraService
z_stub.ZimbraAccountIdentity = ZimbraAccountIdentity
sys.modules["app.services.zimbra"] = z_stub

from app.db import Base
from app.models import EmailLoginMapping, HRSourceRecord
from app.services.hr_registry import HRRegistryService


class RegistryMappingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.settings = SimpleNamespace(
            zimbra_domains=["org.com"],
            onec_source_domain="",
            ad_check_enabled=True,
            zimbra_check_enabled=True,
        )
        self.db.add(
            HRSourceRecord(
                worker_key="a" * 64,
                source_id="org.com",
                source_name="org.com",
                fio="Иванов Иван Иванович",
                corporate_email="boss@org.com",
                login="boss",
                placements_json="[]",
                is_present=True,
            )
        )
        self.db.add(
            EmailLoginMapping(
                worker_key="a" * 64,
                source_domain="org.com",
                source_email="boss@org.com",
                ad_object_guid="11111111-1111-1111-1111-111111111111",
                ad_login="ivanov.ii",
                zimbra_id="z1",
                zimbra_email="boss@org.com",
                created_by="admin",
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("app.services.hr_registry.ZimbraService")
    @patch("app.services.hr_registry.ActiveDirectoryService")
    def test_mapping_removed_when_real_logins_become_equal(self, ad_cls, z_cls):
        ad_cls.return_value.users_by_object_guids.return_value = {
            "11111111-1111-1111-1111-111111111111": SimpleNamespace(
                username="ivanov.ii",
                is_enabled=True,
            )
        }
        z_cls.return_value.accounts_by_ids.return_value = {
            "z1": SimpleNamespace(
                zimbra_id="z1",
                primary_email="ivanov.ii@org.com",
                login="ivanov.ii",
                addresses=("boss@org.com", "ivanov.ii@org.com"),
            )
        }

        service = HRRegistryService(self.settings, self.db)
        summary = service.reconcile_current()

        self.assertEqual(summary["ok"], 1)
        self.assertEqual(self.db.query(EmailLoginMapping).count(), 0)


if __name__ == "__main__":
    unittest.main()
