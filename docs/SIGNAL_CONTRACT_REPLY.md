# Reply to Signal Handoff — rulings on A1–A10 and D1–D4

**To:** the insight-generation side
**Re:** your handoff against Market Signal Contract v0.1
**Contract now at:** v0.2 (`docs/SIGNAL_CONTRACT.md`) — §4.2 and §6.1 changed as a result of your §1. Diff summarised in §6 below.

Thank you for §1.6 in particular. Knowing that the raw magnitudes are computed and
then dropped rather than never computed changes several answers — **A1 is "stop
discarding", not "go build"**, and that reframing lets three items come off your
A8 list entirely.

Answers to the two you flagged are first. Then the one time-sensitive ask, then
everything else.

---

## 1. A9 — sentiment: **keep it, demoted. Do not remove it.**

I'm softening v0.1 §4.2, which said flatly that we don't want sentiment. That was
written before I knew how yours is derived, and it was too blunt. The ruling:

**Keep emitting `sentiment`. We will consume it as advisory only — never as a
gate, never as the sole source of direction.** Nothing to remove, nothing to
build.

The distinction that matters, and the one v0.1 failed to draw:

- **Sentiment about the instrument** — "this is negative for the company" — is a
  legitimate, largely objective claim. Yours is this.
- **Sentiment about the reader** — "this is bad news for you" — is not yours to
  compute, because it depends on which side of the position the reader is on. The
  same filing is bad for a holder and good for someone short a call against it.

v0.1 rejected the second and accidentally rejected the first with it.

**The one thing I do need, and it's a doc change not a build: tell us how
`sentiment` is derived, per insight type.** From the handoff I can see
`OiPositioningTextUtil.resolveSentiment` derives it mechanically from the buildup
class — that is a deterministic relabel of data we're already getting, and we can
trust it as such. For `NEWS_INSIGHT` I assume it's model-derived from article
text, which is a judgement we'd weight much more weakly. Without knowing which is
which, we have to treat every sentiment value as the weakest case, which wastes
the deterministic ones.

A one-column addition to your InsightType table — `sentiment_source:
derived | model | none` — upgrades the field for us at roughly zero cost.

**Where we'll actually use it:** as a corroboration check. When your
mechanically-derived sentiment disagrees with the direction we compute from the
raw magnitudes, that's a bug on one side or the other and we want to log it. A
field we can cross-check is worth more than a field we consume blindly.

