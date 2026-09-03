import unittest
import time
from bigboss.ops import is_port_blocked, is_admin, IPChangeMonitor


class OpsNewTests(unittest.TestCase):
    def test_is_admin(self):
        admin = is_admin()
        self.assertIsInstance(admin, bool)

    def test_is_port_blocked_does_not_crash(self):
        blocked = is_port_blocked(9999)
        self.assertIsInstance(blocked, bool)

    def test_ip_change_monitor(self):
        changes = []
        def callback(old, new):
            changes.append((old, new))

        monitor = IPChangeMonitor(callback, interval_seconds=0.05)
        # Force last_ip to be different from the actual ip to simulate a change
        from bigboss.hosts import local_ip_hint
        real_ip = local_ip_hint()
        monitor.last_ip = "198.51.100.99" if real_ip != "198.51.100.99" else "198.51.100.98"
        
        monitor.start()
        time.sleep(0.2)
        monitor.stop()
        monitor.join(timeout=1.0)
        
        self.assertTrue(len(changes) >= 1)
        expected_old_ip = "198.51.100.99" if real_ip != "198.51.100.99" else "198.51.100.98"
        self.assertEqual(changes[0][0], expected_old_ip)
        self.assertEqual(changes[0][1], real_ip)

    def test_ssh_tunnel_manager_extracts_url(self):
        from unittest.mock import MagicMock, patch
        from bigboss.ops import SSHTunnelManager

        urls = []
        def on_ready(url):
            urls.append(url)

        manager = SSHTunnelManager(local_port=8787, on_url_ready=on_ready)
        
        # Mock subprocess.Popen and its stdout
        mock_proc = MagicMock()
        mock_proc.stdout = [
            '{"address":"abc-123.localhost.run", "port":80}\n'
        ]
        
        with patch("subprocess.Popen", return_value=mock_proc):
            with patch.object(manager, "display_terminal_qr") as mock_qr:
                manager.start()
                # Wait for the read thread to process the stdout mock list
                time.sleep(0.3)
                manager.stop()
                
        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0], "https://abc-123.localhost.run")


if __name__ == "__main__":
    unittest.main()
