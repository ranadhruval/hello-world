# GR-2: what transfers from Hermes Agent and OpenClaw

**Status:** research complete, nothing implemented.
**Sources actually read** (cloned and inspected, not summarised from memory):

| Project | Repo | What I read |
|---|---|---|
| Hermes Agent | `NousResearch/hermes-agent` | `SOUL.md`, `cron/AGENTS.md`, `cron/{suggestions,delivery_queue,unreachable_retry,notepad,incidents}.py`, `agent/{memory_manager,system_prompt}.py`, `tools/{memory_tool,memory_tool_store}.py`, `gateway/whatsapp_identity.py`, `scripts/whatsapp-bridge/owner_message_gate.js` |
| OpenClaw | `openclaw/openclaw` | `VISION.md`, `docs/concepts/{memory-architecture,dreaming,memory-provenance,user-model,standing-intents}.md`, `docs/gateway/heartbeat.md`, `docs/automation/cron-jobs/delivery.md`, `src/{cron,auto-reply,context-engine}/` |
| Pi (Inflection) | — | **No repo exists.** Pi is closed-source. Nothing architectural transfers; I am not going to reconstruct lessons from a product I cannot inspect. |

Decisions taken with the user before writing this:

- GR-2 is heading to **many users** — design for it now.
- Background LLM consolidation is **in scope**, detection stays deterministic.
- Memory stays **internal** for now, with a local dump for tuning.
- **Self-proposed watches are in scope.**

---

## 0. One live bug, found in the research

`hermes-agent/gateway/whatsapp_identity.py`:

> The bridge can surface one human as a LID (`999...@lid`) or a phone JID
> (`1555...@s.whatsapp.net`) **within one conversation**. Authorisation and
> session keys both resolve aliases here so they never drift apart.

GR-2 has `users.wa_id UNIQUE`, and `adapter/index.ts:jidToWaId()` derives `wa_id`
by stripping the JID down to digits. A LID and a phone JID for the same person
strip to *different* digits. So one human arriving under both forms becomes **two
user rows — two books, two belief maps, two alert budgets, two link tokens.**

This is not a multi-user problem. It bites with one user, today, the first time
WhatsApp switches addressing mid-conversation. It is the single most urgent item
in this document and it is half a day of work (B1).

---

## Bucket A — already in GR-2

Not work. Recorded because independent convergence is the strongest evidence we
got the foundations right, and because it tells us which of our decisions to
*stop relitigating*.

| GR-2 has | Converges with | Note |
|---|---|---|
| I1/I2 — numbers and detection never from the model | OpenClaw memory principle 4: *"Scoring, thresholds, eligibility, matching, and lifecycle are deterministic code. The language model is used where language judgment is genuinely needed, always inside bounds that deterministic code enforces."* | Near-verbatim, arrived at independently on a different problem |
| Interrupt budget of 6/day | Hermes `suggestions.py`: `MAX_PENDING = 5` — *"so the list never becomes a nag wall"* | Same instinct, same order of magnitude |
| Fingerprint + cooldown collapse | Hermes `dedup_key`, latched so a dismissed item is never re-offered | Ours is per-condition; theirs is per-proposal |
| Consent-first linking (I3) | Hermes: *"nothing auto-creates (consent-first)"* | |
| `score_trace` on every decision | OpenClaw provenance-at-write-time | |
| Shadow report's SILENT section with reasons | OpenClaw dreaming: *"Deep reports summarize why ranked candidates were not promoted, using counts by rejection category"* | Both discovered that explaining suppression is what makes silence trustworthy |
| Outbox supersession | OpenClaw: *"A retry cannot append the same result twice"* via job/run idempotency key | |

**One place we are ahead:** neither project has anything like `NumericGuard`.
Both rely on prompt discipline to keep models from inventing figures. Neither is
in a domain where a wrong number costs money, so neither needed to. We should
keep it and not assume the absence means it is unnecessary.

---

## Bucket B — minimal effort, should add now

Ten items, roughly a week total. Each maps onto code that already exists.

