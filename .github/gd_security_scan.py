#!/usr/bin/env python3
"""Golden Dome M3 security scan: fail on leaked secrets or known-malicious packages.

Copied into each SSR repository as .github/gd_security_scan.py and run by
.github/workflows/gd-security.yml; the same file runs locally for proofs.
Standard library only. Never prints secret values (Betterleaks runs with --redact).

Exit codes: 0 clean, 1 secrets or malicious packages found, 2 a scanner failed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def scan_secrets(betterleaks: str, root: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "leaks.json"
        p = subprocess.run([betterleaks, "git", str(root), "--redact", "--no-banner", "--log-level", "error",
                            "-f", "json", "-r", str(report), "--exit-code", "0"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0 or not report.exists():
            raise RuntimeError(f"betterleaks failed ({p.returncode}): {p.stderr.strip()[:300]}")
        return json.loads(report.read_text(encoding="utf-8") or "[]")


def scan_packages(osv: str, root: Path) -> tuple[list[tuple[str, str, str]], int, int]:
    """Return (malicious hits as (package, version, MAL id), other advisory count, packages scanned)."""
    p = subprocess.run([osv, "scan", "source", "--no-ignore", "-r", str(root), "--format", "json"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    # osv-scanner exits 1 when it finds vulnerabilities and 128 when there is nothing to scan.
    if p.returncode == 128:
        return [], 0, 0
    if p.returncode not in (0, 1):
        raise RuntimeError(f"osv-scanner failed ({p.returncode}): {p.stderr.strip()[:300]}")
    data = json.loads(p.stdout or "{}")
    malicious, other = [], 0
    # Vulnerable packages are listed in results; the scanned total comes from stderr's "found N package(s)".
    scanned = sum(int(n) for n in re.findall(r"found (\d+) package", p.stderr))
    for result in data.get("results") or []:
        for pkg in result.get("packages") or []:
            info = pkg.get("package", {})
            for vuln in pkg.get("vulnerabilities") or []:
                ids = [vuln.get("id", "")] + vuln.get("aliases", [])
                mal = next((i for i in ids if i.startswith("MAL-")), None)
                if mal:
                    hit = (info.get("name", "?"), info.get("version", "?"), mal)
                    if hit not in malicious:
                        malicious.append(hit)
                else:
                    other += 1
    return malicious, other, scanned


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--betterleaks", required=True)
    ap.add_argument("--osv-scanner", required=True)
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        # Absolute paths: Windows does not resolve relative executables against the cwd.
        leaks = scan_secrets(str(Path(args.betterleaks).resolve()), root)
        malicious, other, scanned = scan_packages(str(Path(args.osv_scanner).resolve()), root)
    except (RuntimeError, OSError, ValueError) as e:
        # A scanner that cannot run is never reported as "clean" or as "found".
        print(f"ERROR {type(e).__name__}: {e}")
        return 2
    for leak in leaks:
        print(f"SECRET  {leak.get('RuleID')} in {leak.get('File')}:{leak.get('StartLine')} (commit {str(leak.get('Commit'))[:8]})")
    for name, version, mal in malicious:
        print(f"MALWARE {name}@{version} ({mal})")
    print(f"summary: {scanned} package(s) scanned, {len(leaks)} secret(s), {len(malicious)} malicious package(s), "
          f"{other} other advisory hit(s) (reported, not failing)")
    return 1 if leaks or malicious else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
