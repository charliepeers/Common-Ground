"""Every Claude call the app makes. Which model runs which role is set in config.py."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date
from typing import Awaitable, Callable
from urllib.parse import urlparse

import anthropic

from . import config, prompts, schemas
from .jsonstream import ClaimStream
from .search import Source, domain
from .util import ms_since, squash

log = logging.getLogger("cg.llm")

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # a refused request reruns on another model, server-side
FAST_BETA = "fast-mode-2026-02-01"  # same model, faster output, premium price
WEB_SEARCH_TOOL = "web_search_20260209"
# Models documented to accept fallbacks="default" on the Claude API, and the ones fast mode exists for.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FAST_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"}

OnClaim = Callable[[int, dict], Awaitable[None]]


def today() -> str:
    return date.today().isoformat()


def final_text(message) -> str:
    """The answer's text. After a mid-answer fallback, only what the fallback model wrote."""
    blocks = list(message.content)
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i].type == "fallback":
            blocks = blocks[i + 1:]
            break
    return "".join(b.text for b in blocks if b.type == "text")


def declined(explanation: str = "The model declined to assess this claim.") -> dict:
    return {"verdict": "unverifiable", "confidence": "low", "explanation": explanation, "correction": "", "sources": []}


def cited(sources: list[Source]) -> list[dict]:
    """The references a verdict shows, in Claude's order: one per site, at most MAX_SOURCES."""
    by_site: dict[str, Source] = {}
    for source in sources:
        by_site.setdefault(domain(source.url), source)
    return [s.as_dict() for s in list(by_site.values())[: config.MAX_SOURCES]]


def with_sources(data: dict, sources: list[Source]) -> dict:
    """Swaps Claude's 1-based source_ids for the source records they point at."""
    out = {k: v for k, v in data.items() if k != "source_ids"}
    picked: list[Source] = []
    for i in data.get("source_ids", []):
        if 1 <= i <= len(sources) and sources[i - 1] not in picked:
            picked.append(sources[i - 1])
    out["sources"] = cited(picked)
    return out


def _url_key(url: str) -> str:
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    return host + parts.path.rstrip("/")


def _verbatim(quote: str, text: str) -> bool:
    quote = quote.strip(" \"'“”…")
    return bool(quote) and squash(quote).lower() in squash(text).lower()


_SELF_INTRO = re.compile(r"\b(i'm|i’m|i am|my name is|my name's|this is|call me)\b", re.I)


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'-]+", text.lower())


def _named_in(name: str, evidence: str) -> bool:
    """Every word of the name was actually said: "Thanks, Yann" can't become "Yann LeCun"."""
    said = set(_words(evidence))
    return bool(_words(name)) and all(word in said for word in _words(name))


def _cue_target(lines: list[tuple[str, str]], evidence: str, points_to: str) -> str | None:
    """The speaker a naming cue refers to, by turn order: whoever said it, or the nearest other
    speaker before or after that line."""
    for i in range(len(lines) - 1, -1, -1):  # the most recent line containing the cue
        who, text = lines[i]
        if not _verbatim(evidence, text):
            continue
        if points_to == "current":
            return who
        step = -1 if points_to == "previous" else 1
        j = i + step
        while 0 <= j < len(lines):
            if lines[j][0] != who:
                return lines[j][0]
            j += step
        return None
    return None


