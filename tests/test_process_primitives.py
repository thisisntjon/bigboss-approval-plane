"""Regression tests for the process liveness/termination primitives.

These guard the Windows os.kill footgun: os.kill(pid, 0) on Windows is not a
signal probe — it maps to OpenProcess(PROCESS_ALL_ACCESS)+TerminateProcess, so
the old pid_is_alive/terminate_pid either reported a live server as dead (the
dashboard port-reclaim blocker) or killed the target outright. The fixed
primitives must give the correct answer on this platform.
"""
import os
import sys
import subprocess
import time
import unittest

from bigboss.ops import pid_is_alive, terminate_pid


class PidIsAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(pid_is_alive(os.getpid()))

    def test_nonpositive_pids_are_dead(self):
        self.assertFalse(pid_is_alive(0))
        self.assertFalse(pid_is_alive(-1))

    def test_probe_does_not_kill(self):
        """The bug's nastiest edge: liveness probing must never terminate."""
        child = _spawn_sleeper()
        try:
            time.sleep(0.3)
            # Probe several times; a broken probe (os.kill on Windows) would kill it.
            for _ in range(5):
                self.assertTrue(pid_is_alive(child.pid))
            self.assertIsNone(child.poll(), "probe terminated the child process")
        finally:
            _hard_kill(child)


class TerminatePidTests(unittest.TestCase):
    def test_terminate_actually_kills(self):
        child = _spawn_sleeper()
        try:
            time.sleep(0.3)
            self.assertTrue(pid_is_alive(child.pid))
            self.assertTrue(terminate_pid(child.pid), "terminate_pid reported failure")
            self.assertFalse(pid_is_alive(child.pid), "process survived terminate_pid")
        finally:
            _hard_kill(child)

    def test_terminate_dead_pid_is_true(self):
        child = _spawn_sleeper()
        _hard_kill(child)
        time.sleep(0.2)
        # Already dead => terminate is a no-op success.
        self.assertTrue(terminate_pid(child.pid))


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _hard_kill(child: subprocess.Popen) -> None:
    try:
        child.kill()
        child.wait(timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main()
