"""Where answers come from: Exa search and Claude."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from functools import partial

from . import config
from .llm import LLM
from .search import ExaSearch, Source
from .util import ms_since

log = logging.getLogger("cg.backend")

X_DOMAINS = ["x.com", "twitter.com"]
MAX_PERSPECTIVE_SOURCES = 16
FACT_RESULTS = 10  # Exa's default, and the most lite returns at its base price
# Exa's `objective`: what a search is for, so the right kind of page ranks first.
FACT_OBJECTIVE = (
    "Evidence to verify or refute a statement made in a live conversation. Rank first: primary and "
    "authoritative sources, such as official and government pages, original data and statistics, "
    "peer-reviewed research, reference works and established news organizations. Rank last: content "
    "farms, SEO pages, forums and unsourced posts."
)
VOICES_OBJECTIVE = (
    "What credible, named people have said about this question in their own words: posts on X, essays, "
    "interviews and talks by researchers, practitioners and journalists. Prefer the person's own words "
    "over coverage of them."
)
REFERENCE = re.compile(r"\b(he|she|they|it|his|her|their|them|this|that|these|those)\b", re.I)


def search_query(line: str, context: list[tuple[str, str]]) -> str:
    """The line as spoken. Short or pronoun-heavy lines borrow the previous line for context."""
    words = len(line.split())
    if context and (words < 6 or (words < 14 and REFERENCE.search(line))):
        return f"{context[-1][1]} {line}"
    return line


class LiveBackend:
    def __init__(self, llm: LLM, search: ExaSearch | None):
        self.llm = llm
        self.search = search

    @property
    def evidence(self) -> str:
        return "exa" if self.search else "claude-web-search"

    async def check(self, line, speaker, context, on_early, on_done) -> dict:
        """Searches the raw line first (no waiting on Claude to write a query), then one streamed Claude call."""
        timing: dict = {}
        sources: list[Source] = []
        if self.search:
            start = time.perf_counter()
            try:
                sources = await self.search.search(search_query(line, context), FACT_RESULTS, objective=FACT_OBJECTIVE)
            except Exception as err:
                log.warning("search failed, checking without results: %s", err)
            timing["search_ms"] = ms_since(start)

        async def done(i: int, data: dict) -> None:
            await on_done(i, {**data, "search_ms": timing.get("search_ms", 0)})

        timing.update(
            await self.llm.check(line, speaker, context, sources, self.search is not None, on_early, done)
        )
        return timing

    async def escalate(self, claim: dict) -> dict:
        """Second pass for a claim the first search didn't cover: a targeted search, then a verdict."""
        start = time.perf_counter()
        if not self.search:
            result = await self.llm.deep_check(claim["claim"], claim["quote"], claim["speaker"])
            return {**result, "method": "claude-web-search", "timings": {"llm_ms": ms_since(start)}}
        sources = await self.search.search(claim.get("query") or claim["claim"], FACT_RESULTS, objective=FACT_OBJECTIVE)
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

    async def perspectives(self, claim: dict) -> dict:
        """Searches for the most credible voices on the question (Claude names them when it sorts the claim)
        and for the conversation on X, then picks takes."""
        if not self.search:
            return {"perspectives": [], "timings": {}, "note": "Perspectives need EXA_API_KEY."}
        start = time.perf_counter()
        topic = claim.get("query") or claim["claim"]
        names = list(dict.fromkeys(claim.get("voices", [])))[:4]
        voices_search = partial(self.search.search, highlight_chars=1200, objective=VOICES_OBJECTIVE)
        jobs = [voices_search(f"{name} on {topic}", 3) for name in names]
        jobs += [voices_search(f"{name}: {topic}", 3, X_DOMAINS) for name in names[:2]]
        jobs.append(voices_search(topic, 5, X_DOMAINS))  # what people on X say, beyond the named voices
        jobs.append(voices_search(f"the case against: {claim['claim']}", 5))
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