**Same ruling applies to `rank`**, which v0.1 also rejected: keep it, we won't
order on it (our ordering is per-person and depends on portfolio data you don't
have and shouldn't), but there's no reason for you to strip it.

---

## 2. D1 — raw-only or dual-serve: **dual-serve. Do not build a second pipeline.**

Persist raw beside the existing prose. Keep your prose product exactly as it is —
it serves a surface we have nothing to do with, and killing it would be a product
regression we have no standing to ask for.

Two conditions, one hard and one architectural.

### The hard one: prose is never on our read path

We will not parse numbers out of `shortInsight`, at any quality level. This isn't
a judgement about your prose — it's that our layer enforces, mechanically, that
every number reaching a user traces back to a structured field from a typed
source. A figure extracted from generated text cannot satisfy that check, so for
our purposes prose isn't lower-quality data, it's **not data**. If the raw field
is absent we suppress the alert; we never fall back to reading the sentence.

Which means: **the raw fields are the contract. The prose is not.** If a value
appears in `short_insight` but not in a structured field, from our side it does
not exist.

One exception worth naming, because it cuts the other way: your **news and concall
summaries** (§1.6) are wanted, as long as they're extractive — sentences lifted
from the source. That's evidence, and we'll use it to answer "why are you telling
me this". A generated one-liner *about* the source is commentary, and isn't.

### The architectural one: persist raw *before* and *independently of* the LLM step

If generation is `compute raw → LLM writes prose → persist both`, then our
latency is hostage to your model call and a prose failure silently drops the whole
signal. Write the raw row first; attach prose when it's ready.

This is the highest-leverage change in D1 and it's small. It gets us the signal
sooner, it makes prose failures non-fatal, and it means a model outage degrades
your product without taking ours offline with it. It also makes A10 much less
pressing — see §5.

### What this does to your A1 estimate

Everything in your §1.6 is already computed. A1 becomes a schema change plus a
write, on values that exist in memory at the moment of the current write. It is
not a new extraction pipeline. I'd expect it to be the cheapest item on the list
relative to its value, which is why it's ranked first below.

---

## 3. The time-sensitive one — please read this even if nothing else lands

**Most of your insight types expire in 30 minutes. Are expired rows retained or
purged?**

If they're purged, then backfill (A4) cannot be built retroactively — the history
simply won't exist — and **the depth we can ever tune against is a function of
when retention starts, not when the read API ships.**

So, separately from and ahead of A4: **turn on retention this week, even with no
read path, no envelope, and no schema change.** Append the rows to cold storage,
a dump table, an S3 prefix, anything. Every week this waits is a week of tuning
data that can never be recovered.

We tune alert thresholds by replaying recorded signal days offline. Without
history, every threshold change costs a week of live market to evaluate, and the
whole thing takes a quarter instead of a fortnight. This one ask is worth more to
us than several of the A8 families combined.

---

## 4. A1–A10 rulings

| # | Ruling | Priority | Note |
|---|---|---|---|
| **A1** | **Build as specified** | **P0** | Cheapest item relative to value; §1.6 says it's already computed. Also load-bearing for dedupe — see A2 |
| **A2** | Build, but split | P0 / P2 | `event_at`+`observed_at` are P0. `absent[]`, `method`, `evidence` are P2 |
| **A3** | **Don't build** | — | We resolve identity. See D2 |
| **A4** | Build | P1 | But retention **now** — §3 |
| **A5** | Build | P1 | Cheap, and the failure it prevents is the one that hurts us most |
| **A6** | **Don't build** | — | Send raw, we derive. See below |
| **A7** | Build | P2 | `filled`/`filled_at` is the valuable half |
| **A8** | **Partial — 3 of 7 come off** | mixed | See table below |
| **A9** | **Keep, demoted** | — | §1 |
| **A10** | **Don't build push** | — | But see §5 — there's a cheaper thing we need instead |

### A2 — one problem that needs checking before `insightId` can be the dedupe key

`insightId` is a candidate for `source_event_id` only if it is **stable across
regenerations of the same underlying condition.** If `FrequentInsightGeneratorJob`
regenerates on a cadence matching the 30-minute expiry, then a sustained condition
— an OI buildup that persists for three hours — plausibly produces a fresh
`insightId` every cycle.

If so, deduping on it fails and the user is told the same thing six times. That
reads as broken faster than almost any other defect.

**Please confirm which it is.** If it's per-row rather than per-event, it's
recoverable on our side — we'd fingerprint on `(entity, insight_type,
state_class)` and collapse ourselves. But we can only do that **if A1 lands**,
because the state class has to come from the raw magnitudes. That dependency is
the second reason A1 is P0.

Also: `expiry_timestamp` is epoch **minutes**. Fine for expiry; please don't carry
that precision into `event_at`/`observed_at` — seconds minimum, RFC 3339 with
explicit offset.

### A6 — send raw, we'll normalise

Don't build time-of-day normalisation. `VolumeInsight{currentVolume,
volumeWeekAverage, volumeRatio}` already carries what we need: send
`currentVolume` with an accurate timestamp and keep `volumeWeekAverage` as-is.

We're building the intraday volume profile on our side regardless — we need the
same trailing candle data for σ — so normalising against it is marginal work for
us and a whole project for you. Just don't put an un-normalised ratio in a field
named `rel_volume`; `volumeRatio` under its own name, documented as
current-vs-week-average, is honest and useful.

### A8 — three of seven come off your list

| Family | Ruling | Why |
|---|---|---|
| `filing.fno_ban` | **Build — do this first** | Tiny daily payload, no computation, hard constraint for any F&O trader. Best value/effort in the whole document |
| `filing.results_calendar` | **Build** | Forward-looking, low volume, high value. We need it days ahead, not on the day |
| `filing.block_deal` / bulk | **Build** | Daily exchange file; explains a large share of single-stock moves. `client_name` as filed — we'll classify |
| news `category` + `role` | **Build** | `category` first, then subject-vs-mention `role`. `is_rumour` is optional |
| `market.macro` | Build, P2 | Needed for a pre-market brief. Mostly aggregation |
| `sigma_move` | **Don't build** | We derive from candles. Ours to calibrate anyway |
| `market.circuit` | **Don't build** | We source circuit bands and locked state from our own market feed in real time. Confirmed not on `master` — we're not planning against it |
| `market.iv` / rank | **Don't build yet** | We have an option-chain source carrying greeks and IV. We'll confirm it covers IV rank; if it doesn't we'll come back, but don't start |
| `filing.pledge` / `shareholding` | Later | As flagged |

Noted and understood on `CIRCUIT`/`PIVOT`/`MACD`/`TECHNICAL_CHANGE`/
`RELATIVE_STRENGTH` living only on the discarded branch. We are not planning
against any of them.

---

## 5. D2, D3, D4

### D2 — who resolves ISIN: **we do. Don't build A3.**

We already run an instrument master (~136k instruments) with fuzzy resolution,
dual-listing collapse and disambiguation, because we need it anyway to resolve
free-text user input. Adding ISIN plumbing through your stack duplicates that at
real cost to you.

Send what you already have — `nse_symbol`, `bse_code`, `groww_contract_id`,
`entity_id` — and we map. Two small asks:

1. **`exchange` and `segment` alongside the symbol.** A bare symbol is ambiguous
   across venues and segments in ways a fuzzy matcher shouldn't have to guess at.
2. **A stable `entity_id`**, so we can cache the resolution rather than re-running
   it per signal.

Your having both `nse_symbol` and `bse_code` actually handles dual listings more
directly than ISIN would. One question that follows: **is an insight emitted once
per entity, or once per listing?** It changes whether we collapse or not.

### D3 — backfill depth: **12 months ideal, 3 months floor**

Below ~3 months (≈60 sessions) there aren't enough sessions to separate a real
threshold from noise. 12 months matters because it covers at least one
high-volatility episode and a full set of monthly expiries — thresholds tuned only
on a calm market fire constantly in a volatile one, which is exactly when we can
least afford to be noisy.

If depth is expensive, **depth of the raw fields matters more than depth of the
prose.** Retaining raw-only history for 12 months is more useful to us than
retaining everything for 3.

And per §3: the achievable depth is set by when retention starts.

### D4 — poll vs push: **poll is fine. Don't build push. Build a since-cursor instead.**

Relaxing v0.1 §6.2. My 60-second targets were written for circuit and gap, and
both of those have now moved to our own market feed (see A8), so the tight-latency
items are off your critical path entirely. What remains on your side — news,
filings, OI, concall — has a natural floor in the minutes regardless of transport.

**Poll at 60s is acceptable for Leg 1.** Two things would make it work properly:

1. **A since-cursor endpoint — this is the real ask, and it's much cheaper than
   push.** `GET /signals?since=<cursor>` returning everything new across all
   entities in one call. Per-entity polling doesn't scale: watching N instruments
   means N requests per cycle, and it gets worse with every user. One changed-since
   firehose replaces all of it and removes the reason push existed.
2. **An uncached path, or a shorter TTL, for that endpoint.** A 5-minute public
   cache in front of a 60-second poll makes the poll interval decorative.

If the raw write lands before the LLM step (§2), effective latency should be good
enough on poll that push never needs building.

---

## 6. Contract v0.2 — what changed

Your handoff caused four edits to `docs/SIGNAL_CONTRACT.md`:

1. **§4.2** — "We do not want sentiment" replaced with the advisory ruling in §1,
   plus a request for `sentiment_source`. Same for `rank`.
2. **§6.1 / §6.2** — push downgraded from preferred to unnecessary; since-cursor
   polling added as the transport requirement; latency table revised to reflect
   circuit and gap moving to our own feed.
3. **§3** — ISIN demoted from "preferred identity" to "optional"; `exchange` +
   `segment` + a stable internal id promoted to required.
4. **§6.3** — retention called out as separable from, and more urgent than, the
   backfill read API.

The contract file remains the single source of truth. Where this reply and the
contract ever disagree, the contract is right and I've failed to update it — tell
me and I'll fix it rather than you working around it.

---

## 7. Open questions back to you

1. **Are expired insight rows retained or purged?** (§3 — most consequential)
2. **Is `insightId` stable across regenerations of the same condition, or per
   persisted row?** (A2 — determines whether we can dedupe at all)
3. How is `sentiment` derived per insight type — deterministic or model? (A9)
4. Can the raw write be decoupled from the prose LLM step? (D1)
5. Is an insight emitted once per entity or once per listing? (D2)
6. Does anything today carry a **market event time**, as distinct from the row's
   `created_at`? (A2)
7. Is the 5-minute cache on the read endpoints configurable per-client? (D4)
8. What's the actual universe — all NSE/BSE cash, an index subset, SME in or out?
   (A5's coverage manifest; partial coverage is workable, undocumented partial
   coverage isn't)

---

**Net effect on your scope:** A3, A6, and three of seven A8 families come off.
A1 shrinks from a pipeline to a schema change. Push is not required. What's added
is small and mostly not code — retention switched on now, a since-cursor read, a
derivation column on the sentiment doc, and an answer on `insightId` stability.
