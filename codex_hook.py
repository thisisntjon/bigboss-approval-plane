from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "src"))

    argv = list(sys.argv[1:])
    if "--harness" not in argv:
        argv = ["--harness", "codex", *argv]
    if "--adapter-token" not in argv and "--adapter-token-file" not in argv:
        argv.extend(["--adapter-token-file", str(root / ".bigboss" / "secrets" / "adapter-token.txt")])

    from bigboss.hook_adapter import main as hook_main

    return hook_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
