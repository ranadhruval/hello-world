# Quickstart — fresh clone to a message on your phone

Ten minutes from a fresh pull to a message on your phone. Assumes Docker
Desktop, Python 3.11+, Node 18+ and the spare SIM already paired once.

> Cloudflared is blocked on this network — its own pre-check showed port 7844
> closed for both QUIC and TCP. Don't retry it; use the LAN IP below. The tunnel
> only ever mattered for the 30 seconds of account linking.

## 1 · Pull and install

```bash
cd ~/groww-desk            # wherever you cloned it
git checkout claude/groww-api-integration-s7f6ey
git pull

source .venv/bin/activate   # or: python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cd adapter && npm install && cd ..
```

## 2 · Secrets (skip if `.env` already has them)

```bash
cp -n .env.example .env && chmod 600 .env
python -m app.auth.crypto >> .env          # writes CRED_KEY=...
echo "LINK_SECRET=$(python -c 'import secrets;print(secrets.token_hex(32))')" >> .env
```

## 3 · Point the link page at your LAN IP

The phone has to reach this box. `localhost` will not do it.

```bash
ipconfig getifaddr en0                     # e.g. 192.168.1.42  (en1 on Wi-Fi-only Macs)
```

Edit `.env`:

```
PUBLIC_BASE_URL=http://192.168.1.42:8000
```

Must be `http`, not `https`, and the real IP — `doctor` rejects the placeholder.

## 4 · Infrastructure and schema

```bash
docker compose up -d                       # postgres + redis only
make migrate                               # applies schema.sql AND schema_phase2.sql
make instruments                           # ~137k instruments, takes a minute
make doctor
```

`migrate` is idempotent — re-running is safe and prints `already exists` notices.

## 5 · Three terminals

```bash
make api HOST=0.0.0.0     # 1 — binds all interfaces so the phone can reach /link
make worker               # 2 — chat + outbox drainer + scheduler
cd adapter && npm start   # 3 — scan the QR with the spare SIM if it asks
```

`HOST=0.0.0.0` on the API is what makes the link page reachable. The adapter
stays on loopback by design — `/send` can make that WhatsApp account message
anyone.

## 6 · Link your account

From **your personal phone**, first check the LAN actually works:

```
http://192.168.1.42:8000/health
```

If that loads, text `link` to the spare SIM, open the link it sends back, and
paste your Groww TOTP token and secret. You get them at
`groww.in/trade-api/api-keys`.

If `/health` does not load, the network has client isolation on. Tether the
laptop to your phone's hotspot, re-run step 3 with the new IP, and link over
that — you only need it once.

## 7 · Check it works

```bash
# chat
# text "portfolio" to the spare SIM — you should get your book back

make shadow                # today's would-have-sent report (empty until signals arrive)
make smoke-db DSN=...      # optional: exercises the DB layer against a throwaway database
```

---

## What works today, and what doesn't

**Works:** chat (portfolio, positions, margin, orders, quotes), the link flow,
and the two scheduled briefs — pre-market 08:45, post-close 15:45 — which queue
to the outbox and are delivered by the worker's drainer.

**Doesn't yet:** intraday alerts. The rules, gate and composer are built and
tested, but nothing polls the signal engine yet, so nothing feeds them. Briefs
will arrive; volume-spike and news nudges will not.

**Known wrong:** `app/market/holidays.json` has only fixed-date national
holidays. 2026 is covered well enough that the worker starts, but festival
holidays (Diwali, Holi, Id, Dussehra) are missing — so a brief will fire on
those mornings. Cosmetic, not dangerous. Paste the dates from the
[NSE holiday list](https://www.nseindia.com/resources/exchange-communication-holidays)
into the `"2026"` array when you get a minute; the file reloads on save, no
restart needed.

**Unverified:** `DEFAULT_BASIS` in `app/tools/pnl.py`. Run `make reconcile`
once against your live book — it auto-detects the right convention. Until then
every F&O number is unconfirmed.

## Verifying credentials and numbers

```bash
export TOTP_TOKEN='your-api-key'
export TOTP_SECRET='your-totp-secret'

python scripts/authcheck.py
```

Single quotes matter — TOTP secrets can contain characters the shell expands.

This tests only the token mint and names the fix for each common failure
(secret not base32, clock skew, wrong auth flow). Once it prints a profile,
credentials are good.

Then confirm the numbers before you trust any of them:

```bash
python scripts/reconcile.py
```

Compare portfolio value and margin against the Groww app **in the same
minute**. The script also decides the basis convention for you and tells you
whether `DEFAULT_BASIS` needs changing.


## If something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `make doctor` fails on PUBLIC_BASE_URL | placeholder, or unreachable | Use the real LAN IP, `http` not `https` |
| Phone can't open the link | API on loopback, or AP isolation | `make api HOST=0.0.0.0`; else hotspot |
| Texting does nothing | adapter not paired | Check terminal 3 for a QR; `redis-cli XLEN inbound` should climb |
| Messages sent, none arrive | wrong address | Adapter log shows the JID; inbound and outbound should match |
| Worker traceback every 5s | stale build | `git pull` — `socket_timeout` fix is committed |
| Bot goes quiet overnight | laptop slept | Expected on a laptop. Caffeinate, or accept it |

## Known limits on a Mac

- **Sleep stops everything.** Fine while dogfooding. It becomes a real problem
  for Phase 2, which is why the spec calls for a Mumbai VPS with a static IP —
  the static IP is also mandatory for Phase 3 order placement.
- **A tunnel URL changes on restart**, so `PUBLIC_BASE_URL` needs re-setting.
  Only matters when linking a new account.
- **Baileys interactive messages** (buttons, lists) often do not render on
  personal accounts, so the adapter sends numbered plain text instead.


## Security notes

- Groww credentials never pass through WhatsApp. They are collected on the
  link page, tested, then stored AES-256-GCM encrypted. A Baileys compromise
  leaks messages, not credentials.
- The adapter binds to `127.0.0.1`. `POST /send` can make your WhatsApp
  message any number, so it must not be exposed to the network.
- `adapter/session/` is a bearer token for the entire WhatsApp account, not
  just this bot. Gitignored; keep it off synced folders.
- Logs record `wa_id` and message ids, never message text. Token-shaped
  strings are redacted.
- `unlink` really deletes — credentials, messages, traces, watches, the lot.
