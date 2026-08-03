import unittest

from app.services.names import (
    build_login_candidates,
    parse_two_line_input,
    transliterate,
    validate_person_name,
)


class NameTests(unittest.TestCase):
    def test_parse_two_lines(self):
        person = parse_two_line_input("Иванов Иван Иванович\nivan.personal@example.com")
        self.assertEqual(person.last_name, "Иванов")
        self.assertEqual(person.first_name, "Иван")
        self.assertEqual(person.middle_name, "Иванович")
        self.assertEqual(person.personal_email, "ivan.personal@example.com")

    def test_parse_reverse_order(self):
        person = parse_two_line_input("ivan.personal@example.com\nИванов Иван")
        self.assertEqual(person.last_name, "Иванов")
        self.assertEqual(person.middle_name, "")

    def test_transliteration(self):
        self.assertEqual(transliterate("Ёлкин"), "elkin")
        self.assertEqual(transliterate("Щукин"), "shchukin")

    def test_standard_login_order(self):
        candidates = build_login_candidates("Иванов", "Иван", "Иванович")
        self.assertEqual(candidates[:3], ["ivanov.ii", "ivanov.i", "ivanov"])
        self.assertTrue(all(len(login) <= 20 for login in candidates))

    def test_patronymic_transliteration_expands_y_to_yu(self):
        candidates = build_login_candidates("Иванов", "Иван", "Юрьевич")
        self.assertEqual(candidates[:4], ["ivanov.iy", "ivanov.i", "ivanov", "ivanov.iyu"])

    def test_rejects_latin_lookalike_in_russian_name(self):
        with self.assertRaisesRegex(ValueError, "не из русской раскладки"):
            validate_person_name("Иванoв", "Иван", "Иванович")  # Latin o

    def test_allows_hyphenated_russian_name(self):
        last, first, middle = validate_person_name("Петров-Сидоров", "Анна-Мария", "")
        self.assertEqual(last, "Петров-Сидоров")
        self.assertEqual(first, "Анна-Мария")
        self.assertEqual(middle, "")


if __name__ == "__main__":
    unittest.main()
