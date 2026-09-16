# Integrating R2D2 — read this before touching anything

**You are connecting GR-2 (this repo, a WhatsApp desk for one Groww user) to
R2D2's `/response` API, so that open questions get real answers.**

The seam is already built and tested. In the normal case you change **one
function**. This document tells you which one, what to look for in
`ml-r2d2-backend`, and which four things will break the product if you get them
wrong.

Read `README.md` first if you have not — it explains what GR-2 is in one page.

---

## 1. What already works, and what this adds

GR-2 answers `portfolio`, `positions`, `margin`, `orders`, a bare stock name and
about twenty other things from typed templates, with no model involved. Those
are fast, exact, and not your problem.

Anything else — "why is TITAN down", "am I over-exposed to metals", or a typo
like "equity positios" — classifies to `Path.LLM` in
`app/router/fastpath.py:149` and today gets a redirect line from
`app/dispatch.py:97`. **That redirect is what you are replacing.**

```
WhatsApp → worker → classify() ─ Path.FAST ──→ typed template   (unchanged)
                               ├ Path.REJECT → refusal line     (unchanged)
                               └ Path.LLM ───→ R2D2 /response   (you)
                                                 └ withheld? → the old line
```

## 2. The seam

Three places. You should only need the first.

| What | Where | Change? |
|---|---|---|
| Map R2D2's response onto `Answer` | `app/agent/r2d2.py:183` `parse_consolidated` | **yes, probably** |
| Decide if the answer is fit to send | `app/agent/answer.py` `Answerer._vet` | no — see §4 |
| Call it from the worker | `app/worker.py:197` | no |

Supporting files you should not need to open: `app/agent/shape.py` (markdown →
WhatsApp), `app/agent/context.py` (what R2D2 is told about the asker),
`app/worker.py:433` (constructs the client when `R2D2_BASE_URL` is set).

**Everything is inert until `R2D2_BASE_URL` is set.** With it empty, GR-2
behaves exactly as it does today; `tests/test_answer.py` asserts that. So you can
land your changes safely before R2D2 is reachable.

## 3. What to find in `ml-r2d2-backend`

A checklist. Most of it maps to constants at the top of `app/agent/r2d2.py`.

1. **The `/response` route** — request schema. GR-2 posts `{"query": ..., **context}`
   and adds `"stream": true` when streaming. If the field is not `query`, change
   it in `R2D2Client.ask`.
2. **Auth** — header name and token. Set `R2D2_AUTH_HEADER` / `R2D2_AUTH_SCHEME`
   in `.env`; no code change.
3. **Tool results in the response, and under which key.** This is the important
   one. See §4.
4. **The consolidated object** — the field holding the final prose
   (`_TEXT_KEYS`), the field holding the tool list (`_TOOLS_KEYS`), and one tool
   entry's shape (`_TOOL_NAME_KEYS`, `_TOOL_OUTPUT_KEYS`).
5. **Whether it takes a user identity**, and in what form. See §5.
6. **The MCP release URL** to set in *R2D2's own* `.env`. GR-2 does no MCP work,
   now or later.

## 4. Tool results are the whole integration

**Invariant I1: every number GR-2 sends traces back to a tool result.** It is
enforced, not requested, in `app/compose/guard.py` — the message is scanned for
digits and any that do not match a tool result cause the message to be withheld.
This is the product's core promise and the reason a broker's customer can trust
the thing.

R2D2's figures come from its own MCP tools. GR-2 never saw those calls. So
`parse_consolidated` collects the tool **outputs** into `Answer.facts`, and
`Answerer` uses them as the pool the guard checks against.

```
answer.facts = {"get_quote_0": {...}, "get_holdings_1": {...}}   ← tool outputs, verbatim
guard pool   = every number reachable in that structure
```

`app/compose/guard.allowed()` already walks nested dicts and lists, so put the
tool output in unmodified. It compares at the precision shown, so `3.2` traces
to `-3.1978`, and a fraction may render as a percentage.

**If `/response` does not return tool results**, say so in your PR rather than
working around it. The consequences, in order of preference:

