"""One listening session: lines come in, claims, verdicts and perspectives go out to the screen."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

import anthropic

from .backends import NotRecorded
from .util import ms_since, normalize

log = logging.getLogger("cg.session")

Emit = Callable[[dict], Awaitable[None]]
CONTEXT_LINES = 6
MAX_CLAIMS_PER_LINE = 3
MAX_VOICES = 4
ANONYMOUS = re.compile(r"^Speaker [A-Z0-9]+$")  # how diarization labels people it can't name
NAMING_EVERY = 4  # lines between attempts to name anonymous speakers


def with_receipts(verdict: dict) -> dict:
    """No receipts, no verdict: true, false and misleading must cite a source or become unverifiable."""
    if verdict.get("verdict") in ("true", "false", "misleading") and not verdict.get("sources"):
        return {**verdict, "verdict": "unverifiable", "leaning": verdict["verdict"]}
    return verdict


def describe(err: Exception) -> str:
    if isinstance(err, anthropic.AuthenticationError):
        return "Claude rejected the API key: set ANTHROPIC_API_KEY in .env, or run with OFFLINE=1 for the recorded demo."
    return f"{type(err).__name__}: {err}"


class Session:
    def __init__(self, emit: Emit, backend, max_parallel: int = 8):
        self.emit = emit
        self.backend = backend
        self.voices: list[str] = []
        self.names: dict[str, str] = {}  # "Speaker 1" -> "Yann LeCun", so Claude knows who is talking
        self.lines: list[dict] = []
        self.claims: dict[str, dict] = {}
        self.settled: dict[str, dict] = {}  # normalized claim -> its final verdict
        self.suggested: set[str] = set()  # labels we've already proposed a name for
        self._since_naming = 0
        self._limit = asyncio.Semaphore(max_parallel)
        self._tasks: set[asyncio.Task] = set()
        self._count = 0

    def _id(self, prefix: str) -> str:
        self._count += 1
        return f"{prefix}{self._count}"

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def set_voices(self, names: list[str]) -> None:
        self.voices = [n.strip() for n in names if n.strip()][:MAX_VOICES]

    def rename(self, speaker: str, name: str) -> None:
        if speaker and name.strip():
            self.names[speaker] = name.strip()

    def who(self, speaker: str) -> str:
        return self.names.get(speaker, speaker)

    def _named(self, claim: dict) -> dict:
        return {**claim, "speaker": self.who(claim["speaker"])}

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    async def hear(self, speaker: str, text: str, spoken_at: float | None = None) -> None:
        """A statement is complete (see segmenter.py for when that is): show it, then check it."""
        text = " ".join(text.split())
        if not text:
            return
        line = {"id": self._id("u"), "speaker": speaker, "text": text, "t": time.time()}
        context = [(old["speaker"], old["text"]) for old in self.lines[-CONTEXT_LINES:]]
        self.lines.append(line)
        await self.emit({"type": "line", **line})
        self.spawn(self._check(line, context, spoken_at))
        self._since_naming += 1
        if self._since_naming >= NAMING_EVERY and self._anonymous():
            self._since_naming = 0
            self.spawn(self._name_speakers())

    def _anonymous(self) -> set[str]:
        return {l["speaker"] for l in self.lines if ANONYMOUS.match(l["speaker"])} - set(self.names) - self.suggested

    async def _name_speakers(self) -> None:
        """Diarization tells voices apart but can't say whose they are. People name each other all the
        time ("Thanks, Yann"), so suggest names from those cues and let a human confirm."""
        try:
            guesses = await self.backend.name_speakers([(l["speaker"], l["text"]) for l in self.lines[-30:]])
        except Exception as err:
            log.warning("naming speakers failed: %s", err)
            return
        open_labels = self._anonymous()
        for guess in guesses:
            if guess["speaker"] in open_labels:
                self.suggested.add(guess["speaker"])
                await self.emit({"type": "speaker_guess", **guess})

    async def _check(self, line: dict, context: list[tuple[str, str]], spoken_at: float | None) -> None:
        start = time.perf_counter()
        ids: dict[int, str] = {}

        async def on_early(i: int, fields: dict) -> None:
            if i >= MAX_CLAIMS_PER_LINE or i in ids:
                return
            claim = {
                "id": self._id("c"),
                "line_id": line["id"],
                "speaker": line["speaker"],
                "quote": fields.get("quote", ""),
                "claim": fields.get("claim", ""),
                "kind": fields.get("kind", "fact"),
            }
            ids[i] = claim["id"]
            self.claims[claim["id"]] = claim
            await self.emit({"type": "claim", **claim, "ms": ms_since(start)})

        async def on_done(i: int, data: dict) -> None:
            if i >= MAX_CLAIMS_PER_LINE:
                return
            if i not in ids:
                await on_early(i, data)
            claim = self.claims[ids[i]]
            claim.update(
                quote=data.get("quote", claim["quote"]),
                claim=data.get("claim", claim["claim"]),
                kind=data.get("kind", claim["kind"]),
                query=data.get("query", ""),
                voices=data.get("voices", []),
                crux=data.get("explanation", ""),
            )
            timings = {"search_ms": data.get("search_ms", 0), "llm_ms": data.get("llm_ms", 0)}
            if claim["kind"] != "fact":
                await self.emit({"type": "debate", "claim_id": claim["id"], "kind": claim["kind"], "crux": claim["crux"]})
                self.spawn(self._perspectives(claim, start))
                return
            verdict = {k: data.get(k, "") for k in ("verdict", "confidence", "explanation", "correction")}
            verdict["sources"] = data.get("sources", [])
            unsourced = verdict["verdict"] != "unverifiable" and not verdict["sources"]
            known = self.settled.get(normalize(claim["claim"]))
            if known:
                await self._settle(claim, {**known, "repeat": True}, start, spoken_at)
            elif data.get("needs_search") or unsourced:
                # Show the first read right away, marked as unconfirmed, while a targeted search runs.
                await self.emit({
                    "type": "verdict", "claim_id": claim["id"], "provisional": True, **verdict,
                    "timings": {**timings, "total_ms": ms_since(start)},
                })
                self.spawn(self._escalate(claim, verdict, start, spoken_at))
            else:
                await self._settle(claim, {**verdict, "method": self.backend.evidence, "timings": timings}, start, spoken_at)

        named = [(self.who(speaker), text) for speaker, text in context]
        try:
            async with self._limit:
                await self.backend.check(line["text"], self.who(line["speaker"]), named, list(self.voices), on_early, on_done)
        except NotRecorded:
            await self.emit({
                "type": "notice",
                "message": "The offline test data only covers the demo scripts. Add API keys to check anything else.",
            })
        except Exception as err:
            log.exception("check failed")
            await self.emit({"type": "error", "message": f"Couldn't check that line. {describe(err)}"})

    async def _settle(self, claim: dict, verdict: dict, start: float, spoken_at: float | None) -> None:
        verdict = with_receipts(verdict)
        timings = {**verdict.get("timings", {}), "total_ms": ms_since(start)}
        if spoken_at:
            timings["since_speech_ms"] = max(0, int((time.time() - spoken_at) * 1000))
        verdict = {**verdict, "timings": timings}
        claim["verdict"] = verdict
        self.settled.setdefault(normalize(claim["claim"]), verdict)
        await self.emit({"type": "verdict", "claim_id": claim["id"], "provisional": False, **verdict})
        log.info("%-12s %5d ms  %s", verdict["verdict"].upper(), timings["total_ms"], claim["claim"])

    async def _escalate(self, claim: dict, first_read: dict, start: float, spoken_at: float | None) -> None:
        try:
            async with self._limit:
                verdict = await self.backend.escalate(self._named(claim))
        except Exception as err:
            log.warning("escalation failed for %r: %s", claim["claim"], err)
            verdict = {**first_read, "method": "model-only"}
        await self._settle(claim, verdict, start, spoken_at)

    async def _perspectives(self, claim: dict, start: float) -> None:
        try:
            async with self._limit:
                result = await self.backend.perspectives(self._named(claim), list(self.voices))
        except NotRecorded:
            result = {"perspectives": []}
        except Exception as err:
            log.warning("perspectives failed for %r: %s", claim["claim"], err)
            result = {"perspectives": [], "note": describe(err)}
        claim["perspectives"] = result["perspectives"]
        await self.emit({
            "type": "perspectives",
            "claim_id": claim["id"],
            "perspectives": result["perspectives"],
            "note": result.get("note", ""),
            "timings": {**result.get("timings", {}), "total_ms": ms_since(start)},
        })
        log.info("PERSPECTIVES %5d ms  %d takes on %s", ms_since(start), len(result["perspectives"]), claim["claim"])

    async def pushback(self, claim_id: str, argument: str) -> None:
        """Someone disputes a verdict: re-check with a deeper web search that takes their argument into account."""
        claim = self.claims.get(claim_id)
        if not claim or not claim.get("verdict"):
            await self.emit({"type": "error", "message": "That claim has no verdict to push back on yet."})
            return
        before = claim["verdict"]
        await self.emit({"type": "pushback", "claim_id": claim_id, "argument": argument})
        start = time.perf_counter()
        try:
            async with self._limit:
                verdict = await self.backend.pushback(self._named(claim), before, argument)
        except NotRecorded:
            await self.emit({"type": "pushback_failed", "claim_id": claim_id, "message": "Offline replay has no recorded re-check for this claim."})
            return
        except Exception as err:
            log.exception("pushback failed")
            await self.emit({"type": "pushback_failed", "claim_id": claim_id, "message": describe(err)})
            return
        note = verdict.pop("note", "")
        verdict = with_receipts(verdict)
        verdict["timings"] = {**verdict.get("timings", {}), "total_ms": ms_since(start)}
        verdict["pushback"] = {
            "argument": argument,
            "note": note,
            "previous": before["verdict"],
            "changed": verdict["verdict"] != before["verdict"],
        }
        claim["verdict"] = verdict
        self.settled[normalize(claim["claim"])] = verdict
        await self.emit({"type": "verdict", "claim_id": claim_id, "provisional": False, **verdict})
