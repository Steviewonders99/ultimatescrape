"""Warehouse and platform-proxy credential resolution.

Portable by design: environment first, Steven's local files second, loud
failure third. When this module moves into worker/pipeline (spec §10) the
environment branch is the only one that runs.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_LOCAL = Path("/Users/stevenjunop/centric-intake/.env.local")


def _from_env_local(key: str) -> str | None:
    if not ENV_LOCAL.exists():
        return None
    for line in ENV_LOCAL.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def warehouse_dsn() -> str:
    dsn = os.getenv("ONETAKE_DATABASE_URL") or _from_env_local("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "warehouse DSN missing: set ONETAKE_DATABASE_URL or provide "
            f"DATABASE_URL in {ENV_LOCAL}"
        )
    return dsn


def proxy_base() -> str:
    return os.getenv("ONETAKE_PROXY_URL", "https://onetake-proxy.oneforma.com")


def proxy_secret() -> str:
    sec = (
        os.getenv("ONETAKE_PROXY_SECRET")
        or _from_env_local("PROXY_SECRET")
        or _from_env_local("DB_PROXY_SECRET")
    )
    if not sec:
        raise RuntimeError(
            "proxy secret missing: set ONETAKE_PROXY_SECRET or provide "
            f"PROXY_SECRET or DB_PROXY_SECRET in {ENV_LOCAL}"
        )
    return sec