1. **Best:** add them. Even tool name plus output JSON is enough.
2. **Workable:** GR-2 pre-fetches the user's book and passes it as
   `local_facts` (`Answerer.reply` already takes it, and
   `tests/test_answer.py::test_local_facts_widen_the_pool` covers it). Answers
   grounded in the user's own numbers pass; freely recalled market figures are
   withheld.
3. **Never:** `R2D2_STRICT_NUMBERS=false`. It exists so one operator can confirm
   the pipe works end to end before the numbers are trustworthy. It logs every
   figure it lets through. It must not be on when anyone else is using the bot.

An answer containing no digits needs no tool results and always passes.

## 5. Identity, and the thing that will cost you an hour

For R2D2 to answer "how is *my* portfolio doing", its MCP tools need to know
which Groww account is asking.

**GR-2 does not store the Groww user id.** `app/main.py:135` deliberately HMACs
it into `account_fingerprint`, so a database leak cannot expose Groww account
identifiers, and the hash is one-way. Do **not** add a column to get it back.

Fetch it at request time from the already-authenticated client
(`get_user_profile`; the key names Groww has used are listed at
`app/main.py:121`) and cache it in memory. The hook is already there:

```python
Worker(..., identity_for=async_callable)   # (user_id) -> str | None
```

Wire it in `app/worker.py:main()` next to where the `Answerer` is built. Nothing
is persisted and no schema changes.

**Never send R2D2 the TOTP secret, the API key, or a minted access token.** The
chat channel is a surface, not a credential (invariant I3), and that holds for
every downstream service too.

## 6. Streaming

Already decided, already built: **the stream is consumed and the tokens are
thrown away.** WhatsApp sends one message and Baileys renders edits poorly, so a
progressive answer has nowhere to go. What the stream is for is liveness — an
agentic call can run thirty seconds and WhatsApp drops a typing indicator after
about ten, so without it the user watches typing stop and assumes the bot died.
`app/worker.py` refreshes typing every eight seconds for the whole wait.

Both server-sent events and newline-delimited JSON parse. If streaming turns out
to be awkward, set `R2D2_STREAM=false` for a single blocking POST — everything
after "we have an answer" is identical.

## 7. Run order

```bash
# 1. R2D2 locally, its own .env pointing MCP at the release URL
# 2. capture one real response over the synthetic fixture
curl -s -X POST "$R2D2_BASE_URL/response" \
  -H 'content-type: application/json' -H "Authorization: Bearer $R2D2_API_KEY" \
  -d '{"query":"why is TITAN down today"}' \
  | python -m json.tool > tests/fixtures/r2d2/response.json

make test          # test_r2d2.py now runs against the real shape
```

A shape mismatch fails there, with the key names printed, instead of silently
withholding in production. Fix `parse_consolidated` until it passes, then:

```bash
# 3. .env: R2D2_BASE_URL, R2D2_API_KEY
make doctor && make triage
make worker        # restart terminal 2 only
```

Then text the bot `why is TITAN down today` and `equity positions`. The first
should answer with figures you can check against the app. The second should
answer as positions, not a refusal.

Watch the worker log:

```
r2d2 2140ms tools=3 grounded=True sent=True
```

`grounded=False` means no tool results — go back to §4. `sent=False` means the
answer was withheld, and the line above it says which figure or which phrase.

## 8. Do not

- Parse numbers out of prose that has no matching tool result.
- Relax `app/compose/guard.py` to make answers flow.
- Put R2D2 in the alert path. Alerts are decided by arithmetic (invariant I2);
  that is out of scope here and is a separate piece of work.
- Send R2D2 any credential.
- Run two adapter processes, or downgrade `baileys` below 7. Version 6.x cannot
  address a LID chat and fails with error 463 — messages are accepted and never
  delivered. That cost a full day. `docs/QUICKSTART.md` has the table.

## 9. Out of scope

GR-1 insight polling, the watcher tick, beliefs, proactive alerts on WhatsApp.
All designed, partly built (`app/watcher/`), and deliberately not wired. The
next leg, not this one.
