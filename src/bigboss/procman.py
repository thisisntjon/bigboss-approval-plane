"""Process management (P-ops) — see what's running across the ecosystem, tell live from
orphaned, and reap safely. Stdlib only: ctypes Toolhelp on Windows, `/proc` on Linux.

The motivating problem: MCP servers and dev daemons accumulate across harness sessions
(local-memory, videowatcher, bigboss router/squire-proxy/mcp_dev, ...) and never get reaped,
plus duplicates fight over ports (e.g. two on :8765). BigBoss should inventory them, flag
duplicates + port conflicts, and reap only TRUE ORPHANS (parent process gone) — never a
server still owned by a live session.

Safety invariants:
- Reap only orphans (dead/recycled parent) whose image is in a reap allowlist (never a random
  or system process).
- Never reap self or any of our own ancestors.
- Default to dry-run; killing requires an explicit opt-in.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .ops import pid_is_alive, terminate_pid

# Images we are willing to reap when orphaned — server hosts, never system binaries.
REAP_ALLOWLIST = {"python.exe", "python", "node.exe", "node", "pythonw.exe"}


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    name: str          # image basename, e.g. "python.exe"
    create_time: int    # comparable start stamp (0 if unknown)
    exe: str = ""       # full path if resolvable (helps identify role by venv)


# ---------------------------------------------------------------- enumeration

def list_processes() -> list[ProcInfo]:
    if sys.platform == "win32":
        return _list_windows()
    return _list_proc()


def _list_windows() -> list[ProcInfo]:
    import ctypes
    from ctypes import wintypes as w

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", w.DWORD),
            ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == ctypes.c_void_p(-1).value:
        return []
    out: list[ProcInfo] = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not k32.Process32First(snap, ctypes.byref(entry)):
            return []
        while True:
            pid = int(entry.th32ProcessID)
            name = entry.szExeFile.decode("utf-8", "replace")
            ct, exe = _win_proc_detail(k32, ctypes, w, pid, PROCESS_QUERY_LIMITED_INFORMATION)
            out.append(ProcInfo(pid=pid, ppid=int(entry.th32ParentProcessID), name=name,
                                create_time=ct, exe=exe))
            if not k32.Process32Next(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


def _win_proc_detail(k32, ctypes, w, pid: int, access: int) -> tuple[int, str]:
    """Best-effort (create_time, exe_path); (0, "") when access is denied (system procs)."""
    h = k32.OpenProcess(access, False, pid)
    if not h:
        return 0, ""
    try:
        creation = w.FILETIME()
        exit_t = w.FILETIME()
        kern = w.FILETIME()
        user = w.FILETIME()
        ct = 0
        if k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                               ctypes.byref(kern), ctypes.byref(user)):
            ct = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        exe = ""
        size = w.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            exe = buf.value
        return ct, exe
    finally:
        k32.CloseHandle(h)


def _list_proc() -> list[ProcInfo]:
    """Linux/POSIX via /proc (used mainly by the remote host/the guarded box side + tests on CI)."""
    procd = Path("/proc")
    if not procd.exists():
        return []
    out: list[ProcInfo] = []
    for d in procd.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            # comm is parenthesized and may contain spaces/parens — split on the last ')'
            rp = stat.rindex(")")
            after = stat[rp + 2:].split()
            comm = stat[stat.index("(") + 1:rp]
            ppid = int(after[1])          # field 4 (ppid) is index 1 after state
            starttime = int(after[19])     # field 22 (starttime) is index 19 after state
        except (OSError, ValueError, IndexError):
            continue
        exe = ""
        try:
            exe = os.readlink(d / "exe")
        except OSError:
            pass
        out.append(ProcInfo(pid=int(d.name), ppid=ppid, name=comm, create_time=starttime, exe=exe))
    return out


# ---------------------------------------------------------------- analysis

def _ancestors_of(pid: int, by_pid: dict[int, ProcInfo]) -> set[int]:
    seen: set[int] = set()
    cur = pid
    while cur in by_pid and cur not in seen:
        seen.add(cur)
        cur = by_pid[cur].ppid
    return seen


def find_orphans(procs: list[ProcInfo], *, allowlist: set[str] | None = None,
                 protect_pids: set[int] | None = None) -> list[ProcInfo]:
    """An orphan's parent is gone: either absent from the table (dead) or a recycled PID
    whose current holder started AFTER the child (create_time guard). Restricted to the
    reap allowlist and never includes protected pids (self + ancestors)."""
    allow = allowlist if allowlist is not None else REAP_ALLOWLIST
    protect = protect_pids or set()
    by_pid = {p.pid: p for p in procs}
    orphans: list[ProcInfo] = []
    for p in procs:
        if p.pid in protect or p.name not in allow:
            continue
        parent = by_pid.get(p.ppid)
        is_orphan = parent is None
        if parent is not None and parent.create_time and p.create_time and parent.create_time > p.create_time:
            is_orphan = True  # PID reuse — the current holder of ppid is a newer, different process
        if is_orphan:
            orphans.append(p)
    return orphans


def group_by_role(procs: list[ProcInfo]) -> dict[str, list[ProcInfo]]:
    """Group processes by a rough 'role' key derived from the exe path (venv/module dir),
    so duplicate MCP servers cluster. Falls back to image name."""
    groups: dict[str, list[ProcInfo]] = {}
    for p in procs:
        groups.setdefault(_role_key(p), []).append(p)
    return groups


def _role_key(p: ProcInfo) -> str:
    exe = (p.exe or "").replace("\\", "/").lower()
    # the parent dir of a venv python is usually the project/tool name
    for marker in ("/.venv/", "/scripts/", "/bin/"):
        if marker in exe:
            head = exe.split(marker)[0]
            return head.rsplit("/", 1)[-1] or p.name
    return p.name


def current_protect_pids(procs: list[ProcInfo]) -> set[int]:
    """Never reap ourselves or our ancestor chain (the shell/harness that launched us)."""
    by_pid = {p.pid: p for p in procs}
    return _ancestors_of(os.getpid(), by_pid) | {os.getpid()}


def reap(orphans: list[ProcInfo], *, dry_run: bool = True) -> list[dict]:
    """Terminate orphaned processes (SIGTERM→grace→SIGKILL via ops.terminate_pid).
    dry_run reports what WOULD be reaped without touching anything."""
    results: list[dict] = []
    for p in orphans:
        if dry_run:
            results.append({"pid": p.pid, "name": p.name, "role": _role_key(p), "reaped": None})
            continue
        ok = terminate_pid(p.pid) if pid_is_alive(p.pid) else True
        results.append({"pid": p.pid, "name": p.name, "role": _role_key(p), "reaped": bool(ok)})
    return results
