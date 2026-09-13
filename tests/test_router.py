import pytest

from app.router.fastpath import Intent, Path, classify


def resolves(_: str) -> bool:
    return True


def never_resolves(_: str) -> bool:
    return False


@pytest.mark.parametrize(
    "text,intent",
    [
        ("portfolio", Intent.PORTFOLIO_SUMMARY),
        ("pf", Intent.PORTFOLIO_SUMMARY),
        ("holdings", Intent.PORTFOLIO_SUMMARY),
        ("positions", Intent.POSITIONS_OPEN),
        ("pos", Intent.POSITIONS_OPEN),
        ("orders", Intent.ORDERS_OPEN),
        ("pending", Intent.ORDERS_OPEN),
        ("margin", Intent.MARGIN_AVAILABLE),
        ("balance", Intent.MARGIN_AVAILABLE),
        ("funds", Intent.MARGIN_AVAILABLE),
        ("market", Intent.MARKET_INDEX),
        ("nifty", Intent.MARKET_INDEX),
        ("sensex", Intent.MARKET_INDEX),
        ("pnl", Intent.PORTFOLIO_DAY_CHANGE),
        ("p&l", Intent.PORTFOLIO_DAY_CHANGE),
        ("help", Intent.META_HELP),
        ("link", Intent.META_LINK),
    ],
)
def test_fast_intents(text, intent):
    route = classify(text)
    assert route.intent is intent
    assert route.path is Path.FAST


@pytest.mark.parametrize(
    "text,intent",
    [
        ("mera pnl kitna hai", Intent.PORTFOLIO_DAY_CHANGE),
        ("aaj ka pnl", Intent.PORTFOLIO_DAY_CHANGE),
        ("kitna balance", Intent.MARGIN_AVAILABLE),
        ("kitna fayda", Intent.PORTFOLIO_DAY_CHANGE),
        ("aaj kya khareeda", Intent.ORDERS_HISTORY),
    ],
)
def test_hinglish_takes_the_fast_path(text, intent):
    """Indian users code-switch constantly (spec §6.4)."""
    route = classify(text)
    assert route.intent is intent
    assert route.path is Path.FAST


def test_case_and_whitespace_insensitive():
    assert classify("  PORTFOLIO  ").intent is Intent.PORTFOLIO_SUMMARY


def test_bare_symbol_is_a_quote_when_it_resolves():
    route = classify("kaynes", resolves_to_instrument=resolves)
    assert route.intent is Intent.MARKET_QUOTE
    assert route.path is Path.FAST


def test_bare_symbol_that_does_not_resolve_goes_to_the_model():
    assert classify("kaynes", resolves_to_instrument=never_resolves).path is Path.LLM


def test_long_message_never_takes_the_bare_symbol_path():
    route = classify("tell me all about kaynes please", resolves_to_instrument=resolves)
    assert route.intent is not Intent.MARKET_QUOTE


@pytest.mark.parametrize(
    "text,intent",
    [
        ("why is silver up", Intent.ANALYSIS_WHY),
        ("what happened to kaynes", Intent.ANALYSIS_WHY),
        ("what is a bull put spread", Intent.ANALYSIS_EXPLAIN),
        ("explain IV", Intent.ANALYSIS_EXPLAIN),
        ("what's my risk", Intent.POSITIONS_RISK),
        ("payoff for my spread", Intent.FNO_PAYOFF),
    ],
)
def test_llm_intents(text, intent):
    route = classify(text)
    assert route.intent is intent
    assert route.path is Path.LLM


@pytest.mark.parametrize(
    "text",
    ["what's the weather", "tell me a joke", "who is the president", "write me python code"],
)
def test_out_of_scope_is_rejected(text):
    """No helpful improvisation — that is what keeps it specialised (spec §1.2)."""
    route = classify(text)
    assert route.intent is Intent.REJECT
    assert route.path is Path.REJECT


def test_empty_message_is_rejected():
    assert classify("").intent is Intent.REJECT
    assert classify("   ").intent is Intent.REJECT


def test_option_chain_and_levels():
    assert classify("nifty option chain").intent is Intent.FNO_CHAIN
    assert classify("where's the max OI").intent is Intent.FNO_LEVELS
    assert classify("delta of my position").intent is Intent.FNO_GREEKS


def test_watch_add():
    assert classify("tell me if nifty hits 25200").intent is Intent.WATCH_ADD
