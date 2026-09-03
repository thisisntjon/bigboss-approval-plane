import tempfile
import unittest
from pathlib import Path

from bigboss.pairing import build_claim_url, issue_pair_code, render_pair_page
from bigboss.store import Store


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_build_claim_url(self):
        self.assertEqual(
            build_claim_url("192.0.2.10:8787", "ABCD-1234"),
            "http://192.0.2.10:8787/?pair=ABCD-1234",
        )

    def test_issue_pair_code_includes_claim_url(self):
        payload = issue_pair_code(
            self.store,
            device_name="Test Phone",
            ttl_seconds=600,
            public_host="192.0.2.10:8787",
        )
        self.assertIn("code", payload)
        self.assertIn("claim_url", payload)
        self.assertIn(payload["code"], payload["claim_url"])

    def test_render_pair_page_includes_claim_url_and_code(self):
        payload = issue_pair_code(
            self.store,
            device_name="Test Phone",
            public_host="192.0.2.10:8787",
        )
        html_page = render_pair_page(payload)
        self.assertIn(payload["claim_url"], html_page)
        self.assertIn(payload["code"], html_page)
        self.assertIn("/static/qrcode.js", html_page)


if __name__ == "__main__":
    unittest.main()
