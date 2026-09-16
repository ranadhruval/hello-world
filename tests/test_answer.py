"""What GR-2 is willing to send when someone else wrote the prose.

R2D2 is a good answering engine and a foreign one: it writes for a web surface,
it can phrase things as advice, and its figures come from tools GR-2 never
called. Every test here is a case where the answer is withheld and the typed
reply goes out instead, because withholding is the only behaviour that keeps
the guarantees the rest of the desk makes.
"""

import pytest

from app.agent.answer import Answerer
from app.agent.r2d2 import Answer, R2D2Unavailable
from app.agent.shape import shape
from app.infra import CircuitBreaker

FACTS = {"get_quote_0": {"last_price": 3090, "day_change_perc": -3.2}}


class FakeR2D2:
    """Scripted answers. `raises` wins over `answer` when both are set."""

    def __init__(self, answer: Answer | None = None, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.asked: list[str] = []

    async def ask(self, question, *, context=None, on_heartbeat=None):
        self.asked.append(question)
        if self.raises:
            raise self.raises
        return self.answer


def answerer(answer=None, raises=None, **kw) -> tuple[Answerer, FakeR2D2]:
    client = FakeR2D2(answer, raises)
    return Answerer(client, **kw), client


async def reply(answer=None, raises=None, **kw):
    a, _ = answerer(answer, raises, **kw)
    return await a.reply("why is TITAN down", wa_id="919999999999")


@pytest.mark.asyncio
async def test_a_figure_from_a_tool_result_is_sent():
    out = await reply(Answer(text="TITAN is at ₹3,090, down 3.2% today.", facts=FACTS))
    assert out is not None
    assert "3,090" in out.text


@pytest.mark.asyncio
async def test_a_figure_no_tool_returned_is_withheld():
    """The one failure this product cannot absorb is a confident wrong number."""
    assert await reply(Answer(text="TITAN is at ₹4,100 today.", facts=FACTS)) is None


@pytest.mark.asyncio
async def test_an_answer_with_no_numbers_needs_no_tool_results():
    out = await reply(Answer(text="Titan makes watches and jewellery.", facts={}))
    assert out is not None


@pytest.mark.asyncio
async def test_any_figure_without_tool_results_is_withheld():
    assert await reply(Answer(text="TITAN is at ₹3,090.", facts={})) is None


@pytest.mark.asyncio
async def test_advice_is_withheld_however_well_grounded():
    """A compliance boundary, not a matter of taste."""
    grounded = "TITAN is at ₹3,090. You should book profits here."
    assert await reply(Answer(text=grounded, facts=FACTS)) is None


@pytest.mark.asyncio
async def test_an_unavailable_r2d2_falls_back_quietly():
    assert await reply(raises=R2D2Unavailable("connection refused")) is None


@pytest.mark.asyncio
async def test_an_unexpected_error_also_falls_back():
    """The desk must keep its turn whatever the transport does."""
    assert await reply(raises=ValueError("something else entirely")) is None


@pytest.mark.asyncio
async def test_the_circuit_opens_so_a_dead_r2d2_costs_one_timeout_not_every_one():
    breaker = CircuitBreaker(threshold=2)
    a, client = answerer(raises=R2D2Unavailable("down"), breaker=breaker)
    for _ in range(2):
        assert await a.reply("q", wa_id="91") is None
    assert breaker.is_open
    asked = len(client.asked)
    assert await a.reply("q", wa_id="91") is None
    assert len(client.asked) == asked, "asked a circuit that was already open"


@pytest.mark.asyncio
async def test_strict_off_sends_the_untraceable_figure_anyway():
    """Off by default and logged; the escape hatch has to actually work."""
    out = await reply(Answer(text="TITAN is at ₹4,100.", facts=FACTS), strict_numbers=False)
    assert out is not None and "4,100" in out.text


@pytest.mark.asyncio
async def test_local_facts_widen_the_pool():
    """Numbers GR-2 fetched itself count as tool results too."""
    a, _ = answerer(Answer(text="Your 40 shares are worth ₹1,23,600.", facts={}))
    out = await a.reply("q", wa_id="91", local_facts={"holdings": {"qty": 40, "value": 123600}})
    assert out is not None


# ---- shaping --------------------------------------------------------


def test_markdown_is_flattened_for_whatsapp():
    out = shape("## Heading\n\n**TITAN** is *down*.\n\n- one\n- two\n\n[link](http://x)")
    assert "##" not in out and "[" not in out
    assert "*TITAN*" in out and "_down_" in out
    assert "• one" in out


def test_long_answers_are_cut_at_a_sentence():
    out = shape("First sentence here. " + "Second sentence padding. " * 80, max_chars=120)
    assert len(out) <= 120
    assert out.endswith(".")


def test_a_number_is_never_cut_in_half():
    """'₹29,08' is not a shortened number, it is a wrong one."""
    out = shape("x" * 95 + " and the total is ₹29,08,281 exactly.", max_chars=100)
    assert "29,08," not in out or "29,08,281" in out


def test_line_count_is_capped():
    assert len(shape("\n".join(f"line {i}" for i in range(40))).split("\n")) <= 8


# ---- the worker seam ------------------------------------------------
#
# The whole integration hangs off one env var. These are the tests that say so.

import asyncio  # noqa: E402

from app.channel.console import ConsoleChannel, inbound  # noqa: E402
from app.render import templates as tpl  # noqa: E402
from app.worker import Worker  # noqa: E402
from tests.test_dispatch import make_desk  # noqa: E402


def worker_with(real_index, answerer=None) -> tuple[Worker, ConsoleChannel]:
    channel = ConsoleChannel()
    desk = make_desk(real_index)

    async def desk_for(wa_id):
        return desk

    return Worker(channel, real_index, desk_for=desk_for, answerer=answerer), channel


@pytest.mark.asyncio
async def test_with_no_r2d2_an_open_question_behaves_exactly_as_before(real_index):
    """The regression guard: unset R2D2_BASE_URL must change nothing."""
    worker, channel = worker_with(real_index)
    await worker.handle(inbound("why is silver up today"))
    await asyncio.sleep(2)
    assert channel.sent[0].text == tpl.OUT_OF_SCOPE


@pytest.mark.asyncio
async def test_with_r2d2_the_same_question_gets_answered(real_index):
    a, client = answerer(Answer(text="Silver is at ₹3,090 an ounce.", facts=FACTS))
    worker, channel = worker_with(real_index, answerer=a)
    await worker.handle(inbound("why is silver up today"))
    await asyncio.sleep(2)
    assert client.asked == ["why is silver up today"]
    assert "3,090" in channel.sent[0].text


@pytest.mark.asyncio
async def test_a_withheld_answer_falls_back_to_the_typed_reply(real_index):
    a, _ = answerer(Answer(text="Silver is at ₹4,100.", facts=FACTS))
    worker, channel = worker_with(real_index, answerer=a)
    await worker.handle(inbound("why is silver up today"))
    await asyncio.sleep(2)
    assert channel.sent[0].text == tpl.OUT_OF_SCOPE


@pytest.mark.asyncio
async def test_a_fast_path_question_never_reaches_r2d2(real_index):
    """Templates answer what they can: no latency, no spend, no guard risk."""
    a, client = answerer(Answer(text="should not be used", facts={}))
    worker, channel = worker_with(real_index, answerer=a)
    await worker.handle(inbound("portfolio"))
    await asyncio.sleep(2)
    assert client.asked == []


@pytest.mark.asyncio
async def test_an_out_of_scope_message_never_reaches_r2d2(real_index):
    """Path.REJECT keeps its deterministic refusal; a joke costs nothing."""
    a, client = answerer(Answer(text="should not be used", facts={}))
    worker, channel = worker_with(real_index, answerer=a)
    await worker.handle(inbound("tell me a joke"))
    await asyncio.sleep(2)
    assert client.asked == []
    assert channel.sent[0].text == tpl.OUT_OF_SCOPE
