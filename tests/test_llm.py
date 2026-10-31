import json
from types import SimpleNamespace as NS

import anthropic
import pytest

from server.jsonstream import ClaimStream
from server.llm import LLM
from server.search import Source
from tests.fakes import FakeClient, api_error, text_message

CLAIMS = {
    "claims": [
        {
            "quote": 'the "Great Wall" is visible {from} space', "claim": "The Great Wall of China is visible from space.",
            "kind": "fact", "verdict": "false", "confidence": "high",
            "explanation": "Not visible to the naked eye from orbit.", "correction": "It isn't.",
            "source_ids": [2, 2, 9], "needs_search": False, "query": "", "voices": [],
        },
        {
            "quote": "LLMs can't reason", "claim": "Large language models cannot reason.", "kind": "contested",
            "verdict": "unverifiable", "confidence": "high", "explanation": "Depends on what counts as reasoning.",
            "correction": "", "source_ids": [], "needs_search": False, "query": "whether LLMs can reason",
            "voices": ["Andrej Karpathy"],
        },
    ]
}
SOURCES = [Source("A", "https://a.example/1"), Source("NASA", "https://nasa.gov/wall", "2023-01-01", "hard to see")]


@pytest.mark.parametrize("chunk", [1, 3, 50, 10_000])
def test_claim_stream_announces_each_claim_before_finishing_it(chunk):
    text = json.dumps(CLAIMS)
    reader, events = ClaimStream(), []
    for i in range(0, len(text), chunk):
        events += reader.feed(text[i:i + chunk])
    assert [(e, i) for e, i, _ in events] == [("early", 0), ("done", 0), ("early", 1), ("done", 1)]
    assert events[0][2] == {k: CLAIMS["claims"][0][k] for k in ("quote", "claim", "kind")}
    assert events[1][2] == CLAIMS["claims"][0]
    assert events[3][2] == CLAIMS["claims"][1]


def test_claim_stream_lights_up_before_the_object_closes():
    text = json.dumps(CLAIMS)
    cut = text.index('"verdict"')  # quote, claim and kind are in; the rest isn't
    reader = ClaimStream()
    events = reader.feed(text[:cut])
    assert [(e, i) for e, i, _ in events] == [("early", 0)]
    assert reader.feed(text[cut:])[0][:2] == ("done", 0)


async def test_check_streams_claims_with_sources_resolved_and_sends_the_right_request():
    client = FakeClient([json.dumps(CLAIMS)])
    llm = LLM(client, fast=True)
    early, done = [], []

    async def on_early(i, data):
        early.append((i, data["quote"]))

    async def on_done(i, data):
        done.append((i, data))

    timing = await llm.check("line", "Alex", [("Sam", "before")], SOURCES, True, on_early, on_done)
    assert [i for i, _ in early] == [0, 1] and [i for i, _ in done] == [0, 1]
    assert done[0][1]["sources"] == [SOURCES[1].as_dict()]  # dedupes ids, drops out-of-range ones
    assert "source_ids" not in done[0][1]
    assert timing["llm_ms"] >= 0 and "llm_first_ms" in timing

    request = client.requests[0]
    assert request["model"] == "claude-sonnet-5-5"  # the live-path default
    assert "speed" not in request and request["betas"] == ["server-side-fallback-2026-07-01"]  # no fast mode on Sonnet
    assert request["fallbacks"] == "default"
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in request
    user = request["messages"][0]["content"]
    assert "[2] NASA (nasa.gov, 2023-01-01)" in user


async def test_fast_mode_refused_falls_back_and_stays_off():
    client = FakeClient([api_error(anthropic.BadRequestError, 400), '{"claims": []}', '{"claims": []}'])
    llm = LLM(client, fast=True, live_model="claude-opus-5-5")

    async def nothing(i, data):
        pass

    await llm.check("hi", "A", [], [], False, nothing, nothing)
    assert llm.fast is False
    assert client.requests[1].get("speed") is None and "fast-mode-2026-02-01" not in client.requests[1]["betas"]
    await llm.check("hi", "A", [], [], False, nothing, nothing)
    assert len(client.requests) == 3 and client.requests[2].get("speed") is None


async def test_fast_mode_rate_limit_falls_back_for_that_call_only():
    client = FakeClient([api_error(anthropic.RateLimitError, 429), '{"verdict":"true","confidence":"high","explanation":"x","correction":"","source_ids":[1]}'])
    llm = LLM(client, fast=True, live_model="claude-opus-5-5")
    result = await llm.verify("c", "q", "A", SOURCES)
    assert result["verdict"] == "true" and result["sources"] == [SOURCES[0].as_dict()]
    assert llm.fast is True


