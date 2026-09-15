from __future__ import annotations

import base64
import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IST = ZoneInfo("Asia/Kolkata")


def to_ist(ts: datetime | None) -> datetime | None:
    """Postgres hands timestamptz back in the server's zone, and anything naive
    predates that or came from a test. One place to coerce, so no two renderers
    can show the same instant as two different times."""
    if ts is None:
        return None
    return ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cred_key: str = ""
    link_secret: str = ""

    database_url: str = "postgresql+psycopg://groww:groww@localhost:5432/growwdesk"
    redis_url: str = "redis://localhost:6379/0"

    public_base_url: str = "http://localhost:8000"

    baileys_url: str = "http://localhost:3001"

    # Tool cache TTLs in seconds (spec §5.2). Tuned so the Live Data group
    # (10/s, 300/min) stays under budget at ~30 users.
    ttl_holdings: int = 30
    ttl_positions: int = 10
    ttl_margin: int = 15
    ttl_orders: int = 5
    ttl_ltp: int = 3
    ttl_quote: int = 3
    ttl_ohlc: int = 30
    ttl_option_chain: int = 15
    ttl_greeks: int = 15

    # Token broker (spec §4.3)
    token_ttl_seconds: int = 6 * 3600
    max_mints_per_day: int = 140  # Groww caps /v1/token/api/access at 150/24h

    # Orchestrator (spec §2.2)
    debounce_ms: int = 1500
    typing_after_ms: int = 1200

    link_ttl_seconds: int = 600

    @field_validator("cred_key")
    @classmethod
    def _validate_cred_key(cls, v: str) -> str:
        if not v:
            return v
        if len(base64.b64decode(v)) != 32:
            raise ValueError("CRED_KEY must be 32 bytes, base64-encoded (AES-256)")
        return v


@lru_cache
def settings() -> Settings:
    return Settings()


BATCH_LIMIT = 50  # get_ltp / get_ohlc accept at most 50 instruments per call

# Rate limits are per type-group, shared across every API in the group (spec §5.5).
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "auth": (5, 30),
    "orders": (10, 250),
    "live_data": (10, 300),
    "non_trading": (20, 500),
}

_TOKEN_SHAPE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def redact(text: str) -> str:
    """Strip anything token-shaped before it reaches a log (spec §4.4)."""
    return _TOKEN_SHAPE.sub("[REDACTED]", text)
