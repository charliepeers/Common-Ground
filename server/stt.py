"""Relays browser audio (16 kHz mono 16-bit PCM) to a streaming speech-to-text API.

AssemblyAI Universal-3.6 Pro is preferred when its key is set. On the Pipecat streaming benchmark
(https://github.com/pipecat-ai/stt-benchmark, 1,000 voice-agent turns, September 2026 scoring) it
had 0.89% mean WER and a 307 ms median time to final transcript; Deepgram Nova-3 had 1.32% and
247 ms. For a fact-checker a misheard number costs more than 60 ms. Neither latency has been
measured in this app yet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from . import config
from .segmenter import Segmenter, TurnSegmenter

log = logging.getLogger("cg.stt")

OnMessage = Callable[[dict], Awaitable[None]]


class SpeechStream:
    name = ""
    segmenter: type = Segmenter
    close_messages: list[dict] = []
    keep_alive: dict | None = None  # sent when no audio has flowed for a few seconds

    def __init__(self, api_key: str, on_message: OnMessage):
        self.api_key = api_key
        self.on_message = on_message
        self.ws = None
        self._last_audio = 0.0
        self._tasks: list[asyncio.Task] = []

    def url(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict:
        raise NotImplementedError

    async def start(self) -> None:
        self.ws = await connect(self.url(), additional_headers=self.headers(), max_size=None)
        self._last_audio = time.monotonic()
        self._tasks = [asyncio.create_task(self._read())]
        if self.keep_alive:
            self._tasks.append(asyncio.create_task(self._ping()))

    async def send(self, chunk: bytes) -> None:
        if self.ws is not None:
            self._last_audio = time.monotonic()
            await self.ws.send(chunk)

    async def stop(self) -> None:
        if self.ws is None:
            return
        try:
            for message in self.close_messages:
                await self.ws.send(json.dumps(message))
            await asyncio.wait_for(self._tasks[0], timeout=3)  # let the last results arrive
        except Exception as err:
            log.info("%s close: %s", self.name, err)
        finally:
            for task in self._tasks:
                task.cancel()
            await self.ws.close()
            self.ws = None

    async def _read(self) -> None:
        async for raw in self.ws:
            if isinstance(raw, str):
                await self.on_message(json.loads(raw))

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(3)
            if time.monotonic() - self._last_audio > 3:
                await self.ws.send(json.dumps(self.keep_alive))


class AssemblyAIStream(SpeechStream):
    name = "AssemblyAI"
    segmenter = TurnSegmenter
    close_messages = [{"type": "ForceEndpoint"}, {"type": "Terminate"}]

    def url(self) -> str:
        params = {
            "speech_model": config.ASSEMBLYAI_MODEL,
            "encoding": "pcm_s16le",
            "sample_rate": "16000",
            "speaker_labels": "true",
            "max_speakers": "4",  # AssemblyAI suggests a little above the real head count
        }
        if config.ASSEMBLYAI_MAX_TURN_SILENCE:
            params["max_turn_silence"] = config.ASSEMBLYAI_MAX_TURN_SILENCE
        return f"wss://streaming.assemblyai.com/v3/ws?{urlencode(params)}"

    def headers(self) -> dict:
        return {"Authorization": self.api_key}

    async def _read(self) -> None:
        async for raw in self.ws:
            if isinstance(raw, str):
                message = json.loads(raw)
                if message.get("type") == "Begin":
                    # Unknown parameters are ignored silently, so confirm which model is actually running.
                    log.info("AssemblyAI session %s, model %s", message.get("id"), (message.get("configuration") or {}).get("model"))
                await self.on_message(message)


class DeepgramStream(SpeechStream):
    name = "Deepgram"
    segmenter = Segmenter
    close_messages = [{"type": "Finalize"}, {"type": "CloseStream"}]
    keep_alive = {"type": "KeepAlive"}  # Deepgram closes a stream that gets no data for about 10 s

    def url(self) -> str:
        params = {
            "model": config.DEEPGRAM_MODEL,
            "language": "en",
            "smart_format": "true",
            "punctuate": "true",
            # Turns diarization on. The older diarize=true is deprecated, and sending both is rejected.
            "diarize_model": "latest",
            "interim_results": "true",
            "endpointing": "300",
            "utterance_end_ms": "1000",
            "vad_events": "true",
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
        }
        return f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"

    def headers(self) -> dict:
        return {"Authorization": f"Token {self.api_key}"}


PROVIDERS = {"assemblyai": (AssemblyAIStream, "ASSEMBLYAI_API_KEY"), "deepgram": (DeepgramStream, "DEEPGRAM_API_KEY")}


def provider() -> tuple[type[SpeechStream], str] | None:
    """The configured speech-to-text class and its key, or None if none is set up."""
    entry = PROVIDERS.get(config.STT_PROVIDER)
    if not entry or not getattr(config, entry[1]):
        return None
    return entry[0], getattr(config, entry[1])
