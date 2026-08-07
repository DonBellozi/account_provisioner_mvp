import sys, types, unittest
from types import SimpleNamespace
from unittest.mock import patch
# Stubs prevent importing optional runtime connector libs in this isolated test.
ad_stub=types.ModuleType("app.services.ad");ad_stub.ActiveDirectoryService=object;sys.modules["app.services.ad"]=ad_stub
z_stub=types.ModuleType("app.services.zimbra");z_stub.ZimbraService=object;sys.modules["app.services.zimbra"]=z_stub
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.services.hr_registry import HRRegistryService
from app.services.onec_xlsx import OneCPlacement, OneCWorkbook, OneCWorker
class HRRegistryTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine("sqlite:///:memory:");Base.metadata.create_all(self.engine);self.db=sessionmaker(bind=self.engine)();self.settings=SimpleNamespace(onec_source_id="org_com",onec_source_name="Организация .com",ad_check_enabled=True,zimbra_check_enabled=True)
    def tearDown(self): self.db.close();self.engine.dispose()
    @patch("app.services.hr_registry.ZimbraService")
    @patch("app.services.hr_registry.ActiveDirectoryService")
    def test_sync(self,ad,z):
        ad.return_value.users_by_logins.return_value={"ivanov.ii":SimpleNamespace(is_enabled=True)};z.return_value.addresses_exist.return_value={"ivanov.ii@example.com"}
        book=OneCWorkbook(workers=(OneCWorker("a"*64,"Иванов Иван Иванович","ivanov.ii@example.com","ivanov.ii",(OneCPlacement("ИТ","Специалист"),)),OneCWorker("b"*64,"Петров Петр Петрович","petrov.pp@example.com","petrov.pp",(OneCPlacement("Проекты","Эксперт"),))),headers=("СНИЛС",),header_row=2,detected_columns={"snils":"СНИЛС"},potential_dismissal_columns=())
        svc=HRRegistryService(self.settings,self.db);s=svc.sync_and_reconcile(book)["reconciliation"];self.assertEqual((s["total"],s["ok"],s["issues"]),(2,1,1));self.assertEqual(len(svc.list_rows(status="issues")),1)
if __name__=="__main__":unittest.main()
