-- Groww desk schema. Phase 1 tables (spec §8) plus the instrument master (§5.4).
-- Phase 2 tables (ledger, beliefs, campaigns, outbox) live in schema_phase2.sql.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS users (
  id            bigserial PRIMARY KEY,
  wa_id         text UNIQUE NOT NULL,
  display_name  text,
  timezone      text DEFAULT 'Asia/Kolkata',
  created_at    timestamptz DEFAULT now(),
  status        text DEFAULT 'active'        -- active | unlinked | blocked
);

CREATE TABLE IF NOT EXISTS credentials (
  user_id           bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  totp_token_enc    bytea NOT NULL,
  totp_secret_enc   bytea NOT NULL,
  last_mint_at      timestamptz,
  mint_failures     int DEFAULT 0,
  state             text DEFAULT 'ok'        -- ok | failing | dead
);

-- Enforces the 150-per-24h cap on /v1/token/api/access (spec §4.3).
CREATE TABLE IF NOT EXISTS token_mints (
  id        bigserial PRIMARY KEY,
  user_id   bigint REFERENCES users(id) ON DELETE CASCADE,
  minted_at timestamptz DEFAULT now(),
  ok        boolean NOT NULL,
  error     text
);
CREATE INDEX IF NOT EXISTS idx_token_mints_recent ON token_mints (minted_at DESC);

CREATE TABLE IF NOT EXISTS link_requests (
  token       text PRIMARY KEY,
  wa_id_hash  text NOT NULL,
  expires_at  timestamptz NOT NULL,
  used        boolean DEFAULT false
);

CREATE TABLE IF NOT EXISTS messages (
  id              bigserial PRIMARY KEY,
  user_id         bigint REFERENCES users(id),
  direction       text NOT NULL,            -- in | out
  channel_msg_id  text,
  text            text,
  intent          text,
  latency_ms      int,
  created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS traces (
  id           bigserial PRIMARY KEY,
  message_id   bigint REFERENCES messages(id),
  path         text,                        -- fast | llm
  intent       text,
  tool_calls   jsonb,                       -- [{name, args, ms, ok}]
  llm_tokens   jsonb,                       -- {in, out, model}
  error        text,
  created_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS watches (
  id          bigserial PRIMARY KEY,
  user_id     bigint REFERENCES users(id) ON DELETE CASCADE,
  exchange    text, segment text, trading_symbol text,
  condition   text,                          -- above | below | pct_move
  value       numeric,
  active      boolean DEFAULT true,
  created_at  timestamptz DEFAULT now(),
  fired_at    timestamptz
);
CREATE INDEX IF NOT EXISTS idx_watches_active ON watches (active, trading_symbol);

CREATE TABLE IF NOT EXISTS instruments (
  exchange        text NOT NULL,
  segment         text NOT NULL,
  trading_symbol  text NOT NULL,
  exchange_token  text NOT NULL,
  groww_symbol    text,
  isin            text,
  name            text,
  instrument_type text,          -- EQ / FUT / CE / PE / IDX
  underlying      text,
  expiry          date,
  strike          numeric,
  lot_size        int,
  tick_size       numeric,
  PRIMARY KEY (exchange, segment, trading_symbol)
);
CREATE INDEX IF NOT EXISTS idx_inst_trgm
  ON instruments USING gin (name gin_trgm_ops, trading_symbol gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_inst_underlying
  ON instruments (underlying, expiry, strike, instrument_type);
CREATE INDEX IF NOT EXISTS idx_inst_token ON instruments (exchange_token);

CREATE TABLE IF NOT EXISTS instrument_cache (
  user_id     bigint REFERENCES users(id) ON DELETE CASCADE,
  query_norm  text,
  exchange    text, segment text, trading_symbol text,
  hits        int DEFAULT 1,
  updated_at  timestamptz DEFAULT now(),
  PRIMARY KEY (user_id, query_norm)
);

CREATE TABLE IF NOT EXISTS prefs (
  user_id     bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  quiet_start time DEFAULT '23:30',
  quiet_end   time DEFAULT '08:00',
  muted_until timestamptz,
  verbosity   text DEFAULT 'normal'
);
