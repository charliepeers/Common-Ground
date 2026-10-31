"""Where answers come from: Exa search and Claude."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import config
from .llm import LLM
from .search import ExaSearch, Source
from .util import ms_since

log = logging.getLogger("cg.backend")

X_DOMAINS = ["x.com", "twitter.com"]
MAX_PERSPECTIVE_SOURCES = 12
_REFERENCE = re.compile(r"\b(he|she|they|it|his|her|their|them|this|that|these|those)\b", re.I)


def search_query(line: str, context: list[tuple[str, str]]) -> str:
    """The line as spoken. Short or pronoun-heavy lines borrow the previous line for context."""
    words = len(line.split())
    if context and (words < 6 or (words < 14 and _REFERENCE.search(line))):
        return f"{context[-1][1]} {line}"
    return line


class LiveBackend:
    def __init__(self, llm: LLM, search: ExaSearch | None):
        self.llm = llm
        self.search = search

    @property
    def evidence(self) -> str:
        return "exa" if self.search else "claude-web-search"

    async def check(self, line, speaker, context, voices, on_early, on_done) -> dict:
        """Searches the raw line first (no waiting on Claude to write a query), then one streamed Claude call."""
        timing: dict = {}
        sources: list[Source] = []
        if self.search:
            start = time.perf_counter()
            try:
                sources = await self.search.search(search_query(line, context))
            except Exception as err:
                log.warning("search failed, checking without results: %s", err)
            timing["search_ms"] = ms_since(start)

        async def done(i: int, data: dict) -> None:
            await on_done(i, {**data, "search_ms": timing.get("search_ms", 0)})

        timing.update(
            await self.llm.check(line, speaker, context, sources, voices, self.search is not None, on_early, done)
        )
        return timing

    async def escalate(self, claim: dict) -> dict:
        """Second pass for a claim the first search didn't cover: a targeted search, then a verdict."""
        start = time.perf_counter()
        if not self.search:
            result = await self.llm.deep_check(claim["claim"], claim["quote"], claim["speaker"])
            return {**result, "method": "claude-web-search", "timings": {"llm_ms": ms_since(start)}}
        sources = await self.search.search(claim.get("query") or claim["claim"])
        search_ms = ms_since(start)
        result = await self.llm.verify(claim["claim"], claim["quote"], claim["speaker"], sources)
        return {**result, "method": "exa", "timings": {"search_ms": search_ms, "llm_ms": ms_since(start) - search_ms}}

    async def pushback(self, claim: dict, verdict: dict, argument: str) -> dict:
        start = time.perf_counter()
        objection = {"verdict": verdict["verdict"], "explanation": verdict.get("explanation", ""), "argument": argument}
        result = await self.llm.deep_check(
            claim["claim"], claim["quote"], claim["speaker"], effort=config.PUSHBACK_EFFORT, max_searches=5,
            pushback=objection, model=self.llm.pushback_model,
        )
        return {**result, "method": "claude-web-search", "timings": {"llm_ms": ms_since(start)}}

    async def perspectives(self, claim: dict, voices: list[str]) -> dict:
        """Fans out searches for named voices (their own posts on X, plus the wider web), then picks takes."""
        if not self.search:
            return {"perspectives": [], "timings": {}, "note": "Perspectives need EXA_API_KEY."}
        start = time.perf_counter()
        topic = claim.get("query") or claim["claim"]
        names = list(dict.fromkeys(voices + claim.get("voices", [])))[:4]
        jobs = [self.search.search(f"{name} on {topic}", 3, highlight_chars=1200) for name in names]
        jobs += [self.search.search(f"{name}: {topic}", 3, X_DOMAINS, highlight_chars=1200) for name in names[:2]]
        jobs.append(self.search.search(f"the case against: {claim['claim']}", 4, highlight_chars=1200))
        sources: dict[str, Source] = {}
        for result in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(result, Exception):
                log.warning("perspective search failed: %s", result)
                continue
            for source in result:
                sources.setdefault(source.url, source)
        search_ms = ms_since(start)
        picked = list(sources.values())[:MAX_PERSPECTIVE_SOURCES]
        takes = await self.llm.perspectives(claim["claim"], claim["speaker"], claim.get("crux", ""), names, picked)
        return {"perspectives": takes, "timings": {"search_ms": search_ms, "llm_ms": ms_since(start) - search_ms}}

    async def name_speakers(self, lines: list[tuple[str, str]]) -> list[dict]:
        return await self.llm.name_speakers(lines)

    async def warm_up(self) -> None:
        jobs = [self.llm.warm_up()]
        if self.search:
            jobs.append(self.search.search("warm up", 1))
        await asyncio.gather(*jobs, return_exceptions=True)
