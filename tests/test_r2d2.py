"""The R2D2 transport: parsing, streaming, and failing loudly.

The fixture in tests/fixtures/r2d2/ is synthetic until someone records a real
call on the work laptop. These tests run against whatever is in that file, so
replacing it is how a shape mismatch gets caught here instead of in production.
"""

import json
from pathlib import Path

import httpx
import pytest

from app.agent.r2d2 import Answer, R2D2Client, R2D2Unavailable

FIXTURE = json.loads((Path(__file__).parent / "fixtures/r2d2/response.json").read_text())


def test_the_fixture_parses():
    a = R2D2Client.parse_consolidated(FIXTURE)
    assert a.text
    assert a.grounded, "no tool results: every figure would be unverifiable"


def test_tool_results_become_the_number_pool():
    """The guard checks against these, so they have to survive parsing."""
    a = R2D2Client.parse_consolidated(FIXTURE)
    from app.compose.guard import allowed

    pool = allowed(a.facts)
    assert 3090 in pool
    assert 123600 in pool


def test_repeated_tools_do_not_overwrite_each_other():
    payload = {
        "response": "two quotes",
        "tools_called": [
            {"name": "get_quote", "output": {"last_price": 100}},
            {"name": "get_quote", "output": {"last_price": 250}},
        ],
    }
    from app.compose.guard import allowed

    pool = allowed(R2D2Client.parse_consolidated(payload).facts)
    assert {100.0, 250.0} <= pool


def test_a_nested_data_envelope_is_unwrapped():
    a = R2D2Client.parse_consolidated({"data": {"answer": "wrapped", "tools_called": []}})
    assert a.text == "wrapped"


def test_an_unknown_shape_raises_and_names_the_keys():
    """Silence beats a half-parsed answer, and the error has to be actionable."""
    with pytest.raises(R2D2Unavailable) as exc:
        R2D2Client.parse_consolidated({"surprise": 1, "other": 2})
    assert "surprise" in str(exc.value)
    assert "_TEXT_KEYS" in str(exc.value)


def test_an_answer_without_tool_results_is_not_grounded():
    a = R2D2Client.parse_consolidated({"response": "no tools here"})
    assert a.text == "no tools here"
    assert not a.grounded


def _client(handler, **kw) -> R2D2Client:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return R2D2Client("http://r2d2.test", "k", http=http, **kw)


@pytest.mark.asyncio
async def test_blocking_post_round_trip():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=FIXTURE)

    a = await _client(handler, stream=False).ask("why is TITAN down", context={"user_id": "u1"})
    assert seen["url"].endswith("/response")
    assert seen["body"]["query"] == "why is TITAN down"
    assert seen["body"]["user_id"] == "u1"
    assert isinstance(a, Answer) and a.grounded


@pytest.mark.asyncio
async def test_streaming_takes_the_consolidated_object_and_beats_a_heartbeat():
    """Tokens are thrown away; the last complete object wins."""
    chunks = [
        'data: {"delta": "TITAN "}',
        'data: {"delta": "is down"}',
        f"data: {json.dumps(FIXTURE)}",
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text="\n".join(chunks))

    beats = []

    async def beat():
        beats.append(1)

    a = await _client(handler, stream=True).ask("q", on_heartbeat=beat)
    assert a.text == FIXTURE["response"]
    assert a.grounded
    assert beats, "no heartbeat: the typing indicator would expire mid-answer"


@pytest.mark.asyncio
async def test_ndjson_streams_parse_too():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"answer": "plain ndjson"}) + "\n")

    assert (await _client(handler, stream=True).ask("q")).text == "plain ndjson"


@pytest.mark.asyncio
async def test_a_stream_with_no_answer_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='data: {"delta": "..."}\ndata: [DONE]')

    with pytest.raises(R2D2Unavailable):
        await _client(handler, stream=True).ask("q")


@pytest.mark.asyncio
async def test_a_transport_error_becomes_R2D2Unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with pytest.raises(R2D2Unavailable):
        await _client(handler, stream=False).ask("q")
