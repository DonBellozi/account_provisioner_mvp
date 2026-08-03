import unittest

from app.services.names import build_login_candidates, parse_two_line_input, transliterate


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

    def test_standard_login_first(self):
        candidates = build_login_candidates("Иванов", "Иван", "Иванович")
        self.assertEqual(candidates[0], "ivanov.ii")
        self.assertLessEqual(len(candidates[0]), 20)


if __name__ == "__main__":
    unittest.main()
