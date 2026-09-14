"""FastAPI surface: health, the account-link page, and the inbound webhook.

Credentials are never accepted in the WhatsApp chat itself (spec §4.1) —
WhatsApp backups are not under your control. They are collected here, over a
signed single-use link, tested against Groww before anything is stored, and
written encrypted.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

import pyotp
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from growwapi import GrowwAPI

from app.auth.broker import _access_token
from app.auth.crypto import Crypto
from app.config import settings
from app.store.db import Store

log = logging.getLogger(__name__)
app = FastAPI(title="Groww desk", docs_url=None, redoc_url=None)

_store: Store | None = None
_crypto: Crypto | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def crypto() -> Crypto:
    global _crypto
    if _crypto is None:
        key = settings().cred_key
        if not key:
            raise RuntimeError("CRED_KEY is not set — refusing to handle credentials")
        _crypto = Crypto(key)
    return _crypto


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/link", response_class=HTMLResponse)
async def link_page(t: str = "") -> HTMLResponse:
    if not t or store().link_request_wa_hash(t) is None:
        return HTMLResponse(_PAGE_EXPIRED, status_code=410)
    return HTMLResponse(_PAGE_FORM.replace("{{TOKEN}}", t))


@app.post("/link", response_class=HTMLResponse)
async def link_submit(
    token: str = Form(...),
    wa_id: str = Form(...),
    totp_token: str = Form(...),
    totp_secret: str = Form(...),
) -> HTMLResponse:
    st = store()
    wa_hash = st.link_request_wa_hash(token)
    if wa_hash is None or wa_hash != hashlib.sha256(wa_id.encode()).hexdigest():
        return HTMLResponse(_PAGE_EXPIRED, status_code=410)

    # Test the credentials before storing anything. On failure show Groww's
    # actual error and store nothing.
    try:
        access = await asyncio.to_thread(
            GrowwAPI.get_access_token,
            api_key=totp_token.strip(),
            totp=pyotp.TOTP(totp_secret.strip()).now(),
        )
        client = GrowwAPI(_access_token(access))
        await asyncio.to_thread(client.get_holdings_for_user, timeout=5)
        fingerprint = await asyncio.to_thread(_account_fingerprint, client)
    except Exception as exc:
        log.warning("link attempt failed: %s", type(exc).__name__)
        return HTMLResponse(
            _PAGE_ERROR.replace("{{ERROR}}", f"{type(exc).__name__}: {exc}"), status_code=400
        )

    if not st.consume_link_token(token, wa_id):
        return HTMLResponse(_PAGE_EXPIRED, status_code=410)

    user_id = await st.user_id_for(wa_id)
    if fingerprint:
        # Merges this chat address into an existing user when the same
        # brokerage account has been linked before under a different WhatsApp
        # address — the only evidence that a LID and a phone JID are one
        # person, since they share no digits.
        user_id = await st.bind_account(user_id, fingerprint)
    await st.put_credentials(
        user_id,
        crypto().encrypt(totp_token.strip()),
        crypto().encrypt(totp_secret.strip()),
    )
    log.info("linked user_id=%s account_known=%s", user_id, bool(fingerprint))
    return HTMLResponse(_PAGE_OK)


# Keys Groww has used for the account identifier. Tried in order; the first
# non-empty one wins.
_ACCOUNT_KEYS = ("user_id", "userId", "groww_user_id", "growwUserId", "client_id", "clientId")


def _account_fingerprint(client: GrowwAPI) -> str:
    """A stable, non-reversible id for the linked brokerage account.

    HMAC rather than the raw id so a database leak does not expose Groww
    account identifiers, keyed on LINK_SECRET like the link tokens.

    Returns "" when the profile carries nothing usable — the account then
    simply cannot be merged, which is a lost convenience rather than a wrong
    answer. Never invent an identifier: a fabricated one would merge two
    unrelated people's books.
    """
    try:
        profile = client.get_user_profile(timeout=5) or {}
    except Exception as exc:
        log.warning("profile lookup failed, skipping account fingerprint: %s", type(exc).__name__)
        return ""
    raw = next((str(profile[k]) for k in _ACCOUNT_KEYS if profile.get(k)), "")
    if not raw:
        log.warning("profile carried no known account key; merge unavailable for this link")
        return ""
    return hmac.new(settings().link_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


@app.post("/webhook/inbound")
async def inbound(request: Request) -> JSONResponse:
    """Receives normalised messages from the channel adapter."""
    payload = await request.json()
    if not payload.get("wa_id"):
        raise HTTPException(status_code=400, detail="wa_id required")
    # The adapter publishes to the Redis stream the worker consumes; this
    # endpoint exists so the Cloud API migration has a home.
    return JSONResponse({"accepted": True})


_STYLE = """
<style>
  body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:32rem;margin:3rem auto;
       padding:0 1.25rem;color:#111}
  h1{font-size:1.35rem;margin-bottom:.25rem}
  p.sub{color:#555;margin-top:0}
  label{display:block;margin:1.1rem 0 .3rem;font-weight:600;font-size:.9rem}
  input{width:100%;padding:.6rem;border:1px solid #ccc;border-radius:6px;font-size:1rem}
  button{margin-top:1.5rem;width:100%;padding:.75rem;border:0;border-radius:6px;
         background:#00b386;color:#fff;font-size:1rem;font-weight:600}
  .note{background:#f6f6f6;border-radius:6px;padding:.85rem;font-size:.85rem;color:#444;
        margin-top:1.5rem}
  code{background:#eee;padding:.1rem .3rem;border-radius:3px}
  @media(prefers-color-scheme:dark){
    body{background:#0b0e11;color:#e8e8e8}
    input{background:#15191e;border-color:#2a2f36;color:#e8e8e8}
    .note{background:#15191e;color:#aaa} code{background:#22272e}
  }
</style>
"""

_PAGE_FORM = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Connect Groww</title>{_STYLE}
<h1>Connect your Groww account</h1>
<p class=sub>Read-only. This desk never places orders.</p>
<form method=post action=/link>
  <input type=hidden name=token value="{{{{TOKEN}}}}">
  <label>Your WhatsApp number <span style="font-weight:400;color:#777">(digits only, with country code)</span></label>
  <input name=wa_id inputmode=numeric placeholder="919876543210" required>
  <label>TOTP API key</label>
  <input name=totp_token required autocomplete=off>
  <label>TOTP secret</label>
  <input name=totp_secret required autocomplete=off>
  <button type=submit>Connect</button>
</form>
<div class=note>
  <strong>Where to find these</strong><br>
  1. Open <code>groww.in/trade-api/api-keys</code><br>
  2. Create a key and choose the <strong>TOTP</strong> flow, not API Key &amp; Secret —
     the latter needs you to approve in the browser every morning.<br>
  3. Copy the API key and the TOTP secret shown with the QR code.<br><br>
  Your credentials are tested immediately, stored encrypted, and never read
  from this chat.
</div>
"""

_PAGE_OK = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Connected</title>{_STYLE}
<h1>Connected</h1>
<p>You can close this page and go back to WhatsApp.</p>
<p class=sub>Try: <code>portfolio</code> · <code>positions</code> · <code>nifty</code></p>
"""

_PAGE_EXPIRED = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Link expired</title>{_STYLE}
<h1>This link has expired</h1>
<p>Links are single-use and last ten minutes. Send <code>link</code> on
WhatsApp for a fresh one.</p>
"""

_PAGE_ERROR = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Couldn't connect</title>{_STYLE}
<h1>Groww rejected those credentials</h1>
<p class=sub>Nothing was saved.</p>
<div class=note><code>{{{{ERROR}}}}</code></div>
<p style="margin-top:1.5rem"><a href="javascript:history.back()">Go back and try again</a></p>
"""