class LLM:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        fast: bool = config.FAST_MODE,
        live_model: str = config.LIVE_MODEL,
        pushback_model: str = config.PUSHBACK_MODEL,
    ):
        self.client = client or anthropic.AsyncAnthropic(timeout=60.0)
        # In fast mode a 429 should drop straight to standard speed, not sit through SDK retries.
        self._fast_client = self.client.with_options(max_retries=0)
        self.fast = fast
        self.live_model = live_model
        self.pushback_model = pushback_model

    def uses_fast(self, model: str) -> bool:
        return self.fast and model in FAST_MODELS

    def _params(self, model: str, fast: bool) -> dict:
        params: dict = {"model": model}
        betas = []
        if model in FALLBACK_MODELS:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        if fast:
            betas.append(FAST_BETA)
            params["speed"] = "fast"
        if betas:
            params["betas"] = betas
        return params

    async def _run(self, model: str, call):
        """Runs call(client, params) in fast mode when it's on, and at standard speed when fast mode is refused."""
        if not self.uses_fast(model):
            return await call(self.client, self._params(model, False))
        try:
            return await call(self._fast_client, self._params(model, True))
        except anthropic.RateLimitError:
            log.warning("fast mode rate limited; this request runs at standard speed")
            return await call(self.client, self._params(model, False))
        except (anthropic.BadRequestError, anthropic.PermissionDeniedError) as err:
            result = await call(self.client, self._params(model, False))
            # Standard speed worked where fast didn't, so this key can't use fast mode.
            log.warning("fast mode refused (%s); using standard speed from now on", err)
            self.fast = False
            return result

    async def create(self, model: str, **kwargs):
        return await self._run(model, lambda client, params: client.beta.messages.create(**params, **kwargs))

    async def stream(self, model: str, on_text: Callable[[str], Awaitable[None]], on_restart: Callable[[], None], **kwargs):
        async def call(client, params):
            async with client.beta.messages.stream(**params, **kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "fallback":
                        on_restart()  # another model takes over mid-answer, and its JSON starts fresh
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        await on_text(event.delta.text)
                return await stream.get_final_message()

        return await self._run(model, call)

    async def check(
        self,
        line: str,
        speaker: str,
        context: list[tuple[str, str]],
        sources: list[Source],
        searched: bool,
        on_early: OnClaim,
        on_done: OnClaim,
    ) -> dict:
        """Finds and judges the claims in one new line in a single streamed call.

        on_early(i, {quote, claim, kind}) fires as soon as a claim is identifiable; on_done(i, claim)
        fires when its verdict is complete. Returns timings in ms.
        """
        reader = ClaimStream()
        start = time.perf_counter()
        timing: dict = {}

        async def on_text(delta: str) -> None:
            for event, i, data in reader.feed(delta):
                if event == "early":
                    timing.setdefault("llm_first_ms", ms_since(start))
                    await on_early(i, data)
                else:
                    await on_done(i, {**with_sources(data, sources), "llm_ms": ms_since(start)})

        message = await self.stream(
            self.live_model,
            on_text,
            reader.reset,
            max_tokens=8000,
            system=[{"type": "text", "text": prompts.check_system(today()), "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": config.CHECK_EFFORT, "format": {"type": "json_schema", "schema": schemas.CHECK_SCHEMA}},
            messages=[{"role": "user", "content": prompts.check_user(line, speaker, context, sources, searched)}],
        )
        timing["llm_ms"] = ms_since(start)
        if message.stop_reason in ("refusal", "max_tokens"):
            log.warning("check stopped (%s) on: %s", message.stop_reason, line)
            return timing
        claims = json.loads(final_text(message)).get("claims", [])
        for i in range(reader.completed, len(claims)):  # anything the stream reader didn't hand over
            if i not in reader.announced:
                await on_early(i, {k: claims[i].get(k, "") for k in ("quote", "claim", "kind")})
            await on_done(i, {**with_sources(claims[i], sources), "llm_ms": timing["llm_ms"]})
        return timing

    async def verify(self, claim: str, quote: str, speaker: str, sources: list[Source]) -> dict:
        """Verdict on one claim from targeted search results."""
        message = await self.create(
            self.live_model,
            max_tokens=8000,
            system=prompts.verify_system(today()),
            output_config={"effort": config.CHECK_EFFORT, "format": {"type": "json_schema", "schema": schemas.VERIFY_SCHEMA}},
            messages=[{"role": "user", "content": prompts.verify_user(claim, quote, speaker, sources)}],
        )
        if message.stop_reason == "refusal":
            return declined()
        return with_sources(json.loads(final_text(message)), sources)

    async def deep_check(
        self,
        claim: str,
        quote: str,
        speaker: str,
        effort: str = "low",
        max_searches: int = 3,
        pushback: dict | None = None,
        model: str | None = None,
    ) -> dict:
        """Claude searches the web itself, then calls submit_verdict. Used for push-back, and when there's no Exa key.

        Structured output can't be combined with web search (its answers carry citations), so the
        verdict comes back through a strict client tool instead.
        """
        tools = [
            {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_searches},
            schemas.submit_verdict_tool(with_note=pushback is not None),
        ]
        messages: list[dict] = [{"role": "user", "content": prompts.deep_user(claim, quote, speaker, pushback)}]
        seen: dict[str, Source] = {}
        for _ in range(6):
            message = await self.create(
                model or self.live_model,
                max_tokens=16000,
                system=prompts.deep_system(today()),
                tools=tools,
                output_config={"effort": effort},
                messages=messages,
            )
            for block in message.content:
                if block.type == "web_search_tool_result" and isinstance(block.content, list):
                    for result in block.content:
                        seen.setdefault(_url_key(result.url), Source(result.title, result.url, result.page_age or ""))
            if message.stop_reason == "refusal":
                return declined()
            call = next((b for b in message.content if b.type == "tool_use" and b.name == "submit_verdict"), None)
            if call:
                data = dict(call.input)
                found = [seen.get(_url_key(url)) for url in data.pop("source_urls", [])]
                data["sources"] = cited([source for source in found if source])  # only pages the search returned count
                return data
            messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason != "pause_turn":  # a paused turn resumes when re-sent as is
                messages.append({"role": "user", "content": "Call submit_verdict now with your verdict."})
        return declined("The deep check ran out of steps without a verdict.")

    async def perspectives(
        self, claim: str, speaker: str, crux: str, voices: list[str], sources: list[Source]
    ) -> list[dict]:
        """Other people's takes on a contested claim, each with a quote verified against its page."""
        if not sources:
            return []
        message = await self.create(
            self.live_model,
            max_tokens=8000,
            system=prompts.PERSPECTIVES_SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schemas.PERSPECTIVES_SCHEMA}},
            messages=[{"role": "user", "content": prompts.perspectives_user(claim, speaker, crux, voices, sources)}],
        )
        if message.stop_reason == "refusal":
            return []
        found = []
        for take in json.loads(final_text(message)).get("perspectives", []):
            i = take.get("source_id", 0)
            if not 1 <= i <= len(sources):
                continue
            source = sources[i - 1]
            if not _verbatim(take.get("quote", ""), source.snippet):
                log.info("dropped a quote attributed to %s: not verbatim in %s", take.get("person"), source.url)
                continue
            found.append({
                "person": take["person"],
                "stance": take["stance"],
                "summary": take["summary"],
                "quote": take["quote"].strip(" \"'“”"),
                "source": source.as_dict(),
            })
        return found[: config.MAX_TAKES]

    async def name_speakers(self, lines: list[tuple[str, str]]) -> list[dict]:
        """Names for anonymous speaker labels, only from explicit cues, each with evidence checked verbatim."""
        message = await self.create(
            self.live_model,
            max_tokens=4000,
            system=prompts.NAME_SPEAKERS_SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schemas.SPEAKER_NAMES_SCHEMA}},
            messages=[{"role": "user", "content": prompts.name_speakers_user(lines)}],
        )
        if message.stop_reason == "refusal":
            return []
        # Checks that research shows matter: the cue must be verbatim, the name must not be expanded
        # beyond what was said, and the cue must point at that label by the order of turns.
        found = []
        for guess in json.loads(final_text(message)).get("guesses", []):
            evidence, points_to = guess.get("evidence", ""), guess.get("points_to", "")
            if points_to == "current" and not _SELF_INTRO.search(evidence):
                continue  # only a self-introduction names the person speaking; "Thanks, Yann" doesn't
            if (
                _named_in(guess.get("name", ""), evidence)
                and _cue_target(lines, evidence, points_to) == guess.get("speaker")
            ):
                found.append({k: guess[k] for k in ("speaker", "name", "evidence")})
        return found

    async def warm_up(self) -> None:
        """First use of a JSON schema compiles it (slow), and the first request opens connections.
        Pay both before anyone is watching."""

        async def ignore(i: int, data: dict) -> None:
            pass

        start = time.perf_counter()
        results = await asyncio.gather(
            self.check("Water boils at 100 degrees Celsius at sea level.", "Warm-up", [], [], [], False, ignore, ignore),
            self.verify("Water is wet.", "Water is wet.", "Warm-up", []),
            self.perspectives("Warm-up.", "Warm-up", "", [], [Source("Warm-up", "https://example.com", "", "warm-up")]),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        for err in errors:
            log.warning("warm-up call failed: %r", err)
        log.info("warm-up done in %d ms (live model %s, fast mode %s)", ms_since(start), self.live_model,
                 "on" if self.uses_fast(self.live_model) else "off")
