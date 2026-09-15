#!/usr/bin/env python3
"""Preflight: check every prerequisite and name the fix for each failure.

    python scripts/doctor.py

Run it from the repo root. Checks are ordered so the first failure is the one
worth fixing — later checks depend on earlier ones. Read-only: it touches
nothing and starts nothing.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "ok  ", "warn", "FAIL"
GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
COLOUR = {OK: GREEN, WARN: YELLOW, FAIL: RED}

results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> str:
    results.append((name, status, detail))
    print(
        f"  {COLOUR[status]}{status}{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else "")
    )
    return status


def port_open(url: str, default_port: int) -> bool:
    parsed = urlparse(url if "//" in url else f"//{url}")
    host, port = parsed.hostname or "localhost", parsed.port or default_port
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


# Shipped in .env.example. Passing the format check while being unroutable is
# the worst case: the link is generated, texted, and simply does not open.
PLACEHOLDER_HOSTS = ("example.in", "example.com", "example.org", "desk.yourdomain")


def _base_url_status(url: str) -> tuple[str, str]:
    """PUBLIC_BASE_URL must be reachable from a phone, not merely well-formed."""
    if not url.startswith("http"):
        return FAIL, f"{url!r} is not a URL"

    host = urlparse(url).hostname or ""
    if any(p in url for p in PLACEHOLDER_HOSTS):
        return FAIL, f"{url} is the placeholder from .env.example — set your real tunnel or LAN URL"
    if host in {"localhost", "127.0.0.1", "::1"}:
        return FAIL, f"{url} — your phone cannot reach localhost; use a LAN IP or a tunnel"

    try:
        import httpx

        httpx.get(f"{url.rstrip('/')}/health", timeout=5)
        return OK, f"{url} reachable"
    except Exception:
        # Unreachable from here is not proof it is unreachable from the phone
        # (split-horizon DNS, a LAN address on another interface), so warn --
        # but name the two causes that actually happen, because the phone
        # seeing "took too long to respond" says nothing about which it is.
        return WARN, (
            f"{url} did not respond to /health — the api is probably on loopback "
            "(restart it as `make api HOST=0.0.0.0`), or this hostname does not "
            "resolve on this network"
        )


def main() -> int:  # noqa: C901 - a flat checklist reads better than nesting
    print(f"\nGroww desk preflight\n{'─' * 60}")

    if Path.cwd() != ROOT:
        check("working directory", WARN, f"run from {ROOT} — .env and data/ are relative")
    else:
        check("working directory", OK, str(ROOT))

    # ---- python ----
    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        check("python >= 3.11", OK, f"{v.major}.{v.minor}.{v.micro}")
    else:
        check("python >= 3.11", FAIL, f"{v.major}.{v.minor} — StrEnum needs 3.11+")

    # ---- dependencies ----
    missing = []
    for mod, pkg in [
        ("growwapi", "growwapi"),
        ("pyotp", "pyotp"),
        ("pydantic", "pydantic"),
        ("pydantic_settings", "pydantic-settings"),
        ("httpx", "httpx"),
        ("psycopg", "psycopg[binary]"),
        ("redis", "redis"),
        ("cryptography", "cryptography"),
        ("fastapi", "fastapi"),
    ]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        check("python packages", FAIL, f"missing: {' '.join(missing)} — pip install -e .")
    else:
        check("python packages", OK)

    # Importing the app is the only check that catches an import-time failure
    # in the API — a missing Form dependency, a bad route decorator. Listing
    # top-level packages does not: python-multipart is imported by FastAPI,
    # not by us, and its absence only surfaces when a route is built.
    try:
        import app.main  # noqa: F401

        check("api imports", OK)
    except Exception as exc:
        check("api imports", FAIL, f"{type(exc).__name__}: {str(exc).splitlines()[0]}")

    # ---- .env ----
    if not (ROOT / ".env").exists():
        check(".env", FAIL, "cp .env.example .env && chmod 600 .env")
    else:
        mode = oct((ROOT / ".env").stat().st_mode)[-3:]
        check(
            ".env",
            OK if mode == "600" else WARN,
            f"mode {mode}" + ("" if mode == "600" else " — chmod 600 .env"),
        )

    from app.config import settings  # noqa: E402

    cfg = settings()

    from app.auth.crypto import Crypto  # noqa: E402

    try:
        Crypto(cfg.cred_key)
        check("CRED_KEY", OK)
    except ValueError as exc:
        check("CRED_KEY", FAIL, str(exc).splitlines()[0])

    if cfg.link_secret:
        check("LINK_SECRET", OK)
    else:
        check("LINK_SECRET", FAIL, "set any long random string — it signs the link tokens")

    check("PUBLIC_BASE_URL", *_base_url_status(cfg.public_base_url))

    # ---- infrastructure ----
    dsn = cfg.database_url.replace("postgresql+psycopg://", "postgresql://")
    redis_up = port_open(cfg.redis_url, 6379)
    pg_up = port_open(dsn, 5432)

    # Only relevant when something is actually down — someone running Postgres
    # and Redis from Homebrew has no Docker and should not be told to install it.
    if not (redis_up and pg_up):
        if not shutil.which("docker"):
            check(
                "docker",
                WARN,
                "not installed — either `brew install --cask docker`, or run postgres "
                "and redis from Homebrew (see docs/QUICKSTART.md §4)",
            )
        elif subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode:
            # The distinction that matters: telling someone to run
            # `docker compose up -d` against a stopped daemon is a loop.
            check(
                "docker",
                FAIL,
                "Docker Desktop is installed but not running — `open -a Docker`, "
                "wait for the whale icon, then retry",
            )
        else:
            check("docker", OK, "daemon running")

    if redis_up:
        check("redis", OK, cfg.redis_url)
    else:
        check("redis", FAIL, "docker compose up -d redis   (or: brew services start redis)")

    if not pg_up:
        check("postgres", FAIL, "docker compose up -d postgres")
    else:
        try:
            import psycopg

            with psycopg.connect(dsn, connect_timeout=3) as conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM users")
                cur.execute("SELECT count(*) FROM instruments")
                count = cur.fetchone()[0]
            check("postgres schema", OK, f"{count} instruments loaded")
        except Exception as exc:
            name = type(exc).__name__
            if "UndefinedTable" in name:
                check("postgres schema", FAIL, "tables missing — make migrate")
            else:
                check("postgres schema", FAIL, f"{name}: {exc}")

    # ---- instrument master ----
    csv = ROOT / "data" / "instruments.csv"
    if not csv.exists():
        check("instrument master", FAIL, "make instruments-download")
    else:
        mb = csv.stat().st_size / 1e6
        age_h = (
            Path(csv).stat().st_mtime and (__import__("time").time() - csv.stat().st_mtime) / 3600
        )
        status = OK if mb > 5 else FAIL
        note = f"{mb:.0f}MB, {age_h:.0f}h old"
        if status is OK and age_h > 48:
            status, note = WARN, note + " — stale, run make instruments-download"
        check("instrument master", status, note)

    # ---- adapter ----
    if port_open(cfg.baileys_url, 3001):
        try:
            import httpx

            r = httpx.get(f"{cfg.baileys_url}/health", timeout=3)
            connected = r.json().get("connected")
            check(
                "whatsapp adapter",
                OK if connected else WARN,
                "connected" if connected else "running but not paired — scan the QR",
            )
        except Exception as exc:
            check("whatsapp adapter", WARN, f"port open but /health failed: {exc}")
    else:
        check("whatsapp adapter", FAIL, "cd adapter && npm start")

    if shutil.which("node"):
        check("node", OK, os.popen("node --version").read().strip())
    else:
        check("node", FAIL, "brew install node — the adapter needs it")

    # ---- groww credentials ----
    if os.environ.get("TOTP_TOKEN") and os.environ.get("TOTP_SECRET"):
        check("groww creds in env", OK, "run scripts/authcheck.py to verify them")
    else:
        check(
            "groww creds in env",
            WARN,
            "TOTP_TOKEN/TOTP_SECRET unset — only needed for authcheck and reconcile, "
            "not for the worker (it reads them from the database)",
        )

    # ---- summary ----
    failed = [n for n, s, _ in results if s is FAIL]
    warned = [n for n, s, _ in results if s is WARN]
    print("─" * 60)
    if failed:
        print(f"{RED}{len(failed)} blocking: {', '.join(failed)}{RESET}\n")
        return 1
    if warned:
        print(f"{YELLOW}ready, with {len(warned)} warning(s): {', '.join(warned)}{RESET}\n")
        return 0
    print(f"{GREEN}all checks passed — text your number and try: portfolio{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
