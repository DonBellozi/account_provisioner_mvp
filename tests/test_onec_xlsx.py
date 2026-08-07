import tempfile, unittest
from pathlib import Path
from openpyxl import Workbook
from app.services.onec_xlsx import parse_onec_xlsx, worker_snapshot
class OneCXlsxTests(unittest.TestCase):
    def make(self,state):
        wb=Workbook();ws=wb.active;ws.append(["Отчет"]);ws.append(["СНИЛС","Сотрудник.Физическое лицо.ФИО","Физическое лицо.Адрес электронной почты","Должность","Состояние"]);ws.append(["Отдел ИТ"]);ws.append(["123-456-789 01","Иванов Иван Иванович","ivanov.ii@example.ru","Специалист",state]);f=tempfile.NamedTemporaryFile(suffix=".xlsx",delete=False);p=Path(f.name);f.close();wb.save(p);wb.close();return p
    def test_state_ignored(self):
        a=self.make("Работа");b=self.make("Отпуск")
        try: ra=parse_onec_xlsx(a,hash_secret="0123456789abcdef");rb=parse_onec_xlsx(b,hash_secret="0123456789abcdef")
        finally:a.unlink(missing_ok=True);b.unlink(missing_ok=True)
        self.assertNotIn("state",ra.detected_columns);self.assertEqual(worker_snapshot(ra.workers[0]),worker_snapshot(rb.workers[0]));self.assertNotEqual(ra.workers[0].worker_key,"12345678901")
if __name__=="__main__":unittest.main()
