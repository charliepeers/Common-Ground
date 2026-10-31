"""A stand-in for anthropic.AsyncAnthropic that records requests and replays canned answers."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import anthropic
import httpx2


def text_message(text: str, stop_reason: str = "end_turn") -> NS:
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=text)], stop_reason=stop_reason)


def api_error(cls, status: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("refused", response=httpx2.Response(status, request=request), body=None)


class _Stream:
    def __init__(self, text: str, chunk: int):
        self.text, self.chunk = text, chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        yield NS(type="content_block_start", content_block=NS(type="thinking"))
        yield NS(type="content_block_start", content_block=NS(type="text"))
        for i in range(0, len(self.text), self.chunk):
            yield NS(type="content_block_delta", delta=NS(type="text_delta", text=self.text[i:i + self.chunk]))

    async def get_final_message(self):
        return text_message(self.text)


class FakeClient:
    """answers: queue of str (JSON text), NS messages, or exceptions, served in order to create() and stream()."""

    def __init__(self, answers: list, chunk: int = 7):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.chunk = chunk
        self.beta = NS(messages=NS(create=self._create, stream=self._stream))

    def with_options(self, **kwargs):
        return self

    def _next(self, kwargs):
        self.requests.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _create(self, **kwargs):
        answer = self._next(kwargs)
        return text_message(answer) if isinstance(answer, str) else answer

    def _stream(self, **kwargs):
        answer = self._next(kwargs)
        return _Stream(answer if isinstance(answer, str) else json.dumps(answer), self.chunk)


__all__ = ["FakeClient", "text_message", "api_error", "anthropic", "NS"]
