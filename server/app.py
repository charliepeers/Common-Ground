"""HTTP + WebSocket server: serves the page and runs one listening session per connection.

    uvicorn server.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .backends import LiveBackend, OfflineBackend
from .llm import LLM
from .search import ExaSearch
from .segmenter import Segmenter
from .session import Session
from .stt import SpeechStream, provider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-10s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("cg.app")


def make_backend():
    if config.OFFLINE:
        return OfflineBackend.load(config.FIXTURES)
    search = ExaSearch(config.EXA_API_KEY, config.EXA_SEARCH_TYPE) if config.EXA_API_KEY else None
    return LiveBackend(LLM(), search)


BACKEND = make_backend()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if isinstance(BACKEND, LiveBackend):
        asyncio.create_task(BACKEND.warm_up())
    yield


app = FastAPI(title="Common Ground", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/config")
async def settings():
    live = isinstance(BACKEND, LiveBackend)
    stt = provider() if live else None
    return {
        "model": config.LIVE_MODEL,
        "pushback_model": config.PUSHBACK_MODEL,
        "offline": config.OFFLINE,
        "fast_mode": live and BACKEND.llm.uses_fast(config.LIVE_MODEL),
        "evidence": BACKEND.evidence,
        "server_stt": stt is not None,
        "stt_name": stt[0].name if stt else None,
        # Latency is shown only when it was measured, which means live mode: the test data has no timings.
        "timings_measured": live,
    }


@app.get("/api/demos")
async def demos():
    return json.loads((config.DEMO_DIR / "script.json").read_text())


@app.websocket("/ws")
async def listen(ws: WebSocket):
    await ws.accept()
    outbox: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await outbox.put(event)

    async def pump() -> None:  # one writer, so events reach the page in the order they were emitted
        while True:
            await ws.send_json(await outbox.get())

    sender = asyncio.create_task(pump())
    session = Session(emit, BACKEND)
    stream: SpeechStream | None = None
    segmenter = Segmenter()

    async def on_transcript(message: dict) -> None:
        caption, ready = segmenter.feed(message)
        for statement in ready:  # finished lines first: the page clears the caption when a line lands
            await session.hear(statement["speaker"], statement["text"], statement["spoken_at"])
        if caption:
            await emit(caption)

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if stream:
                    await stream.send(message["bytes"])
                continue
            data = json.loads(message.get("text") or "{}")
            kind = data.get("type")
            if kind == "say":
                await session.hear(data.get("speaker") or "Speaker", data.get("text", ""))
            elif kind == "voices":
                session.set_voices(data.get("names", []))
            elif kind == "rename":
                session.rename(data.get("speaker", ""), data.get("name", ""))
            elif kind == "pushback":
                session.spawn(session.pushback(data.get("claim_id", ""), data.get("argument", "")))
            elif kind == "start_audio":
                stt = provider() if isinstance(BACKEND, LiveBackend) else None
                if not stt:
                    await emit({"type": "error", "message": "Server speech-to-text needs ASSEMBLYAI_API_KEY or DEEPGRAM_API_KEY. Pick 'Browser speech' instead."})
                    continue
                if stream:
                    await stream.stop()
                stream_class, key = stt
                # Ready before the first message can arrive. Starting the clock at connect time makes
                # "seconds since the words were spoken" err high, never optimistic.
                segmenter = stream_class.segmenter(time.time())
                stream = stream_class(key, on_transcript)
                try:
                    await stream.start()
                except Exception as err:
                    stream = None
                    await emit({"type": "error", "message": f"Couldn't connect to {stream_class.name}: {err}"})
                    continue
                await emit({"type": "audio", "state": "on"})
            elif kind == "stop_audio" and stream:
                await stream.stop()
                stream = None
                await emit({"type": "audio", "state": "off"})
    finally:
        if stream:
            await stream.stop()
        await session.close()
        sender.cancel()
