# Groww desk

A WhatsApp number you can text to ask what your money is doing — holdings,
positions, orders, margin and live prices — answered in under six lines with
the right number.

Phase 1 (reactive: you ask, it answers) is the scope here. Phase 2 makes it
proactive: a watcher daemon holds state, detects deltas, and a gate decides
whether it is worth interrupting you.

## The invariants

Five rules the build is organised around. Breaking any one produces a system
that either breaks or lies.

1. **Numbers never originate in the LLM.** Every rupee figure, quantity,
   strike and percentage is rendered from a typed object returned by a tool.
   The model writes prose around numeric slots; it never emits a digit.
2. **The LLM is not in the detection loop.** Polling, diffing and rule
   evaluation are plain Python. The model interprets and phrases; it never
   notices.
3. **The chat channel is a surface, never a credential.** An inbound phone
   number authenticates nothing. Tokens live server-side.
4. **TOTP auth, not the API-key flow.** The API Key & Secret flow needs a
   human clicking approve on the Groww console every morning. TOTP can be
   automated, which is the only reason an always-on agent is possible.
5. **Fail loudly.** A missing quote or an expired token produces a visible
   error, never a plausible-looking message built on stale data.

## What runs today

```
app/
├── config.py            settings, rate-limit table, log redaction
├── infra.py             cache, per-type-group token bucket, circuit breaker
├── channel/             Channel protocol + console and Baileys implementations
├── auth/                AES-256-GCM credential envelope, TOTP token broker
├── tools/
│   ├── types.py         typed objects every tool returns
│   ├── pnl.py           the computation layer — see below
│   ├── groww.py         cached, rate-limited, batched SDK wrappers
│   ├── instruments.py   instrument master + resolver
│   └── aliases.py       normalisation, Hinglish and commodity aliases
├── render/templates.py  Indian digit grouping, six-line message templates
├── router/fastpath.py   regex intent classifier, 22 intents off the model
├── market/calendar.py   NSE/BSE/MCX hours, holidays, staleness labels
├── dispatch.py          route -> tools -> typed object -> template
├── worker.py            dedupe, debounce, typing indicator, traces
├── main.py              FastAPI: health, the signed link page, webhook
└── repl.py              drives the whole pipeline over stdin
```

Not built yet: the LLM path (`agent/`), charts, the Baileys Node process, and
all of Phase 2 (`watcher/`).

## Why P&L is computed here and not read from Groww

Groww's holdings payload has **no LTP and no current value**. Its positions
payload has `realised_pnl` but **no unrealised P&L and no LTP**. So every P&L
number this bot shows is computed in `app/tools/pnl.py` by joining position
state to a live quote.

That makes the arithmetic yours, and therefore yours to get wrong. Things that
bite:

- **Holdings carry no exchange field.** The `NSE_RELIANCE` key for `get_ltp`
  has to be rebuilt from the instrument master. Default NSE, fall back to BSE,
  warn if neither resolves.
- **F&O quantities are units, not lots.** Do not multiply by lot size for P&L.
  Use lot size only to display "1 lot" and to validate quantities.
- **Pledged quantity still counts toward value** but is not freely sellable.
- **`credit_price` / `debit_price` are ambiguous** — per-unit average, or
  whole-leg notional? The docs do not say, and getting it backwards scales
  every F&O number by the lot size. It is isolated behind `Basis` and
  **unresolved until you run the reconciliation**.

```bash
pip install growwapi pyotp pydantic pydantic-settings httpx   # or: make deps-reconcile

export TOTP_TOKEN='your-api-key'
export TOTP_SECRET='your-totp-secret'

python scripts/authcheck.py     # credentials only — run this first
python scripts/reconcile.py     # the actual comparison
```

`authcheck.py` tests nothing but the token mint, and names the fix for each
common failure (secret not base32, clock skew, wrong auth flow). Reconciliation
loads a 136k-row master before it touches the network, so isolating auth keeps
a credential problem from surfacing late and tangled up in other output.

Neither needs a database, a `.env`, or Redis — that is why the dependency list
above is shorter than `pip install -e .`. `reconcile.py` downloads the
instrument master itself if it is absent.

It prints both basis conventions side by side against your real book. Pin the
one that matches the app in `pnl.py:DEFAULT_BASIS`.

**This is the Phase 1 gate.** Portfolio value must match the Groww app to the
rupee before a single alert gets built on top of it.

## The instrument master

