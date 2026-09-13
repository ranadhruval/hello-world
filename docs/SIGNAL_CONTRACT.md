# Market Signal Contract v0.2

**For:** the team/agent building the insights generation engine
**From:** the team building the personalisation and delivery layer
**Status:** revised after your first handoff. Rulings and rationale in
`SIGNAL_CONTRACT_REPLY.md`; this file is the source of truth where the two differ.

**Changed in v0.2:** sentiment and rank accepted as advisory (§4.2) · ISIN demoted
to optional, exchange+segment+internal id now required (§3) · push no longer
required, since-cursor polling is the transport ask (§6.1–6.2) · circuit, gap, IV
and σ moved to our own market feed (§4.1) · retention separated from backfill and
made urgent (§6.3).

---

## 1. What we're building, and where you fit

We're building a personal markets copilot for Indian retail investors. It watches
an individual's actual portfolio and reaches out — unprompted, conversationally,
in real time — when something has happened that matters *to that person*.

Your engine produces the market-side truth: what moved, why, and on what evidence.
Our layer joins that against a specific person's holdings, positions, history and
stated preferences, decides whether it is worth interrupting them for, and writes
the message.

The division is deliberate and we'd like to hold it firmly:

| Concern | Owner |
|---|---|
| Detecting that something happened in the market | **you** |
| Quantifying it — magnitude, levels, volumes, evidence | **you** |
| Attributing it to a cause — news, filings, corporate actions | **you** |
| Identifying *which instrument* it happened to | **you** |
| Deciding whether a given person cares | us |
| Ranking, scoring, urgency, interruption | us |
| Any user-facing prose | us |

### Things we'd ask you *not* to build

Not because they're bad, but because they'll be discarded and we'd rather you
spend the time on coverage and latency:

- **Don't rank or score for us.** A "top 10 movers" list is less useful to us than
  the raw set with magnitudes, because our ranking is per-person and depends on
  data you don't have. Send us everything above a low objective threshold and let
  us filter.
- **Don't personalise.** You have no portfolio context and shouldn't need any. We
  never send you a user's holdings.
- **Don't decide urgency.** "Critical"/"high"/"low" flags will be ignored. Send
  magnitude and evidence; urgency is a function of exposure.
