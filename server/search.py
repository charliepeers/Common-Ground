"""Web search for evidence and for other people's takes. Exa is the provider."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger("cg.search")


@dataclass
class Source:
    title: str
    url: str
    published: str = ""
    snippet: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class ExaSearch:
    """POST https://api.exa.ai/search.

    The default type, "lite", is Exa's search for real-time and voice experiences. Exa's changelog
    (2026-10-19) made "fast" and "instant" legacy in its favor; lite answers from Exa's index and never
    fetches a page live. If an account rejects it, we fall back to "auto" once and stay there.

    Exa recommends sending only the query and bare highlights. The other fields here are deliberate:
    numResults and highlight length bound how much text goes into each per-sentence Claude prompt
    (prompt size is latency), and includeDomains is only used for the X search the perspectives lane
    asks for. All of them are on lite's accepted list; lite rejects anything else with a 400.
    """

    URL = "https://api.exa.ai/search"

    def __init__(self, api_key: str, search_type: str = "lite", http: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.search_type = search_type
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0))

    async def search(
        self,
        query: str,
        num_results: int = 5,
        include_domains: list[str] | None = None,
        highlight_chars: int = 700,
    ) -> list[Source]:
        body: dict = {
            "query": query[:400],
            "type": self.search_type,
            "numResults": num_results,
            "contents": {"highlights": {"maxCharacters": highlight_chars}},
        }
        if include_domains:
            body["includeDomains"] = include_domains
        r = await self.http.post(self.URL, json=body, headers={"x-api-key": self.api_key})
        if r.status_code == 400 and self.search_type != "auto":
            log.warning("Exa rejected type=%r (%s); using type='auto' from now on", self.search_type, r.text[:200])
            self.search_type = "auto"
            return await self.search(query, num_results, include_domains, highlight_chars)
        r.raise_for_status()
        data = r.json()
        for warning in data.get("warnings") or []:  # request fields Exa ignored, and what to send instead
            log.warning("Exa: %s", warning)
        return [_source(item, highlight_chars) for item in data.get("results", []) if item.get("url")]


def _source(item: dict, max_chars: int) -> Source:
    highlights = [h.strip() for h in item.get("highlights") or [] if h and h.strip()]
    snippet = " … ".join(highlights) or (item.get("text") or "").strip()
    return Source(
        title=(item.get("title") or domain(item["url"])).strip(),
        url=item["url"],
        published=(item.get("publishedDate") or "")[:10],
        snippet=snippet[:max_chars],
    )
