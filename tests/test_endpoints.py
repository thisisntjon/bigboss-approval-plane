import os
import time
import unittest
from unittest import mock

from bigboss.intel.endpoints import (
    Endpoint,
    get_endpoint,
    list_endpoints,
    throttle,
)


class EndpointRegistryTests(unittest.TestCase):
    def test_defaults(self):
        names = {e.name for e in list_endpoints()}
        self.assertEqual(names, {"squire", "beastmode"})
        squire = get_endpoint("squire")
        beast = get_endpoint("beastmode")
        self.assertTrue(squire.auto_ok)
        self.assertEqual(squire.min_interval_s, 0.0)
        # Beastmode is the guarded 5090 box: opt-in only, rate-limited.
        self.assertFalse(beast.auto_ok)
        self.assertGreater(beast.min_interval_s, 0.0)
        self.assertIn("beastmode.local", beast.base_url)

    def test_case_insensitive_and_default(self):
        self.assertEqual(get_endpoint("BEASTMODE").name, "beastmode")
        self.assertEqual(get_endpoint(None).name, "squire")

    def test_unknown_endpoint_raises(self):
        with self.assertRaises(KeyError):
            get_endpoint("gpu-in-the-closet")

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {
            "BEASTMODE_BASE_URL": "http://198.51.100.9:9999/v1",
            "BEASTMODE_MODEL": "custom-model",
            "BEASTMODE_MIN_INTERVAL_S": "0",
        }):
            beast = get_endpoint("beastmode")
            self.assertEqual(beast.base_url, "http://198.51.100.9:9999/v1")
            self.assertEqual(beast.model, "custom-model")
            self.assertEqual(beast.min_interval_s, 0.0)

    def test_client_passes_api_key_from_env(self):
        with mock.patch.dict(os.environ, {"BEASTMODE_API_KEY": "secret-123"}):
            client = get_endpoint("beastmode").client()
            self.assertEqual(client.api_key, "secret-123")


class ThrottleTests(unittest.TestCase):
    def test_throttle_spaces_calls(self):
        ep = Endpoint(name="beastmode-test", base_url="http://x/v1", model="m",
                      auto_ok=False, min_interval_s=0.2)
        start = time.monotonic()
        throttle(ep)  # first is free
        throttle(ep)  # must wait ~0.2s
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.18)

    def test_no_throttle_when_interval_zero(self):
        ep = Endpoint(name="squire-test", base_url="http://x/v1", model="m",
                      auto_ok=True, min_interval_s=0.0)
        start = time.monotonic()
        for _ in range(5):
            throttle(ep)
        self.assertLess(time.monotonic() - start, 0.1)


if __name__ == "__main__":
    unittest.main()
