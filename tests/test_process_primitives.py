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
        pid = _spawn_detached_sleeper()
        try:
            time.sleep(0.3)
            self.assertTrue(pid_is_alive(pid))
            self.assertTrue(terminate_pid(pid), "terminate_pid reported failure")
            self.assertFalse(pid_is_alive(pid), "process survived terminate_pid")
        finally:
            _hard_kill_pid(pid)

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


def _spawn_detached_sleeper() -> int:
    """Start a sleeper this test process does not parent, and return its pid.

    Production never parents what it terminates: procman.reap and
    store._apply_reap both act on pids found by scanning for orphans. The
    distinction matters on POSIX. A direct Popen child that receives SIGTERM
    becomes a zombie until its parent reaps it, and a zombie still answers
    os.kill(pid, 0), so terminate_pid would report failure for a process it
    really did kill. Spawning through a launcher that exits immediately leaves
    the sleeper parented by init, which reaps it, matching what the product
    actually operates on. On Windows there are no zombies and parentage does
    not matter, so the same helper works there.
    """
    launcher = (
        "import subprocess, sys;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
        "sys.stdout.write(str(p.pid))"
    )
    done = subprocess.run(
        [sys.executable, "-c", launcher], capture_output=True, text=True, timeout=30
    )
    return int(done.stdout.strip())


def _hard_kill_pid(pid: int) -> None:
    try:
        terminate_pid(pid)
    except Exception:
        pass


def _hard_kill(child: subprocess.Popen) -> None:
    try:
        child.kill()
        child.wait(timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main()
