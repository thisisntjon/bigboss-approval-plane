from __future__ import annotations

import socket
from socket import gethostbyname, gethostname


def local_ip_hint() -> str:
    """Returns the primary active LAN IP using a routing lookup trick.
    
    Avoids returning virtual adapter IPs (WSL/VPN) or loopback addresses.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # The address does not need to be reachable.
        # This connection is entirely local to trigger routing resolution.
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except OSError:
        try:
            return gethostbyname(gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def get_mdns_hostname() -> str:
    """Returns the native Windows mDNS hostname (e.g. <computername>.local)."""
    try:
        name = gethostname()
        if name and not name.lower().endswith(".local"):
            return f"{name}.local"
        return name or "localhost"
    except Exception:
        return "localhost"

