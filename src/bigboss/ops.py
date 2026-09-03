from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


_pin_failures: dict[str, list[float]] = defaultdict(list)


@dataclass
class ServeState:
    pid: int
    port: int
    host: str
    phone_url: str
    desk_url: str
    started_at: str

    @classmethod
    def from_json(cls, payload: dict) -> ServeState:
        return cls(
            pid=int(payload["pid"]),
            port=int(payload["port"]),
            host=str(payload["host"]),
            phone_url=str(payload["phone_url"]),
            desk_url=str(payload["desk_url"]),
            started_at=str(payload["started_at"]),
        )


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_is_alive(pid)
    # POSIX: signal 0 is a pure existence/permission probe (no signal delivered).
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _win_pid_is_alive(pid: int) -> bool:
    """Windows liveness probe. Never uses os.kill: on Windows os.kill(pid, 0)
    maps to OpenProcess(PROCESS_ALL_ACCESS)+TerminateProcess, which either fails
    to open (false 'dead') or actually kills the target. We open with the
    read-only PROCESS_QUERY_LIMITED_INFORMATION right and read the exit code."""
    import ctypes
    from ctypes import wintypes as w

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = w.HANDLE
        k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k32.GetExitCodeProcess.restype = w.BOOL
        k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k32.CloseHandle.argtypes = [w.HANDLE]
    except (OSError, AttributeError):
        return False
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = w.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # handle opened => process exists; assume alive
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def terminate_pid(pid: int, *, grace_seconds: float = 2.0) -> bool:
    if not pid_is_alive(pid):
        return True
    if sys.platform == "win32":
        return _win_terminate_pid(pid, grace_seconds=grace_seconds)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        return not pid_is_alive(pid)
    time.sleep(0.2)
    return not pid_is_alive(pid)


def _win_terminate_pid(pid: int, *, grace_seconds: float = 2.0) -> bool:
    """Force-kill a process (and its child tree) via taskkill. taskkill /F needs
    only PROCESS_TERMINATE, a lower bar than the PROCESS_ALL_ACCESS that os.kill
    demands, so it succeeds on detached servers where os.kill silently fails."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # fall through to the liveness poll — ground truth is whether it died
    deadline = time.monotonic() + max(grace_seconds, 0.5)
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_is_alive(pid)


def pids_on_port(port: int) -> list[int]:
    if sys.platform == "win32":
        try:
            output = subprocess.check_output(
                ["netstat", "-ano"],
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.CalledProcessError):
            return []
        needle = f":{port}"
        pids: list[int] = []
        for line in output.splitlines():
            if "LISTENING" not in line or needle not in line:
                continue
            parts = line.split()
            if not parts:
                continue
            try:
                pids.append(int(parts[-1]))
            except ValueError:
                continue
        return sorted(set(pids))
    probe_host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((probe_host, port))
            return []
        except OSError:
            pass
    if not Path(f"/proc/net/tcp").exists():
        return []
    return []


def read_serve_state(path: Path) -> ServeState | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ServeState.from_json(payload)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_serve_state(path: Path, state: ServeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_serve_state(path: Path) -> None:
    path.unlink(missing_ok=True)


def reclaim_previous_serve(*, pid_path: Path, state_path: Path, port: int) -> list[int]:
    """Stop a prior BigBoss serve instance so restarts never need manual netstat."""
    reclaimed: list[int] = []
    candidates: list[int] = []
    state = read_serve_state(state_path)
    if state and state.port == port:
        candidates.append(state.pid)
    if pid_path.exists():
        try:
            candidates.append(int(pid_path.read_text(encoding="utf-8").strip()))
        except ValueError:
            pid_path.unlink(missing_ok=True)
    for pid in pids_on_port(port):
        candidates.append(pid)
    seen: set[int] = set()
    for pid in candidates:
        if pid in seen or pid <= 0:
            continue
        seen.add(pid)
        if pid == os.getpid():
            continue
        if terminate_pid(pid):
            reclaimed.append(pid)
    for path in (pid_path, state_path):
        if path.exists():
            path.unlink(missing_ok=True)
    return reclaimed


def acquire_pidfile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def release_pidfile(path: Path) -> None:
    if not path.exists():
        return
    try:
        recorded = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return
    if recorded == os.getpid():
        path.unlink(missing_ok=True)


def assert_serve_port_available(host: str, port: int) -> None:
    if pids_on_port(port):
        print(f"ERROR: port {port} is still in use after reclaim.")
        print("Another application is listening on that port — pick a different --port.")
        raise SystemExit(1)
    probe_host = host if host and host not in {"::"} else "0.0.0.0"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((probe_host, port))
        except OSError as exc:
            print(f"ERROR: port {port} is already in use on {probe_host}.")
            raise SystemExit(1) from exc


def wait_for_health(port: int, *, timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    url_host = f"127.0.0.1:{port}"
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0) as sock:
                request = (
                    f"GET /api/health HTTP/1.1\r\nHost: {url_host}\r\nConnection: close\r\n\r\n"
                ).encode("utf-8")
                sock.sendall(request)
                # Read until EOF: the server writes headers and body as
                # separate segments, so a single recv can miss the body.
                chunks = []
                while len(b"".join(chunks)) < 65536:
                    data = sock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                response = b"".join(chunks)
                if b'"ok"' in response.lower() and b"true" in response.lower():
                    return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def pin_rate_limit_ok(client_key: str, *, max_failures: int = 5, window_seconds: int = 60) -> bool:
    now = time.monotonic()
    recent = [t for t in _pin_failures[client_key] if now - t < window_seconds]
    _pin_failures[client_key] = recent
    return len(recent) < max_failures


def record_pin_failure(client_key: str) -> None:
    _pin_failures[client_key].append(time.monotonic())


def reset_pin_failures(client_key: str) -> None:
    _pin_failures.pop(client_key, None)


def open_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def is_port_blocked(port: int) -> bool:
    """Checks if there is an active inbound firewall rule for the specified TCP port on Windows."""
    if sys.platform != "win32":
        return False
    try:
        # Query using Get-NetFirewallRule (requires Windows 8+/Server 2012+)
        cmd = f'Get-NetFirewallRule -Enabled True -Direction Inbound | Get-NetFirewallPortFilter | Where-Object {{ $_.LocalPort -eq "{port}" -and $_.Protocol -eq "TCP" }}'
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            check=True,
            timeout=5.0
        )
        if result.stdout.strip():
            return False  # An allowed inbound rule exists
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        # Fallback to netsh
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=BigBoss"],
            capture_output=True,
            text=True,
            timeout=5.0
        )
        if "Enabled:                              Yes" in result.stdout or "Enabled: Yes" in result.stdout:
            return False
    except Exception:
        pass

    return True  # Blocked by default policy


def is_admin() -> bool:
    """Checks if the current process runs as Administrator."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def add_firewall_rule_uac(port: int) -> bool:
    """Prompts UAC to add the inbound rule for BigBoss on the Private profile."""
    if sys.platform != "win32":
        return True
    if is_admin():
        cmd = f'New-NetFirewallRule -DisplayName "BigBoss" -Direction Inbound -Action Allow -Protocol TCP -LocalPort {port} -Profile Private'
        res = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=5.0)
        return res.returncode == 0

    ps_cmd = (
        f"Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command "
        f"\"New-NetFirewallRule -DisplayName \\\"BigBoss\\\" -Direction Inbound -Action Allow "
        f"-Protocol TCP -LocalPort {port} -Profile Private\"' -Verb RunAs"
    )
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5.0)
        return result.returncode == 0
    except Exception:
        return False



