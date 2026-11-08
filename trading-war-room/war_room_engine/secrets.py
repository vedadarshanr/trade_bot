"""Optional API keys for paper-mode real-data enrichment.

Keys are read from process env or trading-war-room/.env (never committed).
Missing keys keep public endpoints working; live X / private RPC stay off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .paths import ROOT

ENV_PATH = ROOT / ".env"

# Keys we ask the operator to paste in chat when available.
REQUESTED_SECRETS = (
    ("XAI_API_KEY", "xAI / Grok key for optional Live Search X velocity"),
    ("SOLANA_RPC_URL", "Helius/QuickNode/Alchemy Solana RPC (holder concentration)"),
    ("X_BEARER_TOKEN", "Optional Twitter/X API bearer (else xAI Live Search)"),
)


@dataclass(frozen=True)
class SecretStatus:
    name: str
    present: bool
    purpose: str


def _load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def refresh_env() -> None:
    _load_dotenv()


def get_secret(name: str, default: str = "") -> str:
    refresh_env()
    return (os.getenv(name) or default).strip()


def secret_status() -> list[SecretStatus]:
    refresh_env()
    return [
        SecretStatus(name=n, present=bool(os.getenv(n)), purpose=p)
        for n, p in REQUESTED_SECRETS
    ]


def write_secret(name: str, value: str) -> Path:
    """Append/update a key in .env (local only; .gitignore covers .env)."""
    refresh_env()
    name = name.strip()
    value = value.strip()
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{name}="):
            out.append(f"{name}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[name] = value
    return ENV_PATH
