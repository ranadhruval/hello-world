#!/usr/bin/env python3
"""Test Groww credentials and nothing else.

Run this before scripts/reconcile.py. Reconciliation loads a 136k-row
instrument master before it ever touches the network, so an auth failure there
surfaces late and tangled up with other output. This isolates the mint.

    export TOTP_TOKEN='your-api-key'
    export TOTP_SECRET='your-totp-secret'
    python scripts/authcheck.py

Needs only: growwapi, pyotp. No .env, no Postgres, no Redis, no CSV.
Read-only — it reads your profile and stops.
"""

from __future__ import annotations

import os

FIXES = {
    "binascii": (
        "TOTP_SECRET is not valid base32.",
        "You have probably pasted the API key into TOTP_SECRET, or copied the\n"
        "  otpauth:// URL instead of the secret itself. The secret is the string\n"
        "  shown beside the QR code — A-Z and 2-7 only, no other characters.",
    ),
    "GrowwAPIAuthenticationException": (
        "Groww rejected the credentials (401).",
        "Either the clock is skewed or the code was already used. On macOS turn on\n"
        "  System Settings > General > Date & Time > Set automatically, then wait for\n"
        "  the next 30-second window and retry. TOTP breaks on clock skew.",
    ),
    "GrowwAPIRateLimitException": (
        "Rate limited (429).",
        "The token endpoint allows 150 mints per 24h. Wait, then retry.",
    ),
}


def diagnose(exc: BaseException) -> None:
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    text = f"{name}: {exc}"

    print(f"\n  FAILED  {text}\n")
    for key, (headline, advice) in FIXES.items():
        if key in name or key in module:
            print(f"  {headline}\n  {advice}\n")
            return

    if "400" in str(exc) or "Bad Request" in str(exc):
        print(
            "  Groww rejected the request (400).\n"
            "  The most likely cause is that this is an API Key & Secret credential,\n"
            "  not a TOTP one. That flow needs secret= instead of totp=, and requires\n"
            "  approving in the browser every morning — which is why this project uses\n"
            "  TOTP. Regenerate as a TOTP key at groww.in/trade-api/api-keys.\n"
        )
        return

    print("  Unrecognised error. Paste the whole traceback and I can dig in.\n")


def main() -> int:
    token = os.environ.get("TOTP_TOKEN", "").strip()
    secret = os.environ.get("TOTP_SECRET", "").strip()

    if not token or not secret:
        print(
            "\n  TOTP_TOKEN and TOTP_SECRET must be set in THIS shell.\n\n"
            "    export TOTP_TOKEN='your-api-key'\n"
            "    export TOTP_SECRET='your-totp-secret'\n\n"
            "  Use single quotes — TOTP secrets can contain characters the shell\n"
            "  would otherwise expand.\n"
        )
        return 2

    try:
        import pyotp
        from growwapi import GrowwAPI
    except ImportError as exc:
        print(f"\n  {exc}\n\n  pip install growwapi pyotp\n")
        return 2

    # Groww shows the secret with spaces in groups of four; pyotp wants it bare.
    normalised = secret.replace(" ", "").replace("-", "").upper()
    print(f"\n  token    {token[:4]}…{token[-4:]} ({len(token)} chars)")
    print(f"  secret   {len(normalised)} chars")

    try:
        code = pyotp.TOTP(normalised).now()
    except Exception as exc:
        diagnose(exc)
        return 1
    print(f"  code     {code}")

    try:
        raw = GrowwAPI.get_access_token(api_key=token, totp=code)
        access = raw if isinstance(raw, str) else raw.get("token") or raw.get("access_token")
        groww = GrowwAPI(access)
        profile = groww.get_user_profile(timeout=10)
    except Exception as exc:
        diagnose(exc)
        return 1

    print(f"\n  OK  authenticated as {profile}\n")
    print("  Credentials are good. Next: python scripts/reconcile.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