import threading

class IPChangeMonitor(threading.Thread):
    """Background daemon thread to monitor active LAN IP changes."""
    def __init__(self, on_change_callback, interval_seconds: float = 15.0):
        super().__init__(daemon=True)
        self.on_change_callback = on_change_callback
        self.interval_seconds = interval_seconds
        from .hosts import local_ip_hint
        self.last_ip = local_ip_hint()
        self.stop_event = threading.Event()

    def run(self) -> None:
        from .hosts import local_ip_hint
        while not self.stop_event.is_set():
            current_ip = local_ip_hint()
            if current_ip != self.last_ip:
                try:
                    self.on_change_callback(self.last_ip, current_ip)
                except Exception as exc:
                    print(f"Error in IPChangeMonitor callback: {exc}")
                self.last_ip = current_ip
            self.stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()


import re

class SSHTunnelManager:
    """Manages an on-demand transient SSH reverse tunnel using native ssh.exe."""
    def __init__(self, local_port: int = 8787, on_url_ready=None):
        self.local_port = local_port
        self.on_url_ready = on_url_ready
        self.process = None
        self.tunnel_url = None
        self._stop_event = threading.Event()
        self._thread = None

    def start(self) -> None:
        cmd = [
            "ssh",
            "-N", "-T",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-R", f"80:127.0.0.1:{self.local_port}",
            "nokey@localhost.run",
            "--", "--output", "json"
        ]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
        except FileNotFoundError:
            print("\n[Tunnel] Error: 'ssh' client not found on system PATH. Cannot open tunnel.")
            return

        self._thread = threading.Thread(target=self._read_output, daemon=True)
        self._thread.start()

    def _read_output(self) -> None:
        json_pattern = re.compile(r'"address"\s*:\s*"([^"]+)"')
        text_pattern = re.compile(r"https?://[a-zA-Z0-9.-]+\.localhost\.run")
        
        for line in self.process.stdout:
            if self._stop_event.is_set():
                break
            
            # Try to match JSON output
            json_match = json_pattern.search(line)
            if json_match:
                addr = json_match.group(1).lower()
                if addr not in {"localhost.run", "admin.localhost.run"}:
                    self.tunnel_url = f"https://{addr}"
                    break
            
            # Fallback to standard text match
            text_match = text_pattern.search(line)
            if text_match:
                url = text_match.group(0)
                domain = url.split("://")[-1].lower()
                if domain not in {"localhost.run", "admin.localhost.run"}:
                    self.tunnel_url = url
                    break
                
        if self.tunnel_url:
            print(f"\n[Tunnel] Tunnel established successfully!")
            print(f"[Tunnel] Public URL: {self.tunnel_url}")
            self.display_terminal_qr(self.tunnel_url)
            if self.on_url_ready:
                try:
                    self.on_url_ready(self.tunnel_url)
                except Exception as exc:
                    print(f"[Tunnel] Error in on_url_ready callback: {exc}")
        else:
            print("\n[Tunnel] Error: Could not determine tunnel URL from SSH output.")

    def display_terminal_qr(self, url: str) -> None:
        from urllib.request import urlopen, Request
        from urllib.parse import quote
        print(f"[Tunnel] Fetching terminal QR code for: {url}\n")
        try:
            req = Request(
                f"https://qrenco.de/{quote(url)}",
                headers={"User-Agent": "curl/7.68.0"}
            )
            with urlopen(req, timeout=5) as response:
                qr_art = response.read().decode("utf-8")
                print(qr_art)
        except Exception as e:
            print(f"[Tunnel] Terminal QR code generation failed: {e}")

    def stop(self) -> None:
        self._stop_event.set()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None


