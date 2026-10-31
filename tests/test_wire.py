"""The real anthropic SDK and the real Exa client against mock HTTP transports.

The other tests use a fake client; these check what actually goes over the wire (headers, body)
and that the SDK's own stream parser hands our code the events it expects.
"""
import json

import anthropic
import httpx
import httpx2

from server.backends import LiveBackend
from server.llm import LLM
from server.search import ExaSearch
from server.session import Session


def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def streamed_text(text: str, chunk: int = 9) -> bytes:
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}
    events = [
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    ]
    events += [{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text[i:i + chunk]}}
               for i in range(0, len(text), chunk)]
    events += [
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    ]
    return sse(events)


def message(content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 20}}


CHECK = {"claims": [{
    "quote": "Joe Biden is the 47th president", "claim": "Joe Biden is the 47th president of the United States.",
    "kind": "fact", "verdict": "false", "confidence": "high", "explanation": "Biden was the 46th.",
    "correction": "Trump is the 47th.", "source_ids": [1], "needs_search": False, "query": "", "voices": [],
}]}
EXA_RESULT = {"results": [{"title": "List of presidents", "url": "https://en.wikipedia.org/wiki/List", "publishedDate": "2025-02-01T00:00:00.000Z",
                           "highlights": ["46 Joe Biden", "47 Donald Trump"], "highlightScores": [0.9, 0.8]}]}


class Recorder:
    def __init__(self, reject_fast: bool = False):
        self.requests: list[tuple[dict, dict]] = []
        self.reject_fast = reject_fast

    def anthropic(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append((dict(request.headers), body))
        if self.reject_fast and body.get("speed") == "fast":
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "fast mode is not available"}})
        if body.get("stream"):
            return httpx2.Response(200, content=streamed_text(json.dumps(CHECK)), headers={"content-type": "text/event-stream"})
        if body.get("tools"):  # the deep check: a search, then the verdict through the client tool
            return httpx2.Response(200, json=message([
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
                {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                    {"type": "web_search_result", "url": "https://www.nasa.gov/x", "title": "NASA", "page_age": "2009-04-02", "encrypted_content": "e"}]},
                {"type": "tool_use", "id": "toolu_1", "name": "submit_verdict", "input": {
                    "verdict": "false", "confidence": "high", "explanation": "x", "correction": "y", "note": "n",
                    "source_urls": ["https://nasa.gov/x"]}},
            ], stop_reason="tool_use"))
        return httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(
            {"verdict": "true", "confidence": "high", "explanation": "ok", "correction": "", "source_ids": [1]})}]))


def make_backend(recorder: Recorder, exa_requests: list, live_model: str = "claude-sonnet-5-5") -> LiveBackend:
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(recorder.anthropic))
    client = anthropic.AsyncAnthropic(api_key="test-key", base_url="http://anthropic.test", http_client=http, max_retries=0)

    def exa(request: httpx.Request) -> httpx.Response:
        exa_requests.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json=EXA_RESULT)

    search = ExaSearch("exa-key", http=httpx.AsyncClient(transport=httpx.MockTransport(exa)))
    return LiveBackend(LLM(client, fast=True, live_model=live_model), search)


async def run_line(backend, text="Okay so Joe Biden is the 47th president, right?"):
    events = []

    async def emit(event):
        events.append(event)

    session = Session(emit, backend)
    await session.hear("Alex", text)
    while session._tasks:
        import asyncio
        await asyncio.sleep(0.01)
    return session, events


async def test_streamed_check_over_the_wire():
    recorder, exa_requests = Recorder(), []
    _, events = await run_line(make_backend(recorder, exa_requests))
    assert [e["type"] for e in events] == ["line", "claim", "verdict"]
    verdict = events[2]
    assert verdict["verdict"] == "false" and verdict["sources"][0]["url"] == "https://en.wikipedia.org/wiki/List"
    assert verdict["sources"][0]["published"] == "2025-02-01" and verdict["sources"][0]["snippet"] == "46 Joe Biden … 47 Donald Trump"

    headers, body = recorder.requests[0]
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert headers["authorization"] == "Bearer test-key"  # anthropic 2.x sends the key as a bearer token
    assert headers["anthropic-version"] == "2026-10-21"  # the API version that rejects sampling parameters
    assert body["model"] == "claude-sonnet-5-5" and body["stream"] is True
    assert "speed" not in body and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low" and body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert not {"temperature", "top_p", "top_k", "thinking"} & body.keys()

    exa_headers, exa_body = exa_requests[0]
    assert exa_headers["x-api-key"] == "exa-key"
    assert exa_body["type"] == "lite" and exa_body["query"].startswith("Okay so Joe Biden")
    assert exa_body["contents"] == {"highlights": {"maxCharacters": 700}}
    assert exa_body["numResults"] == 10 and exa_body["objective"].startswith("Evidence to verify or refute")


async def test_refused_fast_mode_retries_at_standard_speed_over_the_wire():
    recorder = Recorder(reject_fast=True)
    backend = make_backend(recorder, [], live_model="claude-opus-5-5")  # fast mode only exists on Opus
    _, events = await run_line(backend)
    assert events[-1]["type"] == "verdict"
    assert [b.get("speed") for _, b in recorder.requests] == ["fast", None]
    assert recorder.requests[1][0]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert backend.llm.fast is False


async def test_pushback_parses_server_tool_blocks_from_the_wire():
    recorder = Recorder()
    session, events = await run_line(make_backend(recorder, []))
    await session.pushback(events[1]["id"], "it was photographed from the ISS")
    final = events[-1]
    assert final["verdict"] == "false" and final["pushback"]["note"] == "n"
    assert [s["url"] for s in final["sources"]] == ["https://www.nasa.gov/x"]  # matched despite the missing www.
    _, body = recorder.requests[-1]
    assert body["tools"][0] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    assert body["tools"][1]["strict"] is True and body["output_config"] == {"effort": "high"}
    assert body["model"] == "claude-sonnet-5-5"
