# Working in this repo

GR-2 is a WhatsApp number one Groww user texts for answers about their own
portfolio. It is live against a real brokerage account. Read `README.md` for the
architecture; it is one page and has the diagrams.

## Before you start

```bash
make doctor          # prerequisites, and it names the fix for each failure
make test            # 480 unit tests, hand-written fakes, no network, no DB
make triage          # the running system: which of the four hops is broken
```

Three processes, three terminals: `make api HOST=0.0.0.0`, `make worker`,
`cd adapter && npm start`. Details and the troubleshooting table are in
`docs/QUICKSTART.md`.

## The five invariants

These are enforced in code, not asked for in prose. Do not weaken one to make a
feature work; if a feature needs one weakened, that is the finding to report.

1. **Numbers never originate in a model.** Every figure in an outbound message
   traces to a tool result — checked in `app/compose/guard.py`.
2. **A model is never in the detection loop.** What to alert about is decided by
   arithmetic (`app/watcher/`). Models interpret and phrase; they never notice.
3. **The chat channel is a surface, never a credential.** No downstream service
   receives a Groww token or a TOTP secret.
4. **TOTP, not the API-key flow** — the only reason an unattended worker is
   possible.
5. **Fail loudly.** A missing quote or a stale source produces a visible error,
   never a plausible message built on stale data.

Plus one house rule: describe, do not prescribe. Advice-shaped phrasing is a
regulatory boundary and is checked in `app/compose/voice.py`.

## Conventions

- Tests use hand-written fakes, not a mocking library. See
  `tests/test_dispatch.py::FakeGroww`.
- `ruff check` and `ruff format` on the files you touched. The repo is not
  globally format-clean; do not reformat files you did not change.
- Anything that talks to Postgres gets a check in `scripts/smoke_db.py`. Fakes
  have missed real bugs that the smoke test caught.
- A feature that integrates an external system is gated on an env var, and with
  that variable unset the system behaves exactly as it did before. There is a
  test asserting it.

## Current work

**Connecting open questions to R2D2's `/response` API** — start at
`docs/INTEGRATE_R2D2.md`. The seam is built; in the normal case one function
changes.

Not in scope: GR-1 insight polling and proactive WhatsApp alerts. That code
exists under `app/watcher/` and is deliberately unwired.
