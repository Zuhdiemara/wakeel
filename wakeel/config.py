"""Start-up checks: in production (WAKEEL_ENV=production) Wakeel refuses to
start with demo settings, instead of quietly running insecurely."""
from __future__ import annotations

import os


class ConfigError(SystemExit):
    pass


def production() -> bool:
    return os.getenv("WAKEEL_ENV") == "production"


def problems() -> list[str]:
    """What is wrong with this configuration for production (empty = fine)."""
    if not production():
        return []
    out = []
    if not os.getenv("WAKEEL_DATABASE_URL"):
        out.append("WAKEEL_DATABASE_URL is required: production state lives in Postgres, shared by every replica")
    if os.getenv("WAKEEL_DEMO", "0") == "1":
        out.append("WAKEEL_DEMO must be off: demo tokens would let anyone sign in")
    for prefix in ("CUSTOMER", "STAFF"):
        missing = [k for k in ("ISSUER", "AUDIENCE", "JWKS_URL") if not os.getenv(f"{prefix}_{k}")]
        if missing:
            out.append(f"{prefix} sign-in needs {', '.join(prefix + '_' + m for m in missing)}")
    if not os.getenv("DAFTAR_URL"):
        out.append("DAFTAR_URL is required: production refunds go to the real ledger, not the in-memory demo")
    for k in ("WAKEEL_DATABASE_URL", "DAFTAR_URL"):
        v = os.getenv(k, "")
        if v and ("localhost" in v or "127.0.0.1" in v):
            out.append(f"{k} points at localhost")
    return out


def check() -> None:
    if errs := problems():
        raise ConfigError("Refusing to start in production:\n  - " + "\n  - ".join(errs))
