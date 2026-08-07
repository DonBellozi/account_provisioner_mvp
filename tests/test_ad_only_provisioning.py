import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Runtime connector stubs: isolated tests do not require ldap3/paramiko.
ad_stub = types.ModuleType("app.services.ad")
class ActiveDirectoryService:
    def __init__(self, settings):
        self.settings = settings
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
    def __init__(self, settings):
        self.settings = settings
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
from app.models import ADProvisioningOperation, AuditLog, HRSourceRecord
from app.services.provisioning import ProvisioningService


class FakeAD:
    def __init__(self, *, name_candidates=None, email_matches=None):
        self.created = False
        self.enabled = False
        self.name_candidates = list(name_candidates or [])
        self.email_matches = list(email_matches or [])
        self.created_login = ""
        self.created_email = ""

    def get_user(self, login):
        for user in [*self.name_candidates, *self.email_matches]:
            if user.username.casefold() == str(login).casefold():
                return user
        if not self.created:
            return None
        return ADDirectoryUser(
            login,
            display_name="Абакумова Алла Владимировна",
            email=self.created_email,
            distinguished_name="CN=test,OU=users,DC=local,DC=dmn",
            is_enabled=self.enabled,
            object_guid="11111111-1111-1111-1111-111111111111",
        )

    def users_by_email(self, email, limit=10):
        return self.email_matches[:limit]

    def search_users(self, query, limit=50):
        q = str(query or "").casefold()
        result = []
        for user in self.name_candidates:
            haystack = " ".join(
                [user.username, user.display_name, user.email]
            ).casefold()
            if q in haystack:
                result.append(user)
        return result[:limit]

    def create_disabled_user(
        self,
        login,
        password_candidates,
        last_name,
        first_name,
        middle_name,
        corporate_email,
    ):
        self.created = True
        self.created_login = login
        self.created_email = corporate_email
        return SimpleNamespace(
            dn="CN=Абакумова Алла Владимировна,OU=users,DC=local,DC=dmn",
            login=login,
            upn=f"{login}@local.dmn",
            accepted_password=password_candidates[0],
        )

    def enable_user(self, dn):
        self.enabled = True


class FakeZimbra:
    def __init__(self, email, login=None):
        self.email = email
        self.login = login or email.split("@", 1)[0]
        self.lookup_count = 0

    def account_by_address(self, email):
        self.lookup_count += 1
        if email != self.email:
            return None
        return ZimbraAccountIdentity(
            "zimbra-1",
            self.email,
            self.login,
            (self.email,),
        )


class FakeMailer:
    def __init__(self):
        self.calls = []

    def send_ad_credentials(
        self,
        profile,
        corporate_email,
        full_name,
        ad_login,
        ad_password,
    ):
        self.calls.append(
            {
                "corporate_email": corporate_email,
                "full_name": full_name,
                "ad_login": ad_login,
                "ad_password": ad_password,
            }
        )


class ADOnlyProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.settings = SimpleNamespace(
            dry_run=False,
            zimbra_domains=["org.com"],
            zimbra_primary_domain="org.com",
            ad_password_min_length=8,
            ad_password_max_length=12,
            ad_password_specials="!@#$%&?",
        )
        self.record = HRSourceRecord(
            worker_key="a" * 64,
            source_id="org.com",
            source_name="org.com",
            fio="Абакумова Алла Владимировна",
            corporate_email="abacumova.av@org.com",
            login="abacumova.av",
            placements_json="[]",
            is_present=True,
            ad_status="missing",
            zimbra_status="present",
            reconciliation_status="issue",
        )
        self.db.add(self.record)
        self.db.commit()
        self.db.refresh(self.record)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def service(self, *, name_candidates=None, email_matches=None):
        service = ProvisioningService(self.settings)
        service.ad = FakeAD(
            name_candidates=name_candidates,
            email_matches=email_matches,
        )
        service.zimbra = FakeZimbra("abacumova.av@org.com")
        service.mailer = FakeMailer()
        return service

    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_login_is_taken_exactly_from_existing_email(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        service = self.service()

        preflight = service.prepare_ad_for_existing_mailbox(
            self.db,
            self.record.id,
        )

        self.assertEqual(preflight.login, "abacumova.av")
        self.assertTrue(preflight.can_create)

    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_name_search_handles_reversed_order_and_initial(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        candidate = ADDirectoryUser(
            "legacy.user",
            display_name="Алла В. Абакумова",
            email="",
            distinguished_name="CN=legacy",
            is_enabled=True,
            object_guid="22222222-2222-2222-2222-222222222222",
        )
        service = self.service(name_candidates=[candidate])

        preflight = service.prepare_ad_for_existing_mailbox(
            self.db,
            self.record.id,
        )

        self.assertEqual(len(preflight.name_candidates), 1)
        self.assertEqual(
            preflight.name_candidates[0].username,
            "legacy.user",
        )
        self.assertIn("ad_login=legacy.user", preflight.name_candidates[0].mapping_url)

    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_exact_ad_email_match_blocks_creation(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        existing = ADDirectoryUser(
            "legacy.login",
            display_name="Алла Владимировна Абакумова",
            email="abacumova.av@org.com",
            distinguished_name="CN=legacy",
            is_enabled=True,
            object_guid="33333333-3333-3333-3333-333333333333",
        )
        service = self.service(email_matches=[existing])

        preflight = service.prepare_ad_for_existing_mailbox(
            self.db,
            self.record.id,
        )

        self.assertFalse(preflight.can_create)
        self.assertEqual(len(preflight.exact_matches), 1)
        self.assertEqual(preflight.exact_matches[0].username, "legacy.login")

    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_name_candidate_requires_explicit_confirmation(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        candidate = ADDirectoryUser(
            "legacy.user",
            display_name="Абакумова Алла В.",
            email="",
            distinguished_name="CN=legacy",
            is_enabled=True,
            object_guid="44444444-4444-4444-4444-444444444444",
        )
        service = self.service(name_candidates=[candidate])

        with self.assertRaisesRegex(RuntimeError, "Подтвердите"):
            service.provision_ad_for_existing_mailbox(
                self.db,
                "admin",
                self.record.id,
                confirm_name_candidates=False,
            )

    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_ad_only_creation_uses_existing_mail_and_does_not_store_password(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        service = self.service()

        credentials = service.provision_ad_for_existing_mailbox(
            self.db,
            "admin",
            self.record.id,
        )

        self.assertTrue(credentials.ad_created)
        self.assertTrue(credentials.ad_enabled)
        self.assertTrue(credentials.credentials_mail_sent)
        self.assertTrue(credentials.registry_updated)
        self.assertEqual(credentials.ad_login, "abacumova.av")
        self.assertEqual(credentials.corporate_email, "abacumova.av@org.com")
        self.assertTrue(credentials.ad_password)

        self.assertEqual(len(service.mailer.calls), 1)
        sent = service.mailer.calls[0]
        self.assertEqual(sent["corporate_email"], "abacumova.av@org.com")
        self.assertEqual(sent["ad_login"], "abacumova.av")

        operation = self.db.query(ADProvisioningOperation).one()
        audit = self.db.query(AuditLog).filter_by(
            action="provision_ad_existing_mailbox"
        ).one()
        stored = " ".join(
            [
                operation.error_message or "",
                audit.details or "",
                audit.target or "",
            ]
        )
        self.assertNotIn(credentials.ad_password, stored)

        self.db.refresh(self.record)
        self.assertEqual(self.record.ad_status, "enabled")
        self.assertEqual(self.record.zimbra_status, "present")
        self.assertEqual(self.record.reconciliation_status, "ok")


    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_name_candidate_can_be_found_after_surname_change(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        candidate = ADDirectoryUser(
            "petrova.av",
            display_name="Петрова Алла Владимировна",
            email="",
            distinguished_name="CN=legacy",
            is_enabled=True,
            object_guid="55555555-5555-5555-5555-555555555555",
        )
        service = self.service(name_candidates=[candidate])

        preflight = service.prepare_ad_for_existing_mailbox(
            self.db,
            self.record.id,
        )

        self.assertEqual(len(preflight.name_candidates), 1)
        self.assertEqual(preflight.name_candidates[0].username, "petrova.av")

    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_confirm_candidate_marks_registry_ok(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        candidate = ADDirectoryUser(
            "petrova.av",
            display_name="Петрова Алла Владимировна",
            email="",
            distinguished_name="CN=legacy",
            is_enabled=True,
            object_guid="66666666-6666-6666-6666-666666666666",
        )
        service = self.service(name_candidates=[candidate])

        with patch(
            "app.services.provisioning.EmailLoginMappingService.save_confirmed_identity",
            return_value={
                "status": "created",
                "mapping_id": 1,
                "fio": self.record.fio,
                "email": self.record.corporate_email,
                "ad_login": candidate.username,
                "zimbra_login": "abacumova.av",
            },
        ) as save_mapping:
            result = service.confirm_ad_candidate(
                self.db,
                "admin",
                self.record.id,
                candidate.username,
            )

        self.assertEqual(result["ad_login"], "petrova.av")
        self.assertEqual(result["reconciliation_status"], "ok")
        save_mapping.assert_called_once()
        self.db.refresh(self.record)
        self.assertEqual(self.record.ad_status, "enabled")
        self.assertEqual(self.record.zimbra_status, "present")
        self.assertEqual(self.record.reconciliation_status, "ok")

    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_confirm_rejects_unknown_candidate(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        service = self.service()

        with self.assertRaisesRegex(ValueError, "не входит в найденные кандидаты"):
            service.confirm_ad_candidate(
                self.db,
                "admin",
                self.record.id,
                "someone.else",
            )



    @patch("app.services.provisioning.get_domain_mail_profile")
    def test_second_ad_creation_request_is_rejected_while_first_is_running(self, profile):
        profile.return_value = SimpleNamespace(domain="org.com")
        service = self.service()

        preflight = service.prepare_ad_for_existing_mailbox(
            self.db,
            self.record.id,
        )
        lock = service._ad_only_lock_for_login(preflight.login)
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "уже выполняется",
            ):
                service.provision_ad_for_existing_mailbox(
                    self.db,
                    "admin",
                    self.record.id,
                )
        finally:
            service._release_ad_only_lock(preflight.login, lock)

    def test_ad_only_lock_is_shared_between_service_instances(self):
        first = self.service()
        second = self.service()
        lock1 = first._ad_only_lock_for_login("Abacumova.AV")
        lock2 = second._ad_only_lock_for_login("abacumova.av")
        self.assertIs(lock1, lock2)



if __name__ == "__main__":
    unittest.main()
