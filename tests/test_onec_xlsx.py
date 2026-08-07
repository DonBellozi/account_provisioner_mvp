import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.services.onec_xlsx import parse_onec_xlsx


class OneCXlsxTests(unittest.TestCase):
    def _book(self) -> Path:
        workbook = Workbook()
        sheet = workbook.active

        sheet.append(["Отчет"])
        sheet.append([
            "СНИЛС",
            "Сотрудник.Физическое лицо.ФИО",
            "Физическое лицо.Адрес электронной почты",
            "Должность",
            "Состояние",
            "Произвольное поле",
        ])

        sheet.append(["Отдел информационных технологий"])
        sheet.row_dimensions[3].outlineLevel = 0

        sheet.append([
            "123-456-789 01",
            "Иванов Иван Иванович",
            "ivanov.ii@example.ru",
            "Специалист",
            "Работа",
            "",
        ])

        sheet.append(["Отдел проектов"])
        sheet.row_dimensions[5].outlineLevel = 0

        sheet.append([
            "12345678901",
            "Иванов Иван Иванович",
            "ivanov.ii@example.ru",
            "Менеджер",
            "Работа",
            "",
        ])

        sheet.append([
            "111-222-333 44",
            "Петров Петр Петрович",
            "",
            "Эксперт",
            "Работа",
            "",
        ])

        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        path = Path(handle.name)
        handle.close()
        workbook.save(path)
        workbook.close()
        return path

    def test_multiple_placements_and_no_dismissal_field(self):
        path = self._book()
        try:
            result = parse_onec_xlsx(
                path,
                hash_secret="0123456789abcdef",
            )
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(len(result.workers), 2)
        self.assertEqual(result.placements_count, 3)
        self.assertEqual(result.multiple_placements_count, 1)
        self.assertEqual(result.missing_email_count, 1)
        self.assertEqual(result.states, ("Работа",))
        self.assertEqual(result.potential_dismissal_columns, ())
        self.assertEqual(result.header_row, 2)

        ivanov = next(
            worker
            for worker in result.workers
            if worker.fio == "Иванов Иван Иванович"
        )
        self.assertEqual(len(ivanov.placements), 2)
        self.assertNotEqual(
            ivanov.worker_key,
            "12345678901",
        )


if __name__ == "__main__":
    unittest.main()