async def test_deep_check_only_counts_pages_the_search_returned():
    search_result = NS(
        type="web_search_tool_result",
        content=[NS(type="web_search_result", url="https://www.weather.gov/safety/lightning-myths", title="Lightning myths", page_age="2024-05-01", encrypted_content="x")],
    )
    submit = NS(
        type="tool_use", name="submit_verdict", id="t1",
        input={"verdict": "false", "confidence": "high", "explanation": "x", "correction": "y", "note": "n",
               "source_urls": ["https://weather.gov/safety/lightning-myths/", "https://made-up.example/page"]},
    )
    paused = NS(content=[NS(type="server_tool_use", name="web_search", id="s1", input={})], stop_reason="pause_turn")
    client = FakeClient([paused, NS(content=[search_result, submit], stop_reason="tool_use")])
    result = await LLM(client, fast=False).deep_check("c", "q", "A", pushback={"verdict": "false", "explanation": "e", "argument": "a"})
    assert [s["url"] for s in result["sources"]] == ["https://www.weather.gov/safety/lightning-myths"]
    assert result["note"] == "n"
    # the paused turn was sent back as is, with no extra user message
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "assistant"]
    tools = client.requests[0]["tools"]
    assert tools[0]["type"] == "web_search_20260209" and tools[1]["strict"] is True


async def test_perspectives_drop_quotes_that_are_not_verbatim():
    sources = [Source("Post", "https://x.com/k/status/1", "2025-10-01", "LLMs are  “ghosts”, not animals. They are trained differently.")]
    answer = {"perspectives": [
        {"person": "Andrej Karpathy", "stance": "complicates", "summary": "s", "quote": "LLMs are \"ghosts\", not animals.", "source_id": 1},
        {"person": "Andrej Karpathy", "stance": "disagrees", "summary": "s", "quote": "LLMs reason just like people.", "source_id": 1},
        {"person": "Nobody", "stance": "agrees", "summary": "s", "quote": "x", "source_id": 5},
    ]}
    takes = await LLM(FakeClient([json.dumps(answer)]), fast=False).perspectives("c", "Yann", "crux", [], sources)
    assert [t["quote"] for t in takes] == ['LLMs are "ghosts", not animals.']
    assert takes[0]["source"]["url"] == "https://x.com/k/status/1"


async def test_refusal_yields_unverifiable():
    client = FakeClient([text_message("", stop_reason="refusal")])
    result = await LLM(client, fast=False).verify("c", "q", "A", SOURCES)
    assert result["verdict"] == "unverifiable" and result["sources"] == []


async def test_speaker_names_follow_turn_order_and_are_never_expanded():
    lines = [
        ("Speaker A", "Welcome everyone. Thanks, Yann, for coming."),
        ("Speaker B", "Happy to be here."),
        ("Speaker B", "I'm Yann, by the way."),
        ("Speaker C", "Great point, Yann."),
    ]
    guesses = [
        ("Speaker B", "Yann", "Thanks, Yann, for coming", "next"),  # kept: B is the next to speak
        ("Speaker B", "Yann LeCun", "Thanks, Yann, for coming", "next"),  # dropped: the surname was never said
        ("Speaker A", "Yann", "Thanks, Yann, for coming", "current"),  # dropped: thanking someone isn't introducing yourself
        ("Speaker B", "Yann", "the famous Yann", "next"),  # dropped: not in the transcript
        ("Speaker B", "Yann", "I'm Yann", "current"),  # kept: a self-introduction
        ("Speaker B", "Yann", "Great point, Yann", "previous"),  # kept: B spoke just before C
        ("Speaker C", "Yann", "Great point, Yann", "previous"),  # dropped: the cue points at B, not C
    ]
    answer = {"guesses": [dict(zip(("speaker", "name", "evidence", "points_to"), g)) for g in guesses]}
    kept = await LLM(FakeClient([json.dumps(answer)]), fast=False).name_speakers(lines)
    assert [(g["speaker"], g["evidence"]) for g in kept] == [
        ("Speaker B", "Thanks, Yann, for coming"), ("Speaker B", "I'm Yann"), ("Speaker B", "Great point, Yann"),
    ]


async def test_fast_mode_and_fallbacks_only_go_to_models_that_support_them():
    client = FakeClient(['{"verdict":"true","confidence":"high","explanation":"x","correction":"","source_ids":[]}'] * 3)
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        await LLM(client, fast=True, live_model=model).verify("c", "q", "A", [])
    opus, sonnet, haiku = client.requests
    assert opus["speed"] == "fast" and set(opus["betas"]) == {"server-side-fallback-2026-07-01", "fast-mode-2026-02-01"}
    assert "speed" not in sonnet and sonnet["fallbacks"] == "default"
    assert not {"speed", "fallbacks", "betas"} & haiku.keys()  # Haiku 5.5 has no server-side fallback
