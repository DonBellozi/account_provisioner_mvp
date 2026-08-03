import string
import unittest

from app.services.passwords import generate_ad_password, generate_mail_password


class PasswordTests(unittest.TestCase):
    def test_mail_password_rules(self):
        specials = "!@#$%&?"
        for _ in range(200):
            password = generate_mail_password(16, specials)
            self.assertEqual(len(password), 16)
            self.assertTrue(any(ch in string.ascii_uppercase for ch in password))
            self.assertTrue(any(ch in string.ascii_lowercase for ch in password))
            self.assertTrue(any(ch in string.digits for ch in password))
            self.assertTrue(any(ch in specials for ch in password))
            self.assertTrue(all(ch in string.ascii_letters + string.digits + specials for ch in password))

    def test_ad_password_rules(self):
        specials = "!@#$%&?"
        for _ in range(200):
            password = generate_ad_password("ivan", "ivanov", 8, 12, specials)
            self.assertGreaterEqual(len(password), 8)
            self.assertLessEqual(len(password), 12)
            self.assertTrue(any(ch.isupper() for ch in password))
            self.assertTrue(any(ch.islower() for ch in password))
            self.assertTrue(any(ch.isdigit() for ch in password))
            self.assertEqual(sum(ch in specials for ch in password), 1)

    def test_ad_password_uses_name_fragments(self):
        password = generate_ad_password("petr", "petrov", 8, 12, "!")
        lowered = password.lower()
        self.assertTrue("pet" in lowered or "pe" in lowered)


if __name__ == "__main__":
    unittest.main()
