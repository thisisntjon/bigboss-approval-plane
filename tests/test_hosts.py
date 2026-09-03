import socket
import unittest

from bigboss.ops import assert_serve_port_available
from bigboss.hosts import local_ip_hint, get_mdns_hostname


class HostsTests(unittest.TestCase):
    def test_assert_serve_port_available_rejects_busy_port(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            with self.assertRaises(SystemExit):
                assert_serve_port_available("127.0.0.1", port)
        finally:
            holder.close()

    def test_local_ip_hint(self):
        ip = local_ip_hint()
        self.assertIsNotNone(ip)
        self.assertNotEqual(ip, "")

    def test_get_mdns_hostname(self):
        hostname = get_mdns_hostname()
        self.assertIsNotNone(hostname)
        self.assertTrue(hostname.endswith(".local") or hostname == "localhost")


if __name__ == "__main__":
    unittest.main()

