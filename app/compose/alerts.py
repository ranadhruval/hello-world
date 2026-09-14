"""Alert copy (spec §17.1).

Three parts, no exceptions:

  STATE     what changed, with the number
  SO WHAT   why it matters *to this person*, concretely
  ACTION    one thing they can do about it

If the SO WHAT cannot be written in one concrete sentence, the alert should not
exist. "Silver is up 2%" fails that test. "Silver up 2% — your 1,05,000 PE is
0.4% from ITM with two sessions left" passes.

Template-first (spec §17.2): the model does not write these. It supplies at most
the SO WHAT line, and only where a template cannot — and whatever it writes goes
through `compose.guard` like everything else.

The ACTION line is typed text rather than a button on purpose: Baileys
interactive messages do not render reliably on personal accounts, so the
adapter flattens them to numbered text anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.compose.guard import guard
from app.compose.voice import advice_violations
from app.render.templates import WARN, arrow, inr, pct, qty, signed
from app.watcher.rules import Trigger

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    body: str
    slots: dict = field(default_factory=dict)
    rule_id: str = ""

    def __bool__(self) -> bool:
        return bool(self.body)


def _exposure_line(p: dict) -> str:
    """'You hold 40 — ₹1,23,600, 35% of your book.'"""
    bits = [f"You hold {qty(p['qty'])} — {inr(p['notional'])}"]
    share = p.get("share_of_book")
    if share:
        bits.append(f"{share * 100:.0f}% of your book")
    return ", ".join(bits) + "."


def volume_spike(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    move = p.get("price_change_pct")
    # Arrow, symbol, then the signed figure — the house layout from
    # render.templates.quote_line. Putting the arrow directly against a signed
    # percentage renders "▼-3.2%", which reads as a double negative.
    head = f"{p['symbol']}  {p['rel_volume']:.1f}× normal volume for this hour"
    if move is not None:
        head = (
            f"{arrow(move)} {p['symbol']}  {pct(move)}"
            f" · {p['rel_volume']:.1f}× normal volume for this hour"
        )
    lines = [head, "", _exposure_line(p)]
    held = p.get("unrealised_pct")
    if held is not None:
        lines.append(f"Still {pct(held)} on cost." if held > 0 else f"Now {pct(held)} on cost.")
    lines += ["", "Reply 1 to mute this name today"]
    return "\n".join(lines), p


def breakout(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    what = p["level_type"].replace("_", " ")
    lines = [
        f"{p['symbol']} through its {what} — {inr(p['level_value'])}",
        "",
        _exposure_line(p),
    ]
    if p.get("unrealised_pct") is not None:
        lines.append(f"You're {pct(p['unrealised_pct'])} on cost.")
    lines += ["", "Reply 1 to mute this name today"]
    return "\n".join(lines), p


def oi_buildup(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    label = p["buildup_class"].replace("_", " ")
    move = p.get("price_change_pct")
    lines = [f"{p['symbol']} — {label}, OI {pct(p['oi_change_pct'])}"]
    if move is not None:
        lines.append(f"Price {pct(move)} on the session.")
    lines += ["", "You have a position in this underlying.", "", 'Reply "why" for the detail']
    return "\n".join(lines), p


def news(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    lines = [
        f"{p['symbol']} — {p['category'].replace('_', ' ')}",
        p["headline"],
        "",
        _exposure_line(p),
    ]
    if p.get("unrealised_pct") is not None:
        lines.append(f"You're {pct(p['unrealised_pct'])} on cost.")
    lines += ["", "Reply 1 to mute this name today"]
    return "\n".join(lines), p


def corp_action(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    lines = [f"{p['symbol']} — {p['action_type']}, ex-date {p['ex_date']}"]
    value = p.get("value")
    if value:
        lines.append(f"{inr(value, 2)} per share on {qty(p['qty'])} — {inr(value * p['qty'])}.")
    else:
        lines.append(f"You hold {qty(p['qty'])}.")
    lines += ["", 'Reply "why" for the filing']
    return "\n".join(lines), {**p, "credit": (value or 0) * p["qty"]}


def margin_band_up(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    lines = [
        f"{WARN} Margin now {p['band']} — {p['utilisation'] * 100:.0f}% used, was {p['from_band']}",
        "",
        f"Used {inr(p['used'])} · headroom {inr(p['headroom'])}",
        f"SPAN {inr(p['span'])} · exposure {inr(p['exposure'])}",
        "",
        'Reply "margin" for the breakdown',
    ]
    return "\n".join(lines), p


def expiry_itm_short(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    days = int(p["days_to_expiry"])
    when = "expires today" if days == 0 else f"{days} session{'s' if days > 1 else ''} to expiry"
    lines = [
        f"{WARN} {p['symbol']} — short and in the money, {when}",
        "",
        f"Spot {inr(p['spot'], 2)} · strike {inr(p['strike'], 2)} · {p['moneyness_pct']:.1f}% ITM",
        f"Short {qty(abs(p['net_qty']))} · {signed(p['unrealised'])} unrealised",
        "",
        'Reply "why" for the full position',
    ]
    return "\n".join(lines), p


def delta_drift(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    lines = [
        f"{p['symbol']} — delta now {p['delta_now']:.2f}, was {p['delta_entry']:.2f} at entry",
        "",
        f"Short {qty(abs(p['net_qty']))} · {signed(p['unrealised'])} unrealised.",
        "Directional risk has roughly changed shape since you sold it.",
        "",
        'Reply "why" for the greeks',
    ]
    return "\n".join(lines), p


def pnl_day_move(t: Trigger) -> tuple[str, dict]:
    p = t.payload
    lines = [
        f"Day P&L {signed(p['day_pnl'])} — {p['sigma']:.1f}× your usual day",
        "",
        f"Your typical daily swing is about {inr(p['daily_sigma_rupees'])}.",
        f"Book {inr(p['total_value'])}.",
        "",
        'Reply "portfolio" for the breakdown',
    ]
    return "\n".join(lines), p


COMPOSERS = {
    "market.volume_spike": volume_spike,
    "market.breakout": breakout,
    "market.oi_buildup": oi_buildup,
    "news.position_scoped": news,
    "corp.action": corp_action,
    "margin.band_up": margin_band_up,
    "expiry.itm_short": expiry_itm_short,
    "greeks.delta_drift": delta_drift,
    "pnl.day_move": pnl_day_move,
}


def compose(trigger: Trigger) -> Alert:
    """Render a trigger, or return an empty Alert if it cannot be rendered safely.

    Two gates, both of which withhold rather than degrade: every number must
    trace to a tool result (I1), and nothing may read as advice. A template
    that drifts into "you should" is the same defect as a hallucinated figure —
    it just fails a different audit.
    """
    fn = COMPOSERS.get(trigger.rule_id)
    if fn is None:
        return Alert("", {}, trigger.rule_id)
    body, slots = fn(trigger)
    body = guard(body, slots, where=trigger.rule_id)
    if body:
        violations = advice_violations(body)
        if violations:
            log.error(
                "advice-shaped phrasing in %s (%s); message withheld",
                trigger.rule_id,
                ", ".join(violations),
            )
            return Alert("", slots, trigger.rule_id)
    return Alert(body, slots, trigger.rule_id)