`data/instruments.csv` (136,779 rows) is the source of truth for symbols,
lot sizes, expiries and exchange tokens. Refresh it daily at 07:30 IST:

```bash
make instruments
```

The real schema differs from what you might assume: `underlying_symbol`,
`expiry_date` and `strike_price` naming; a structured `groww_symbol`
(`NSE-NIFTY-15Sep26-19550-CE`) that is far easier to match against than
`trading_symbol` (`NIFTY2691519550CE`); futures rows carrying a strike
sentinel of `-0.01` or `0` rather than null; and NSE running its own
`COMMODITY` segment alongside MCX.

Resolution rules that real data forced (`app/tools/instruments.py`):

- **Dual-listed equities collapse on ISIN.** RELIANCE on NSE and BSE is one
  instrument quoted twice, not an ambiguity worth asking about.
- **Commodity aliases never reach the equity table.** NSE lists an equity
  called SILVER and one called GOLD1. "chandi" means the metal, always.
- **GOLD stays ambiguous** across MCX and NSE — those are genuinely different
  contracts — until the user's own book breaks the tie.
- **A strike off the ladder offers neighbours** rather than nothing.
- **Never guess on a close call.** If the top two candidates are within 15
  points, send a list message.

Lot sizes come from the master and only from the master. NIFTY is 65 today.

## Running it

**Full setup, fresh clone to texting your own number: [docs/RUNBOOK.md](docs/RUNBOOK.md).**
`python scripts/doctor.py` checks every prerequisite and prints the command
that fixes each failure.

```bash
cp .env.example .env && chmod 600 .env
python -m app.auth.crypto >> .env        # generates CRED_KEY

make up            # postgres, redis, api, worker
make instruments   # seed the instrument master
make test          # 176 tests
make eval          # golden set: intent accuracy, numeric exactness
```

To drive the real pipeline without WhatsApp:

```bash
TOTP_TOKEN=... TOTP_SECRET=... python -m app.repl
```

## Evaluation

Three layers (`eval/run.py`), gated before every deploy:

| Layer | Target |
|---|---|
| Intent accuracy | >95% |
| **Numeric exactness** | **100% — any failure is a ship-blocker** |
| Prose quality (LLM-as-judge) | mean >4.2, pending the LLM path |

Numeric exactness asserts that rendered figures equal figures computed
independently from frozen fixtures. It is the layer that matters: a finance
assistant that is occasionally wrong about a number is worse than none.

## Credentials

Collected on a signed, single-use, ten-minute link page — never pasted into
the WhatsApp chat, because WhatsApp backups are not under your control. They
are tested against Groww before anything is written, then stored AES-256-GCM
encrypted with a key that lives outside the repo. Logs redact anything
token-shaped.

The Baileys WhatsApp session directory is a bearer token for the whole
assistant. Keep it on a dedicated number, on a box you control, gitignored
(it is) and encrypted at rest.

## Rate limits

Limits are **per type-group**, shared across every API in the group:

| Group | /sec | /min |
|---|---|---|
| Authentication | 5 | 30 (+150 per 24h on the token endpoint) |
| Orders | 10 | 250 |
| Live Data | 10 | 300 |
| Non-Trading | 20 | 500 |

At ~30 users the binding constraint is Live Data. `get_ltp` and `get_ohlc`
take up to 50 instruments per call — one batched call for the whole
portfolio, never one per holding. The token bucket in `app/infra.py` is
shared across processes via Redis; when it runs dry, serve cache with a
staleness label rather than erroring.

## Open questions

Carried from the spec, plus what the SDK and the instrument master settled:

| # | Question | Status |
|---|---|---|
| 1 | MCX portfolio parity | Master confirms MCX instruments under `segment=COMMODITY`; live positions still need checking |
| 2 | Order list method name | **Settled**: `get_order_list(page, page_size, segment, timeout)` |
| 3 | Trading API subscription cost per account | Open |
| 4 | Margin utilisation definition | Open — `make reconcile` |
| 5 | Access token TTL under TOTP | Open — measure, then halve it for the broker |
| 6 | Physical delivery flags | Open — needed by the Phase 2 expiry rule |
| 7 | Baileys stability | Open — run a week on a spare number first |

One more the SDK settled: `GrowwAPI.get_access_token` is annotated `-> dict`
but returns the bare token string. `app/auth/broker.py` accepts both shapes.

## Definition of done

> You have used it as your primary way of checking your book for five
> consecutive trading days and have not once opened the app to verify a
> number it gave you.

Phase 2 does not start until that is true.