### B1 · Canonical WhatsApp identity — **do this first** · ½ day
`app/channel/identity.py`: `canonical_wa_id()` applied at the adapter boundary,
used by **both** `Store.user_id_for` and any session keying, plus a `wa_aliases`
table mapping every observed form to one user. Hermes' hard-won framing is that
authorisation and session keys must resolve through the *same* function "so they
never drift apart" — two lookups that disagree is the actual failure.

Also adopt `to_whatsapp_jid()`'s note that **Baileys' `jidDecode` crashes on a
bare phone**, which validates our `${wa_id}@s.whatsapp.net` fallback.

### B2 · Per-job notepad for cursors and watermarks · ½ day
`cron/notepad.py` — durable KV carried across wake-ups, with hard caps
(16 KB/value, 64 KB/job) *because the notepad is prompt-injected each run*.
GR-2 needs exactly this for the signal engine's `since` cursor. The cap
discipline is the transferable part, not the storage.

### B3 · Bounded retry, only when nothing was spent · ½ day
`cron/unreachable_retry.py` re-runs at 5/15/30 min **only** when the run failed
with a transient network error **and** the agent completed zero API calls:

> Nothing was executed and nothing was spent, so re-running cannot double a side
> effect — unlike a generic failure retry, which has to answer for one-shot
> dispatch accounting and mid-run side effects.

GR-2's outbox has a generic `attempts` counter. Replace with this narrow
condition. Also: *"While a retry is pending the failure notice is suppressed"* —
don't tell the user about a failure you are about to silently fix.

### B4 · An `unknown` delivery state — **and deliberately invert their default** · ½ day
`cron/delivery_queue.py`:

> If that gateway dies after claiming, the outcome is marked unknown and never
> retried: losing a delivery is safer than duplicating a possibly-completed send.

Adopt the third state. **Reject the default for protective families.** Their
domain is chat; ours is money. A duplicated margin alert is mildly annoying; a
dropped one costs real rupees. So: `unknown` exists, and the retry decision is
per-family — `margin`/`expiry`/`structure` retry on unknown, `market`/`news` do
not. This is a place where copying the reference implementation would be wrong.

### B5 · Alerts must commit to the conversation transcript · ½ day
OpenClaw's delivery contract:

> The run is reported delivered only after **both** the external recipient
> handoff (when required) **and** the canonical session commit succeed.

GR-2 marks `outbox.sent` on a returned `channel_msg_id` and never writes the
alert into `messages`. But our own spec §17.3 routes a free-text reply to an
alert "into the Phase 1 agent with the alert as context" — which is impossible
if the alert was never in the history. Small fix, unblocks the whole reply
grammar. Also adopt their distinction between a **delivery failure and a turn
failure**; our outbox currently conflates them.

### B6 · Scheduler hardening invariants · 1 day
From `cron/AGENTS.md`, each guarding a named real failure:

- advance `next_run_at` **before** dispatch (at-most-once across a mid-run crash)
- stamp `pending_slot` in the same save; a scan finding the stamp with a dead
  owner restores the instant **once**
- an executions ledger whose `scheduled_instant` blocks a second fire
- catch-up window = **half the period, clamped 120 s–2 h**
- a file lock preventing duplicate ticks across processes
- **"Never drop a slot silently."**

