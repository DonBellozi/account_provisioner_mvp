import unittest

from app.config import Settings


class ConfigTests(unittest.TestCase):
    def test_domain_and_group_parsing(self):
        settings = Settings(
            _env_file=None,
            app_secret_key="1234567890abcdef",
            zimbra_domains="one.example,two.example",
            ad_default_group_dns=(
                "CN=Base Users,OU=Groups,DC=example,DC=local;"
                "CN=VPN Users,OU=Groups,DC=example,DC=local"
            ),
        )
        self.assertEqual(settings.zimbra_domains, ["one.example", "two.example"])
        self.assertEqual(len(settings.ad_default_group_dns), 2)
        self.assertTrue(settings.ad_default_group_dns[0].startswith("CN=Base Users"))


if __name__ == "__main__":
    unittest.main()
