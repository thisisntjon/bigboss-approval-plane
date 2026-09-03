"""P-ops process management — orphan detection logic + safety invariants (pure, no OS calls)."""

import os
import unittest

from bigboss import procman
from bigboss.procman import ProcInfo


def _p(pid, ppid, name="python.exe", ct=100, exe=""):
    return ProcInfo(pid=pid, ppid=ppid, name=name, create_time=ct, exe=exe)


class OrphanDetectionTests(unittest.TestCase):
    def test_dead_parent_is_orphan(self):
        # 200's parent 999 is not in the table -> dead -> orphan; 100 has a live parent (root)
        procs = [_p(1, 0, name="init", ct=1), _p(100, 1), _p(200, 999)]
        orphans = {p.pid for p in procman.find_orphans(procs)}
        self.assertEqual(orphans, {200})

    def test_live_parent_is_not_orphan(self):
        procs = [_p(100, 1, name="explorer.exe"), _p(200, 100)]  # parent 100 alive, older
        orphans = procman.find_orphans(procs)
        self.assertEqual(orphans, [])

    def test_pid_reuse_is_orphan(self):
        # parent 100 exists but started AFTER child 200 -> recycled pid -> orphan
        procs = [_p(1, 0, name="init", ct=1), _p(100, 1, ct=500), _p(200, 100, ct=100)]
        orphans = {p.pid for p in procman.find_orphans(procs)}
        self.assertEqual(orphans, {200})

    def test_only_allowlisted_images_are_reapable(self):
        procs = [_p(200, 999, name="svchost.exe"), _p(201, 999, name="python.exe")]
        orphans = {p.pid for p in procman.find_orphans(procs)}
        self.assertEqual(orphans, {201})  # svchost never reaped even though orphaned

    def test_protected_pids_never_orphaned(self):
        procs = [_p(200, 999)]
        orphans = procman.find_orphans(procs, protect_pids={200})
        self.assertEqual(orphans, [])

    def test_current_protect_includes_self_and_ancestors(self):
        me = os.getpid()
        procs = [_p(me, 4242), _p(4242, 1, name="bash.exe"), _p(1, 0, name="init")]
        protect = procman.current_protect_pids(procs)
        self.assertIn(me, protect)
        self.assertIn(4242, protect)  # our shell/harness ancestor is protected


class GroupingTests(unittest.TestCase):
    def test_role_key_from_venv_path(self):
        p = _p(1, 0, exe="C:/dev/sampleapp/videowatcher/.venv/Scripts/python.exe")
        self.assertEqual(procman._role_key(p), "videowatcher")

    def test_group_clusters_duplicates(self):
        procs = [
            _p(1, 0, exe="D:/brainz/.venv/Scripts/python.exe"),
            _p(2, 0, exe="D:/brainz/.venv/Scripts/python.exe"),
            _p(3, 0, exe="C:/x/tool/.venv/Scripts/python.exe"),
        ]
        groups = procman.group_by_role(procs)
        self.assertEqual(len(groups["brainz"]), 2)
        self.assertEqual(len(groups["tool"]), 1)


class ReapTests(unittest.TestCase):
    def test_dry_run_touches_nothing(self):
        results = procman.reap([_p(999999, 1)], dry_run=True)
        self.assertEqual(results[0]["reaped"], None)  # None = not attempted


if __name__ == "__main__":
    unittest.main()
