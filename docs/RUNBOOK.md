# Runbook — getting it onto WhatsApp

From a fresh clone to texting your own number. Written for macOS; the Linux
differences are noted where they matter.

Run **every command from the repo root** — `.env` and `data/` are resolved
relative to the working directory.

At any point, `python scripts/doctor.py` tells you what is missing and the
exact command to fix it. If you only remember one thing, remember that.

---

## 0. Prerequisites

```bash
python3 --version      # must be 3.11+ — the code uses StrEnum
node --version         # 18+ for the adapter
```

If Python is older: `brew install python@3.11`.
If Node is missing: `brew install node`.

You also need a **spare WhatsApp number**. Not your primary — Baileys is an
unofficial client and the number can be banned. It must be a number you can
receive WhatsApp on to scan the pairing QR.

---

## 1. Clone and install

```bash
git clone https://github.com/ranadhruval/hello-world.git groww-desk
cd groww-desk
git checkout claude/groww-api-integration-s7f6ey

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cd adapter && npm install && cd ..
```

---

## 2. Configure

```bash
cp .env.example .env
chmod 600 .env

python -m app.auth.crypto >> .env                                   # CRED_KEY
echo "LINK_SECRET=$(python -c 'import secrets;print(secrets.token_hex(32))')" >> .env
```

`CRED_KEY` encrypts your Groww credentials at rest. Losing it means re-linking;
leaking it means the encryption bought you nothing. It lives only in `.env`,
which is gitignored and `chmod 600`.

**`PUBLIC_BASE_URL` is the one people get wrong.** It must be reachable *from
your phone*, because that is where you open the link page. `localhost` will not
work. Either:

```bash
# LAN — fine at home, breaks when you change networks
ipconfig getifaddr en0                      # e.g. 192.168.1.42
# then set PUBLIC_BASE_URL=http://192.168.1.42:8000

# or a tunnel — survives network changes, works anywhere
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
# then set PUBLIC_BASE_URL to the https://….trycloudflare.com it prints
```

The tunnel URL changes on every restart unless you configure a named tunnel,
so re-set `PUBLIC_BASE_URL` when it does.

---

## 3. Infrastructure

```bash
docker compose up -d          # postgres + redis only
make migrate                  # create the schema
make instruments              # download the master and load it into postgres
```

No Docker? `brew install postgresql@16 redis && brew services start postgresql@16 && brew services start redis`,
then create the database with `createdb growwdesk`.

The app processes are deliberately **not** in Compose on a laptop — the
adapter needs an interactive QR scan, and a containerised worker would have to
reach it through `host.docker.internal`. On a Linux VPS where everything runs
in one place, `docker compose --profile full up -d` runs the lot.

---

## 4. Verify your Groww credentials

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

---

## 5. Start the three processes

Three terminals, all from the repo root with the venv active.

```bash
# 1 — WhatsApp adapter. First run prints a QR; scan it from the spare SIM.
cd adapter && npm start

# 2 — worker
python -m app.worker

# 3 — link page
uvicorn app.main:app --port 8000
```

The QR is scanned **once**. The session persists in `adapter/session/`, which
is gitignored — keep it out of iCloud Desktop or Dropbox sync, because anyone
holding that directory can impersonate the whole WhatsApp account.

---

## 6. Link and use it

From your own phone, text the spare number:

```
link
```

You get a single-use link valid for ten minutes. Open it, paste the TOTP key
and secret, submit. The page tests them against Groww before storing anything;
on failure it shows Groww's actual error and stores nothing.

Then:

| You text | You get |
|---|---|
| `portfolio` | value, today's move, top 3 movers |
| `pnl` · `mera pnl kitna hai` | day change |
| `positions` | open F&O positions with P&L |
| `margin` | utilisation, headroom, SPAN/exposure |
| `nifty` · `market` | index basket |
| `reliance` · `nifty 25000 ce` | a quote |
| `gold` | a numbered choice — MCX vs NSE, it will not guess |
| `help` | what it can do |
| `unlink` | deletes credentials and history |

`why is silver up`, `what's my risk` and `explain IV` are routed to the LLM
path, which is **not built yet** — they currently get the out-of-scope line.

---

## Preflight

```bash
python scripts/doctor.py
```

Checks working directory, Python version, packages, `.env` permissions,
`CRED_KEY`, `LINK_SECRET`, `PUBLIC_BASE_URL` reachability, Redis, Postgres
schema, instrument master freshness, adapter pairing state, and Node. Every
failure prints the command that fixes it. Exit code 1 if anything blocks.

---

## When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| Bot silent, no errors | Baileys session died | `curl localhost:3001/health`. If `connected:false`, re-scan. The adapter exits non-zero on logout so it is loud. |
| Link says "expired" | Token is single-use, 10-minute TTL | Text `link` again. |
| Link page won't open on phone | `PUBLIC_BASE_URL` unreachable | It cannot be `localhost`. Use the LAN IP or a tunnel (§2). |
| Worker exits at startup | `CRED_KEY` missing or malformed | The error names the fix. `python -m app.auth.crypto >> .env`. |
| Everything 401s at once | TOTP clock skew | System Settings → General → Date & Time → set automatically. TOTP breaks on skew. |
| Numbers slightly off vs app | Basis logic on credit/debit legs | Re-run `scripts/reconcile.py` and check its basis verdict. |
| Messages arrive late or never | Worker not consuming | `redis-cli XLEN inbound` — if it grows and nothing is handled, the worker is down. |
| Bot stops overnight | Your Mac slept | Expected on a laptop. No overnight MCX coverage and no 06:00 token mint until this moves to an always-on box. |

---

## Known limits on a Mac

- **Sleep stops everything.** Fine while dogfooding. It becomes a real problem
  for Phase 2, which is why the spec calls for a Mumbai VPS with a static IP —
  the static IP is also mandatory for Phase 3 order placement.
- **A tunnel URL changes on restart**, so `PUBLIC_BASE_URL` needs re-setting.
  Only matters when linking a new account.
- **Baileys interactive messages** (buttons, lists) often do not render on
  personal accounts, so the adapter sends numbered plain text instead.

---

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
