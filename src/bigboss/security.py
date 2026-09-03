from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from pathlib import Path


TOKEN_BYTES = 32


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(TOKEN_BYTES)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(token_hash(token), expected_hash)


def new_pair_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    first = "".join(secrets.choice(alphabet) for _ in range(4))
    second = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"{first}-{second}"


def new_lan_pin() -> str:
    return f"{secrets.randbelow(900_000) + 100_000:06d}"


def read_or_create_lan_pin(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    pin = new_lan_pin()
    path.write_text(pin + "\n", encoding="utf-8")
    return pin


def read_or_create_secret(path: Path, prefix: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = new_token(prefix)
    path.write_text(token + "\n", encoding="utf-8")
    return token

