# GR-2: a markets copilot on WhatsApp

**For:** engineers joining or maintaining this.
**State:** briefs work end to end; intraday alerts are built but not fed. 442 tests.
**Code:** branch `claude/groww-api-integration-s7f6ey`. Setup: `docs/QUICKSTART.md`.

---

## The problem

A Groww user with money in the market opens the app several times a day. Most of
those checks find nothing. The few that matter — a margin band tightening, a
short option going in the money the day before expiry, a block deal in a stock
that is a third of their book — are the ones they miss, because the moment that
matters is rarely the moment they happen to look.

GR-2 is a WhatsApp number they can text, which also texts them first when
something in their own book changes. Order placement is out of scope. Insight
does all the work.

## Why we did not just ask a model

The obvious build is to poll the market, hand everything to an LLM, ask "is this
important to this user?", and forward whatever it says yes to. That is about
fifty lines. It is what most assistants in this category do.

Midway through the project we found the evidence for not doing it. OpenPoke is an
open-source implementation of Poke, a well-regarded messaging assistant. Its
entire interrupt decision is one LLM call with a prose prompt returning a
boolean. On a public benchmark of 71 assistants, Poke scores 7 out of 10 on
proactivity and 2 out of 10 on restraint — the lowest on the board. Those two
numbers together are the lesson. Being proactive is easy. Being proactive without
becoming noise is the product, and a model asked "is this important?" cannot know
that this stock is 0.4% of your money and that one is 34%.

The same benchmark names the failure exactly. Its restraint task scores a 3 for
an assistant that "acts on everything, **or on nothing**." Silence and spam are
the same failure. Going quiet is not the safe direction.

## Five rules

Set before the first line of code. Nothing has been allowed to break them.

**Numbers never come from the model.** Every figure in an outbound message is
rendered from a typed broker result. Enforced, not trusted: `NumericGuard` pulls
every numeric token out of a finished message and requires each to trace to a
tool value. A message that fails is not sent. It has already caught a hardcoded
constant in one of our own templates.

**The model is not in the detection loop.** Polling, diffing and scoring are
plain Python. A model is too slow for a breached strike and non-deterministic on
the thing that must be deterministic.

**The chat channel is a surface, never a credential.** A phone number
authenticates nothing. Broker credentials are collected on a web page, encrypted
at rest.

**Fail loudly.** A missing quote is named as missing, never zeroed. A plausible
wrong number costs trust permanently; an absent one costs one alert.

**Describe, do not prescribe.** GR-2 says what moved and what it did to your
position. Never buy, sell, book or exit. That is a regulatory line, so it is
checked in code like the numbers are.

## How it works

A **Node adapter** owns the WhatsApp session and nothing else. Inbound goes onto a
Redis stream; outbound over an HTTP surface bound to loopback, because that
endpoint can make the account message anyone.

A **Python worker** consumes the stream and runs three more loops in-process: the
brief scheduler, the outbox drainer, and a nightly instrument refresh.
**Postgres** holds the linked account, an append-only ledger, the outbox and the
suppression log.

The **decision pipeline** is the part to understand. A signal arrives. It is
joined against that user's exposure. Rules turn it into triggers. A gate scores
each and routes it to one of four outcomes — interrupt, batch, evening digest, or
silent. Survivors are composed from templates, checked, queued, sent.

The join is the product. An external engine can say TITAN fell 3.2% on four times
normal volume. Only we know the user holds 40 shares at ₹2,618, that it is 34% of
their book, and that 3.2% is unremarkable for them. The same signal is an
interruption for one person and noise for another.

## Product calls

**Split at the signal boundary.** Movers, volume, breakouts, news and filings come
from an external engine over a written contract. Margin, expiry, structure, delta
and P&L come from us, because no external source sees a user's book. Two terms in
that contract matter: do not rank for us, because ranking is per-person; and send
an explicit null rather than a zero when you cannot compute something.

**The gate scores, it does not threshold.** Seven multiplicative stages —
severity, materiality against this user's typical position, time criticality,
irreversibility, confluence with other signals on the same name, learned
responsiveness, fatigue. Every stage is recorded, because tuning needs to know
which stage let a thing through, not just that the total was 0.78.

**Silence is a decision and we show it.** The evening wrap says "Held back 6
things today." If a user routinely asks to see them, the gate is too tight — a
measurement rather than a guess.

**Shadow mode first.** The pipeline runs complete and sends nothing for a week.
Every would-be alert is scored and logged to a local file. Alerting products get
one chance.

**Two bookend briefs carry the habit.** Pre-market and post-close, the only
predictable messages, which is what earns the right to interrupt at other times.
Both send nothing rather than filler when there is nothing to say.

**Users can turn it down.** `pause`, `snooze 2h`, `brief at 8:15`. A small closed
grammar parsed before classification, because "pause" must mean pause on the
first try. Someone who cannot turn briefs down turns them off, and takes the
useful messages with them.

## Engineering decisions

**One process, not two.** An earlier design had a separate watcher. We dropped
it: the argument for splitting was contention between a live feed and the broker
thread pool, and we have neither a live feed nor enough users for it to bite. The
seam to split later is intact.

**Identity anchors on the brokerage account, not the phone number.** WhatsApp can
address one human as a phone number, an opaque LID, or either with a device
suffix, and can switch mid-conversation. Those forms share no digits, so no
string handling relates them. Left alone, one user becomes two rows — two books,
two budgets. Two addresses that link to the same broker account are provably one
person, so we merge there.

**We test against a real Postgres, not only fakes.** Unit doubles are right for
policy and blind to SQL. Running `scripts/smoke_db.py` against a throwaway
database has caught three real bugs, including one where a re-run superseded the
only pending alert row and inserted nothing — a message vanishing silently,
inside the component whose whole job is not losing messages.

**We wrote the restraint test from the public benchmark.** One evening, three
signals, one of which should reach you. It failed on its first run and showed
that a *critical* margin band could never qualify for priority override, so it
would have been deferred to the morning digest if it arrived in quiet hours. By
morning the position may be gone. No existing test would have found that.

## Risks and open items

- **Intraday alerts do not fire.** Rules, gate and composer are built and tested;
  nothing polls the signal engine yet. Briefs work.
- **F&O numbers are unverified.** `DEFAULT_BASIS` assumes one of two conventions.
  One `make reconcile` run against a live book settles it. Do this before anyone
  else sees numbers.
- **Holidays are incomplete.** A brief will fire on Diwali. Five-minute fix.
- **Credentials are broader than we would like.** The automatable broker auth
  grants full account access. We constrain it by convention — the tool layer is
  read-only — not by scope, because no scoped grant exists.
- **Security was deliberately deferred** to reach dogfooding. The link page runs
  over plain HTTP on a LAN address. Fine for ten internal users, not beyond.
- **The gate's numbers are a first guess.** We cannot self-score restraint
  credibly; the person who built the gate is the worst judge of it. Shadow mode
  and ten dogfooders are the instrument.

## Next

Wire the signal-engine poll. Run reconcile. Fill in the holidays. Then a week of
shadow mode, reading the report each evening before it is allowed to speak.
