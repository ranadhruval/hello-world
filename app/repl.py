"""Console REPL — drives the real pipeline over stdin.

    TOTP_TOKEN=... TOTP_SECRET=... python -m app.repl

Same router, same tools, same renderer as WhatsApp; only the channel differs.
That is the point of putting every channel behind one interface (spec §3.1).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import pyotp
from growwapi import GrowwAPI

from app.auth.broker import _access_token
from app.channel.console import ConsoleChannel, inbound
from app.dispatch import Desk
from app.tools.groww import GrowwTools
from app.tools.instruments import InstrumentIndex
from app.worker import Worker

CSV = Path(__file__).resolve().parents[1] / "data" / "instruments.csv"


class _DirectBroker:
    """Mints once from the environment. The real broker is per-user and DB-backed."""

    def __init__(self) -> None:
        token, secret = os.environ.get("TOTP_TOKEN"), os.environ.get("TOTP_SECRET")
        if not token or not secret:
            sys.exit("Set TOTP_TOKEN and TOTP_SECRET (groww.in/trade-api/api-keys)")
        access = GrowwAPI.get_access_token(api_key=token, totp=pyotp.TOTP(secret).now())
        self._client = GrowwAPI(_access_token(access))

    async def client(self, user_id: int) -> GrowwAPI:
        return self._client

    async def invalidate(self, user_id: int) -> None:
        return None


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if not CSV.exists():
        sys.exit(f"instrument master missing at {CSV} — run `make instruments`")

    print(f"Loading instrument master from {CSV}…")
    index = InstrumentIndex.from_csv(CSV)
    print(f"  {len(index)} instruments")

    broker = _DirectBroker()
    channel = ConsoleChannel()
    desk = Desk(GrowwTools(broker, user_id=1), index)

    worker = Worker(channel, index, desk_for=lambda wa_id: _ready(desk))
    print("\nType a message (ctrl-d to exit). Try: portfolio · positions · margin · nifty\n")

    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, input, "> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line.strip():
            continue
        await worker.handle(inbound(line))
        print()


async def _ready(desk: Desk) -> Desk:
    return desk


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