My `DailyScheduler` design has catch-up but neither the pre-advance nor the slot
ledger. Their 3-minute hard interrupt on cron sessions ("runaway loops cannot
monopolise the scheduler") maps onto our per-tick timeout.

### B7 · Wake provenance markers · trivial, after B5
OpenClaw marks transcript entries so a `[heartbeat poll]` is distinguishable from
a cron wake or a session event, *"without copying internal instructions into chat
history."* Our stored alert rows should carry which rule and which signal
produced them — needed for the feedback loop anyway.

### B8 · An incident model for our own failures · 1 day
`cron/incidents.py` keys incidents on `(job_id, error signature)` with a
`detected → alerted → closed` lifecycle, so *"the same job failing with the same
error does not re-ping the operator every run once acknowledged"*, and a closed
incident stays closed until the error text changes and mints a new one.

`app/obs/` is empty and our three "alerts stopped, chat works" detectors have no
dedup — they would page on every tick. This is the fix.

### B9 · A voice contract · 2 hours
Hermes' `SOUL.md` is **one paragraph**, not a persona sheet: match reply length
to the weight of the ask, no filler, no restating the request, no narrating tool
calls, plain claims over adjectives, *"when unsure, say so plainly"*, *"agree
because it's right, not because the user said it."*

Ours already exists scattered — the house style in `app/render/templates.py`'s
docstring, the three-part alert anatomy, the no-advice rule. Consolidating it
into one reviewable paragraph costs nothing and is the natural home for
"describe, don't prescribe" when the LLM path lands.

### B10 · Self-proposed watches · 2 days *(user: yes)*
`cron/suggestions.py` in full: a proposal is a ready-to-run spec the user accepts
(creating the real job) or dismisses (latched by `dedup_key`, never re-offered);
`MAX_PENDING = 5`; **nothing auto-creates**; accepting calls the same
`create_job` as any other path — *"no second job engine."*

GR-2 has an unused `watches` table and a natural set of proposal sources:
`asked_repeatedly` (four TITAN queries this week), `position_opened` (a new
position with no watch), `catalog` (starter watches for a new user). The "no
second engine" rule matters: an accepted suggestion must create an ordinary
watch row, not a parallel construct.

---

## Bucket C — greenfield / brownfield, real projects

### C1 · Tiered memory with write-time provenance gating · 2–3 weeks · brownfield
`docs/concepts/memory-architecture.md`. Five principles, of which three change
how we'd build:

> **Writing is the hard part.** Retrieval over notes files is competitive with
> far heavier designs; what degrades memory systems is unreliable write-time
> curation. [...] OpenClaw therefore moves curation off the busy reply path and
> into a dedicated background pass.

> **The write path is the security boundary.** Content-level scanning of memory
> cannot catch poisoned facts reliably, so OpenClaw enforces provenance at write
> time and gates promotion structurally instead of trying to detect bad memories
> later.

> **Failures never block replies.** Every memory step in the reply path has a
> timeout, a fallback, or both. A memory subsystem that is down degrades recall
> quality; it never eats a turn.

**Why this one matters more for us than for them.** GR-2 ingests untrusted
external text — news headlines, filing bodies — by design. If a belief derived
from a filing body ever enters a prompt, we have a persistent injection vector
that survives sessions. Hermes defends with content scanning
(`_scan_memory_content`, *"memory enters the system prompt, so a poisoned entry
persists across sessions"*). OpenClaw says explicitly that content scanning is
**not sufficient** and gates structurally on provenance instead. Given we are
multi-user, take OpenClaw's position.

Our `beliefs` table is a flat KV with a `source` column. This replaces it with
tiers that differ in trust, write rule, and injection behaviour.

### C2 · Background consolidation · 2 weeks · greenfield *(user: yes)*
`docs/concepts/dreaming.md`. A nightly pass over the ledger producing a durable
trading profile — *sells premium on weeklies, cuts losers fast, holds winners,
checks in at 9:20 and 15:35*. Four details worth copying exactly:

- **Rewrite preimages stored before an accepted rewrite** — a bad consolidation
  is reversible. We have the analogous instinct in outbox supersession.
- **Rejection-category reporting** — the deep report says why candidates were
  *not* promoted, by category, without copying rejected snippets. Same shape as
  our "suppressed today (N)".
- **Recall metadata on promoted entries** — up to three concept tags and a
  bounded importance 1–10.
- **Byte-for-byte preservation** of existing entries unless explicitly merged.

**The constraint we must add, which they do not need:** this is the one place a
model writes durable state. Consolidation *proposes*; deterministic code
*accepts*. And nothing it writes may lower a protective belief — our existing
`PROTECTIVE_FLOOR` must extend to consolidation, or the system can learn its way
out of margin alerts through the back door.

### C3 · Prompt tiering for cache economics · small **now**, expensive later
`agent/system_prompt.py` joins three tiers — `stable` (identity, guidance),
`context` (workspace, caller message), `volatile` (skills index, memory, USER.md,
timestamp) — *"built once per session and reused across turns (only context
compression triggers a rebuild) so the upstream prefix cache stays warm."*
`memory_tool` reinforces it: memory enters as a **frozen snapshot at session
start; mid-session writes hit disk but never change the prompt.**

This is a **design constraint to adopt before writing `app/agent/`**, not a
retrofit. Cheap now, a rewrite later. Flagging it because our LLM path is still
an empty directory — this is the moment.

### C4 · Standing intents · 1–2 weeks · greenfield
`docs/concepts/standing-intents.md` gives a taxonomy we lack:

| Intention | Mechanism | GR-2 today |
|---|---|---|
| Time-based | scheduled job | the bookend briefs |
| Event-based | **standing intent** | our spec's "campaigns", unbuilt |
| Aspiration | markdown + explicit review date | **nothing** |

> Standing intents are prospective memory. They remember what to do when a
> trigger appears; they do not schedule work for a clock time.

Their intent carries description + trigger + scope + **expiry** + **fire budget**
+ **cooldown** — better specified than our campaign model, and the fire budget in
particular is something we'd have missed. The aspiration tier ("reduce smallcap
exposure this quarter" — reviewed, never triggered) is a genuine gap.

Note their creation rule, which matters given multi-user: creation requires an
authenticated channel **and** sender identity; admins can inspect and cancel but
not create. That is our I3, stated more precisely than we state it.

### C5 · Multi-tenant isolation · 2–3 weeks · brownfield *(now load-bearing)*
Hermes' lesson is specific and clearly hard-won — from `cron/suggestions.py`:

> Production resolves the path at CALL time so multiplexed profile ticks
> cannot leak one profile's suggestions into the import-time home.

The same comment recurs across `notepad.py`, `executions.py`, `incidents.py`.
**Resolve per-user state at call time, never at import time.**

GR-2 is full of import-time and process-global state that is fine for one user
and leaks for many: `_HOLIDAYS`, `@lru_cache settings()`, `TokenBroker._clients`,
the in-process `CircuitBreaker`, `RateLimiter` with no per-user partition. The
rate limiter and breaker are already on the Phase 2 fix list for a different
reason; this makes them non-optional.

---

## What I deliberately recommend *not* taking

Judgement is mostly about what to leave. From two repos totalling ~55,000 files:

- **OpenClaw's heartbeat-as-detection.** A scheduled agent turn that decides
  whether to speak is a model in the interrupt loop — a direct trade against I2,
  and against determinism on money. The user chose background-only, which is
  right. We can take the *rate-limit shape* (30 s minimum between event turns, a
  flood guard after five starts in 60 s) without the architecture.
- **Plugin and skill systems.** Both are large. Our tool surface is small,
  fixed, and financial; dynamic skills are a liability here, not a feature.
- **Kanban / multi-agent work queues.** Irrelevant.
- **Gateway identity plumbing** (GitHub-backed sign-in, operator roles, Cloudflare
  Access). Our identity problem is one WhatsApp number, not a shared workspace.
- **Hermes' content-scanning approach to memory poisoning** — superseded by
  OpenClaw's structural argument (C1).

---

## Suggested order

1. **B1** — the live bug. Before anything else.
2. **C3** — decide prompt tiering *before* `app/agent/` gets written.
3. **B5, B7, B4, B3, B2** — delivery correctness, roughly two days together.
4. **B6, B8** — scheduler and self-monitoring, before the watcher runs unattended.
5. **B9, B10** — voice contract, then self-proposed watches.
6. **C5** — isolation, once multi-user stops being hypothetical.
7. **C1 → C2** — memory tiers first, then consolidation on top. In that order:
   consolidation without provenance gating is the injection vector, fully built.
8. **C4** — standing intents, the natural home for the unbuilt campaign model.

Bucket B is about a week and makes the watcher safe to leave running. Bucket C is
two to three months and is what makes it feel like it knows you.
