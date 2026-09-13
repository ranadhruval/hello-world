-- Phase 2: the proactive layer (spec §12, §14, §18).
--
-- Three stores with different lifetimes, and the difference matters:
--   ledger   append-only, never updated — what happened
--   beliefs  mutable, versioned, provenanced — what we know about the user
--   outbox   short-lived, state machine — what we are about to say
--
-- Run after schema.sql. Everything is IF NOT EXISTS, so re-running is safe.

-- ---------------------------------------------------------------- signals

-- Raw inbound signals from the insight engine, before any judgement.
-- Kept so a day can be replayed through a changed gate without a market:
-- alerting thresholds cannot be tuned live, and this table is what makes
-- offline tuning possible (scripts/replay.py).
CREATE TABLE IF NOT EXISTS signals (
  id              bigserial PRIMARY KEY,
  source          text NOT NULL,
  source_event_id text NOT NULL,
  kind            text NOT NULL,
  entity_key      text,                 -- resolved '<exchange>_<trading_symbol>'
  entity_raw      jsonb NOT NULL,       -- as received, before resolution
  payload         jsonb NOT NULL,
  evidence        jsonb,
  absent          text[] DEFAULT '{}',  -- fields the source could not populate
  event_at        timestamptz,          -- when it happened in the market
  observed_at     timestamptz NOT NULL, -- when the source saw it
  received_at     timestamptz NOT NULL DEFAULT now(),
  resolve_error   text                  -- set when entity resolution failed
);
-- Cross-source redelivery dedupe. The engine's own id is per-row rather than
-- per-event, so this catches redelivery only; a sustained condition is
-- collapsed by the ledger fingerprint below, not here.
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_event
  ON signals (source, source_event_id);
CREATE INDEX IF NOT EXISTS idx_signals_replay ON signals (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_entity ON signals (entity_key, received_at DESC);

-- ----------------------------------------------------------------- ledger

-- Append-only. Never UPDATE a row.
CREATE TABLE IF NOT EXISTS ledger (
  id           bigserial PRIMARY KEY,
  user_id      bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ts           timestamptz NOT NULL DEFAULT now(),
  kind         text NOT NULL,       -- position.opened | margin.band_changed | ...
  entity       text,                -- trading_symbol, or 'ACCOUNT'
  before       jsonb,
  after        jsonb,
  magnitude    numeric,             -- normalised 0..1, for gate scoring
  source       text NOT NULL,       -- feed | rest | signal | chat
  fingerprint  text NOT NULL,       -- hash(kind|entity|state_class|date)
  signal_id    bigint REFERENCES signals(id) ON DELETE SET NULL
);
-- The index that stops a wobbling value producing 400 events.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_fingerprint
  ON ledger (user_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_ledger_recent ON ledger (user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_entity ON ledger (user_id, entity, ts DESC);

-- ---------------------------------------------------------------- beliefs

-- What we know about the user. Versioned and provenanced so a preference can
-- be revoked cleanly: "stop telling me about OI walls" sets one belief and
-- every derived weight recomputes, rather than hunting through config.
CREATE TABLE IF NOT EXISTS beliefs (
  user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key         text NOT NULL,       -- sizing.typical_notional | response.margin | ...
  value       jsonb NOT NULL,
  confidence  numeric NOT NULL DEFAULT 0.5,  -- 0..1
  evidence_n  int NOT NULL DEFAULT 0,        -- must reach 3 before it counts
  source      text NOT NULL DEFAULT 'default',  -- default | chat | activity | explicit
  updated_at  timestamptz NOT NULL DEFAULT now(),
  version     int NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, key)
);

-- ----------------------------------------------------------------- outbox

-- Never send from inside rule evaluation. Rules write here; a drainer in the
-- worker process sends. An alert the system believes it delivered but did not
-- is worse than no alert.
CREATE TABLE IF NOT EXISTS outbox (
  id               bigserial PRIMARY KEY,
  user_id          bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  idempotency_key  text UNIQUE NOT NULL,   -- (user_id, rule_id, dedupe_key, ist_date)
  rule_id          text NOT NULL,
  fingerprint      text NOT NULL,
  payload          jsonb NOT NULL,         -- the typed slot map, for audit
  body             text NOT NULL,          -- the rendered message
  route            text NOT NULL,          -- interrupt | batch | digest
  score            numeric,
  score_trace      jsonb,                  -- stage-by-stage, for tuning
  state            text NOT NULL DEFAULT 'pending',
                   -- pending | sent | failed | superseded | shadow
  attempts         int NOT NULL DEFAULT 0,
  send_after       timestamptz NOT NULL DEFAULT now(),
  created_at       timestamptz NOT NULL DEFAULT now(),
  sent_at          timestamptz,
  channel_msg_id   text,
  last_error       text
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
  ON outbox (state, send_after) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_outbox_user_day
  ON outbox (user_id, created_at DESC);
-- Supersession looks up an unsent row for the same fingerprint.
CREATE INDEX IF NOT EXISTS idx_outbox_fingerprint
  ON outbox (user_id, fingerprint) WHERE state = 'pending';

-- ------------------------------------------------------------ suppressions

-- Every signal that did NOT become a message, with the reason. This is what
-- makes "silent" mean "decided" rather than "dropped": it backs the
-- "suppressed today (N)" digest block, and it is how silence precision gets
-- measured — when the user asks "did anything happen with X", we can check
-- whether we suppressed something on X.
CREATE TABLE IF NOT EXISTS suppressions (
  id           bigserial PRIMARY KEY,
  user_id      bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rule_id      text NOT NULL,
  entity       text,
  fingerprint  text NOT NULL,
  route        text NOT NULL,          -- digest | silent
  score        numeric,
  score_trace  jsonb,
  reason       text,                   -- quiet_hours | cooldown | budget | below_bar
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_suppress_user_day
  ON suppressions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_suppress_entity
  ON suppressions (user_id, entity, created_at DESC);

-- --------------------------------------------------------------- job_runs

-- Scheduler last-fired state. Postgres and not Redis on purpose: a Redis
-- flush must never cause a duplicate EOD digest. Redis is the doorbell,
-- Postgres is the truth.
CREATE TABLE IF NOT EXISTS job_runs (
  job_name    text PRIMARY KEY,
  last_run_on date NOT NULL,
  last_run_at timestamptz NOT NULL DEFAULT now(),
  ok          boolean NOT NULL DEFAULT true,
  error       text
);
