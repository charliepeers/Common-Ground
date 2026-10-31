"""Runs the app on the fixed answers in tests/fixtures.json, for the browser test (tests/e2e.cjs).

    .venv/bin/python -m tests.serve 8765

The app itself only ever runs live. This swaps in a backend that answers from the test data, which
the page labels as such. A line the data doesn't cover gets an error, never a made-up answer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import uvicorn

from server import app as app_module
from server.util import normalize

FIXTURES = Path(__file__).resolve().parent / "fixtures.json"


class FixtureBackend:
    evidence = "test-data"

    def __init__(self, data: dict):
        self.lines = {normalize(k): v for k, v in data.get("lines", {}).items()}
        self.escalations = {normalize(k): v for k, v in data.get("escalations", {}).items()}
        self.pushbacks = {normalize(k): v for k, v in data.get("pushbacks", {}).items()}
        self.takes = {normalize(k): v for k, v in data.get("perspectives", {}).items()}

    @staticmethod
    def _lookup(table: dict, key: str) -> dict:
        found = table.get(normalize(key))
        if found is None:
            raise LookupError(f"not in the test data: {key}")
        return json.loads(json.dumps(found))  # a copy, so callers can't edit the test data

    async def check(self, line, speaker, context, voices, on_early, on_done) -> dict:
        for i, claim in enumerate(self._lookup(self.lines, line).get("claims", [])):
            await on_early(i, {k: claim[k] for k in ("quote", "claim", "kind")})
            await on_done(i, claim)
        return {}

    async def escalate(self, claim: dict) -> dict:
        return {**self._lookup(self.escalations, claim["claim"]), "method": self.evidence}

    async def pushback(self, claim: dict, verdict: dict, argument: str) -> dict:
        return {**self._lookup(self.pushbacks, claim["claim"]), "method": self.evidence}

    async def perspectives(self, claim: dict, voices: list[str]) -> dict:
        return self._lookup(self.takes, claim["claim"])

    async def name_speakers(self, lines: list[tuple[str, str]]) -> list[dict]:
        return []


if __name__ == "__main__":
    app_module.BACKEND = FixtureBackend(json.loads(FIXTURES.read_text()))
    uvicorn.run(app_module.app, port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