- **Don't write prose.** No headlines-for-humans, no summaries-for-display, no
  "what this means" copy. Structured fields only. (An extractive summary of a news
  article is fine and wanted — that's data. Generated commentary is not.)
- **Don't deduplicate across sources on our behalf** if it means dropping
  evidence. Send both with their source ids; we'd rather see corroboration than
  lose it.

---

## 2. Five principles the contract is built on

These matter more than any individual field. If a field spec and a principle
conflict, the principle wins and we should talk.

**1. Missing is a value.** If you don't have `rel_volume`, send `null` and, where
you can, a reason. Never send `0`, never interpolate, never substitute a related
metric silently. We build user-visible claims on these numbers and a plausible
wrong number is far worse for us than an absent one — an absent field costs us one
suppressed alert, a wrong one costs us the user's trust permanently. This is the
single most important rule in this document.

**2. Every number must be corroborable.** We independently cross-check a subset of
what you send against exchange data. Numbers should be derived from a stated
source, not estimated or smoothed. Where you *do* derive something (a normalised
volume ratio, a computed percentile), say so in `method` so we can reproduce it.

**3. Events need stable identity.** We will receive the same event more than once —
redelivery, reconnection, overlapping sources. Every signal carries a
`source_event_id` that is stable for the same real-world event across redeliveries,
and we dedupe on it. Getting this wrong means users get told the same thing twice,
which reads as broken.

**4. Two timestamps, always.** `event_at` (when it happened in the market) and
`observed_at` (when your system saw it). We need both: the gap is how we measure
staleness, and staleness is how we decide whether an alert is still worth sending.
All timestamps RFC 3339 with explicit offset. Assume nothing about IST.

**5. Silence must be distinguishable from breakage.** If a source produces nothing
for an hour, we cannot tell whether the market was quiet or your feed died. See
§6.4 — we need a heartbeat.

---

## 3. The envelope

Every signal, regardless of kind, has this shape:

```jsonc
{
  "schema_version": "0.1",
  "signal_id":      "uuid",            // unique per delivery
  "source_event_id": "bse:ann:1234567", // STABLE per real-world event; dedupe key
  "source":         "bse_announcements",
  "kind":           "filing.announcement",

  "event_at":       "2026-09-15T11:04:12+05:30",
  "observed_at":    "2026-09-15T11:05:41+05:30",

  "entity": {
    "isin":           "INE280A01028",   // preferred identity, when it exists
    "exchange":       "NSE",            // NSE | BSE | MCX
    "segment":        "CASH",           // CASH | FNO | COMMODITY | INDEX
    "trading_symbol": "TITAN"
  },

  "payload":  { /* per-kind, see §4 */ },
  "evidence": { /* optional, see below */ },
  "absent":   ["rel_volume"],           // fields you could not populate
  "method":   { "rel_volume": "cum_vol / median(cum_vol, same_minute, 20d)" }
}
```

**On `entity`.** *(Revised in v0.2 — ISIN is no longer requested.)* We resolve
identity on our side through our own instrument master, which we run regardless in
order to resolve free-text user input. **Do not build ISIN plumbing for us.**

Send the internal identifiers you already have — `nse_symbol`, `bse_code`,
`groww_contract_id`, `entity_id` — and we map. Two things are required rather than
optional, because a fuzzy matcher shouldn't have to guess at them:

- **`exchange` and `segment`** alongside the symbol. A bare symbol is ambiguous
  across venues and segments.
- **A stable internal id** (`entity_id`), so we cache the resolution instead of
  re-running it per signal.

For derivatives, include `underlying`, `expiry`, `strike` and `option_type` in the
payload. A symbol that doesn't match exactly is recoverable; an ambiguous one
often isn't.

**On `evidence`.** Optional but valuable: the raw values behind a derived claim
(the trailing volumes behind a spike ratio, the price series behind a breakout).
When a user asks "why are you telling me this", we want to answer from data rather
than restate the claim.

**On `absent`.** An explicit list beats null-checking every field, and it lets us
detect a source that has quietly stopped populating something. We'll report back
on this — see §7.

---

## 4. Signal catalogue

Grouped by family. Leg 1 is what we need first; **Later** marks things we'd like
you to keep in mind for roadmap but don't need yet.

### 4.1 `market.*` — price and volume action

> **Withdrawn in v0.2 — do not build these.** `market.circuit`, `market.gap`,
> `market.iv` (incl. rank/percentile) and the `sigma_move` field are now sourced
> from our own market feed, which carries circuit bands, locked state, open
> interest and option greeks in real time. Their specs are kept below only so the
> field semantics stay documented if we ever hand them back. `market.volume_spike`
> is **still wanted**, but send raw values rather than a normalised ratio — see
> §5.1.

#### `market.mover`
Large intraday moves, whole tradeable universe.

| Field | Type | Notes |
|---|---|---|
| `ltp` | number | |
| `prev_close` | number | the reference the move is measured against |
| `pct_change` | number | signed |
| `abs_change` | number | signed |
| `sigma_move` | number\|null | move ÷ trailing realised daily σ. **Very high value to us** — see §5.1 |
| `sigma_lookback_days` | int | if `sigma_move` present |
| `universe` | string | e.g. `nifty500`, `fno`, `all_nse` |

Send everything beyond an objective floor (suggest ±2% or ±1.5σ, whichever is
looser). Don't truncate to a top-N.

#### `market.volume_spike`
**The most commonly-botched signal in this contract. Please read §5.1.**

| Field | Type | Notes |
|---|---|---|
| `rel_volume` | number\|null | **time-of-day normalised.** Un-normalised is unusable |
| `volume` | number | cumulative today at `event_at` |
| `baseline_volume` | number | what normal looks like at this time of day |
| `window_minutes` | int | measurement window |
| `price_change_pct` | number | volume without direction is ambiguous |

#### `market.breakout`

| Field | Type | Notes |
|---|---|---|
| `level_type` | enum | `52w_high`\|`52w_low`\|`Nd_high`\|`Nd_low`\|`range_high`\|`range_low` |
| `level_value` | number | the price actually broken — not just a flag |
| `lookback_days` | int | |
| `ltp` | number | |
| `volume_confirmed` | bool\|null | |
| `prior_touches` | int\|null | how many times this level held before |

#### `market.gap`
At open only.

| Field | Type | Notes |
|---|---|---|
| `prev_close` | number | |
| `open` | number | |
| `gap_pct` | number | signed |
| `filled` | bool | send an update when it fills — same `source_event_id` |
| `filled_at` | timestamp\|null | |

#### `market.circuit`

| Field | Type | Notes |
|---|---|---|
| `direction` | enum | `upper`\|`lower` |
| `band_pct` | number | the applicable band |
| `band_price` | number | the actual price level |
| `is_locked` | bool | locked vs merely touched — materially different |
| `unlocked_at` | timestamp\|null | |

#### `market.oi` — open interest, F&O
Per contract or aggregated per underlying; tell us which.

| Field | Type | Notes |
|---|---|---|
| `oi` | number | |
| `oi_change_pct` | number | vs previous session close |
| `price_change_pct` | number | |
| `buildup_class` | enum\|null | `long_buildup`\|`short_buildup`\|`short_covering`\|`long_unwinding` — see §5.3 |
| `max_oi_call_strike` | number\|null | |
| `max_oi_put_strike` | number\|null | |
| `pcr` | number\|null | say whether OI-based or volume-based in `method` |

#### `market.iv` — implied volatility, F&O

| Field | Type | Notes |
|---|---|---|
| `iv` | number | |
| `iv_change_pct` | number | session-relative |
| `iv_rank` | number\|null | **more important than `iv` itself** — see §5.4 |
| `iv_percentile` | number\|null | |
| `iv_lookback_days` | int | |

#### `market.macro`
Snapshot, polled. India VIX, USDINR, index levels, sector performance table,
advance/decline, and overnight references (US close, SGX/GIFT Nifty, crude, gold).
Shape is flexible — tell us what you have and we'll adapt.

---

### 4.2 `news.*`

#### `news.item`

| Field | Type | Notes |
|---|---|---|
| `headline` | string | as published, not rewritten |
| `summary` | string\|null | **extractive** — sentences from the source. Not generated |
| `body_text` | string\|null | full text if licensing permits; very valuable |
| `url` | string | |
| `publisher` | string | |
| `published_at` | timestamp | |
| `entities` | array | `[{isin?, trading_symbol, confidence, role}]` |
| `category` | enum\|null | see below |
| `is_rumour` | bool\|null | unconfirmed/"sources say" reporting |

`category`: `results` `guidance` `block_deal` `pledge` `order_win` `rating_change`
`regulatory` `legal` `management` `mna` `fundraise` `product` `macro` `sector`
`other`.

`role` in `entities` distinguishes *the company the news is about* from one merely
mentioned. A supplier named in a competitor's story shouldn't alert that
supplier's holder. If you can only do mention-detection, say so — we'll weight
accordingly rather than assume.

**On sentiment and rank** *(revised in v0.2 — v0.1 rejected these outright, which
was too blunt).* Keep emitting both. We consume `sentiment` as **advisory only**:
never as a gate, and never as the sole source of direction — we need the raw
magnitudes so we can derive direction ourselves. We don't order on `rank`, since
our ordering is per-person, but there's no reason to strip it.

The distinction v0.1 missed: sentiment **about the instrument** ("negative for the
company") is a legitimate, largely objective claim and is what you produce.
Sentiment **about the reader** ("bad news for you") depends on which side of the
position they're on, and is ours to compute.

**One thing we do need, and it's a doc change not a build:** tell us how
`sentiment` is derived, per insight type — `derived | model | none`. Sentiment
mechanically derived from a buildup class is a deterministic relabel we can trust
and cross-check; sentiment inferred from article text is a weak prior. Without
knowing which is which we must treat all of it as the weakest case, which wastes
the deterministic ones.

---

### 4.3 `filing.*` — exchange and regulatory disclosures

**Highest value per byte in this entire contract.** Filings are authoritative,
structured, timestamped, and are the actual cause of most single-stock moves.
If you build one family well, build this one.

#### `filing.announcement`
| `category`, `subject`, `body_text`, `attachment_url`, `filed_at`, `is_price_sensitive` |

#### `filing.corp_action`
| `action_type` (`dividend`\|`bonus`\|`split`\|`rights`\|`buyback`\|`merger`\|`demerger`), `ex_date`, `record_date`, `value`, `ratio` |

**`ex_date` is the field that matters to us**, not the announcement date — it's the
day a holder's position value changes. Send corporate actions as soon as declared,
with the ex-date; we schedule our own reminders from it.

#### `filing.results_calendar`
| `results_date`, `quarter`, `confirmed` (scheduled vs indicated) |

Forward-looking. We want this days ahead, not on the day.

#### `filing.block_deal`
| `client_name`, `qty`, `price`, `side`, `pct_equity`, `deal_type` (`block`\|`bulk`) |

`client_name` matters — "promoter entity sold" and "a mutual fund bought" are
different stories. Send the name as filed; we'll classify.

#### `filing.fno_ban`
| `date`, `symbols[]`, `action` (`entry`\|`exit`) |

Daily, pre-open. Small payload, disproportionately useful.

#### `filing.pledge` *(Later)*
#### `filing.shareholding` *(Later)* — quarterly pattern changes, FII/DII/promoter deltas

---

### 4.4 Later legs — for your roadmap, not needed now

Flagging so you can shape the architecture, not requesting:

- **Mutual fund NAVs and scheme metadata** — daily NAV, category, benchmark,
  portfolio holdings per scheme. Enables allocation drift and fund-overlap
  analysis for a later leg.
- **Sector and thematic rotation** — relative strength by sector over rolling windows.
- **Index composition changes** — inclusions/exclusions, rebalance dates.
- **Analyst estimate revisions** — consensus changes, target price moves.
- **Delivery percentage** — daily deliverable vs traded quantity.
- **Correlation matrices** — for concentration and diversification analysis.

---

## 5. Semantics that are easy to get wrong

This section exists because each of these has a plausible-looking wrong
implementation that produces a signal we'd have to throw away.

### 5.1 Relative volume must be time-of-day normalised

Comparing cumulative volume-so-far against a full-day average makes every stock
look quiet at 09:30 and normal by 15:00. Comparing against *yesterday's full day*
is worse. Either way we'd be alerting on the clock, not on the stock.

What we need: today's volume at time *t* against the distribution of volume at the
*same time of day* over a trailing window — median cumulative volume at minute *t*
across the last 20 sessions is a reasonable baseline. Intraday volume is
U-shaped and the normalisation has to respect that.

If you can't do this, **send `rel_volume: null` and populate `volume` +
`baseline_volume`** and we'll derive it. Please don't send an un-normalised ratio
in the `rel_volume` field.

### 5.2 σ-normalised moves beat percentage moves

A 3% day in a large-cap index heavyweight and a 3% day in a smallcap are not the
same event. Wherever you can, give us the move in units of that instrument's own
trailing realised volatility. This single field does more to make alerts feel
intelligent than any other in the contract, because it encodes "unusual for *this*
stock" rather than "big in the abstract".

### 5.3 OI buildup classification

Standard four-quadrant, stated explicitly to avoid sign confusion:

| Price | OI | Class |
|---|---|---|
| ↑ | ↑ | `long_buildup` |
| ↓ | ↑ | `short_buildup` |
| ↑ | ↓ | `short_covering` |
| ↓ | ↓ | `long_unwinding` |

Send `buildup_class: null` rather than guessing when either change is within noise.
Please also send the raw `oi_change_pct` and `price_change_pct` so we can apply our
own thresholds.

### 5.4 IV rank matters more than IV level

"IV is 24" is close to meaningless without history. "IV is at the 85th percentile
of its trailing year" is actionable. If you can only send one, send the rank.
State the lookback.

### 5.5 Circuits: locked vs touched

A stock that tags the upper band and trades back is a different event from one
locked with no counterparty. `is_locked` shouldn't be inferred from price alone.

### 5.6 Gaps update

A gap that fills by 10:00 is a different story from one that holds all day. Send
the fill as an **update carrying the same `source_event_id`**, not a new event.

---

## 6. Delivery

### 6.1 Transport — **do not build push**

*(Revised in v0.2. v0.1 asked for webhooks; that requirement is withdrawn.)*

The 60-second targets in v0.1 existed for circuits and gaps. Both now come from
our own market feed, so the tight-latency items are off your critical path
entirely. What remains on your side — news, filings, OI, concall — has a natural
floor in the minutes regardless of transport. **Poll at 60s is acceptable.**

Two things make polling work properly, and the first is the real transport ask:

1. **A since-cursor endpoint.** `GET /signals?since=<cursor>` returning everything
   new across all entities in one call. Per-entity polling doesn't scale —
   watching N instruments is N requests per cycle, worse with every user. One
   changed-since firehose replaces all of it and removes the reason push existed.
   It is substantially cheaper to build than push.
2. **An uncached path, or a shorter TTL, on that endpoint.** A 5-minute cache in
   front of a 60-second poll makes the interval decorative.

If the raw write can land before the prose step (see the reply doc, D1), effective
latency on poll should be good enough that push never needs building.

### 6.2 Latency budget

Targets, not hard requirements — tell us where they're unrealistic and we'll adjust
the product rather than pretend.

| Kind | Target, from `event_at` |
|---|---|
| `filing.*` | 2 min |
| `market.volume_spike`, `market.breakout` | 3 min |
| `news.item` | 5 min |
| `market.oi` | one 3-min bar |
| `market.mover`, `market.macro` | 1 min refresh |

Filings are tightest because they're the highest-confidence explanation of a move,
and a move explained 20 minutes late has usually already been explained by the
market.

`market.circuit`, `market.gap`, `market.iv` and `sigma_move` are **no longer on
this list** — we source them ourselves.

### 6.3 Ordering, retries, backfill

- **At-least-once is fine.** We dedupe on `source_event_id`. Please don't build
  exactly-once.
- **Out-of-order is fine.** We sort on `event_at`.
- **Backfill is a real requirement, not a nice-to-have.** We need to fetch
  historical signals for an arbitrary past date range — ideally as a
  `GET /signals?from=&to=&kinds=` or an equivalent export. We tune our
  personalisation offline by replaying real signal days; without backfill, every
  threshold change costs a week of live market to evaluate. **Please treat this as
  a Leg 1 requirement.**

  **Retention is separable from the read API, and far more urgent** *(added in
  v0.2)*. If signals expire and are purged, backfill cannot be built
  retrospectively — the history won't exist — and **the depth we can ever tune
  against is set by when retention starts, not when the API ships.** So: start
  retaining now, even with no read path, no envelope and no schema change. Cold
  storage, a dump table, an object-store prefix; anything append-only. Every week
  this waits is a week of tuning data that cannot be recovered.

  Depth: **12 months ideal, 3 months floor.** Below ~60 sessions there isn't
  enough to separate a threshold from noise; 12 months matters because it spans at
  least one high-volatility episode and a full set of monthly expiries, and
  thresholds tuned only on a calm market fire constantly in a volatile one. If
  depth is expensive, **raw-field history is worth more to us than prose history**
  — 12 months raw-only beats 3 months of everything.

### 6.4 Heartbeat

A per-source heartbeat — "source `X` alive, last event at `T`, `N` events today" —
at a fixed interval regardless of activity. We cannot otherwise distinguish a quiet
market from a dead feed, and a silently dead feed is the failure mode that damages
us most: the user learns we're not watching.

### 6.5 Coverage

Tell us plainly what universe you cover, so we know where our blind spots are:

- NSE and BSE cash equities — all, or an index subset?
- SME and illiquid scrips — in or out?
- NSE F&O — all underlyings? Weeklies and monthlies?
- Indices, and which?
- MCX commodities?
- Are dual-listed names sent once or twice?

Partial coverage is workable. Undocumented partial coverage is not — we'd be
telling users nothing happened when we simply couldn't see.

---

## 7. What we'll send back

We'd like this to be a loop, not a pipe. Without exposing anything about
individual users, we can return in aggregate:

- which signal kinds most often led to a user-visible message, and which never did
- which fields were most often `absent`, per source
- signals we received but could not resolve to an instrument
- corroboration failures — where your number and the exchange's disagreed

The last two are worth wiring early; they're how a quietly degrading source gets
caught.

---

## 8. Versioning

`schema_version` on every signal. Additive changes (new optional fields, new kinds)
don't need a bump — we ignore unknown fields. Removing a field, changing a type,
or changing the meaning of an existing field is breaking and needs a version bump
and a conversation. Changing how a derived field is *computed* (a different
volume baseline, a different σ window) is a **breaking change** even though the
type is unchanged, because our thresholds are calibrated against it. Please tell us
before shipping one.

---

## 9. Open questions for you

*(Revised in v0.2 — your first handoff answered most of the original list. These
are what's still open, in order of consequence.)*

1. **Are expired signal rows retained or purged?** Most consequential question
   here — see §6.3.
2. **Is the signal id stable across regenerations of the same underlying
   condition, or per persisted row?** This determines whether dedupe is possible
   at all. If it's per-row we can fingerprint on `(entity, type, state_class)`
   ourselves — but only once raw magnitudes are persisted.
3. How is `sentiment` derived, per signal type — deterministic or model? (§4.2)
4. Can the raw structured write be decoupled from, and land before, the prose
   generation step?
5. Is a signal emitted once per entity, or once per listing? (§3, dual listings)
6. Does anything today carry a **market event time**, as distinct from the row's
   creation time? (§2, principle 4)
7. Is the read cache TTL configurable per client? (§6.1)
8. What does coverage (§6.5) actually look like today?
9. Anything you already produce that isn't in this document? We wrote it from what
   we need, not from what exists, so there are probably useful things we haven't
   thought to ask for.

---

**We'd rather have five signal kinds that are correct, timely and honest about
their gaps than fifteen that are approximately right.** Nothing in this contract is
more important than principle 1 in §2: when you don't know, say you don't know.
