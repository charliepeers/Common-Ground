"""Benchmarks the live pipeline on labeled statements: does it sort and judge them right, and how fast?

    python -m bench.run_bench                      # every item once (needs ANTHROPIC_API_KEY, EXA_API_KEY)
    python -m bench.run_bench --repeat 3           # more latency samples
    python -m bench.run_bench --no-search          # Claude's own web search instead of Exa
    ->  bench/results/<timestamp>.csv and a summary

The number that matters most is false alarms: true statements flagged false or misleading in
front of an audience. Items run one at a time so the latencies aren't skewed by concurrency.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from server import config
from server.backends import LiveBackend
from server.llm import LLM
from server.search import ExaSearch
from server.session import with_receipts
from server.util import ms_since

HERE = Path(__file__).resolve().parent


def score(item: dict, kind: str, verdict: str) -> dict:
    fact = "fact" in item["kind"] and kind == "fact"
    return {
        "kind_ok": kind in item["kind"],
        "verdict_ok": verdict in item["verdicts"] if fact and item["verdicts"] else None,
        # the costly mistake: a true statement shown as false or misleading
        "false_alarm": "true" in item["verdicts"] and verdict in ("false", "misleading"),
        "missed_false": "false" in item["verdicts"] and verdict == "true",
    }


async def run_item(backend: LiveBackend, item: dict) -> dict:
    start = time.perf_counter()
    first, claims = None, []

    async def on_early(i: int, fields: dict) -> None:
        nonlocal first
        first = first or ms_since(start)

    async def on_done(i: int, data: dict) -> None:
        claims.append(data)

    timings = await backend.check(item["text"], "Speaker", [], on_early, on_done)
    kind, verdict, escalated = (claims[0]["kind"] if claims else "none"), "", False
    if claims and kind == "fact":
        result = claims[0]
        if result.get("needs_search") or with_receipts(result) is not result:
            result = await backend.escalate({**result, "speaker": "Speaker"})
            escalated = True
        verdict = with_receipts(result)["verdict"]
    return {
        "id": item["id"], "text": item["text"], "expected_kind": "|".join(item["kind"]), "kind": kind,
        "expected": "|".join(item["verdicts"]), "verdict": verdict, "escalated": escalated,
        "search_ms": timings.get("search_ms", ""), "first_claim_ms": first or "", "llm_ms": timings.get("llm_ms", ""),
        "total_ms": ms_since(start), **score(item, kind, verdict),
    }


def pct(values: list[float], q: float) -> str:
    if not values:
        return "-"
    values = sorted(values)
    return f"{values[min(len(values) - 1, round(q * (len(values) - 1)))]:.0f}"


def summarize(rows: list[dict]) -> None:
    kind_acc = sum(r["kind_ok"] for r in rows) / len(rows)
    judged = [r for r in rows if r["verdict_ok"] is not None]
    trues = [r for r in rows if "true" in r["expected"].split("|")]
    falses = [r for r in rows if "false" in r["expected"].split("|")]
    print(f"\nitems {len(rows)}   kind right {kind_acc:.0%}   verdict right {sum(r['verdict_ok'] for r in judged)}/{len(judged)}")
    print(f"false alarms {sum(r['false_alarm'] for r in rows)}/{len(trues)} true statements   "
          f"missed falsehoods {sum(r['missed_false'] for r in rows)}/{len(falses)}   escalated {sum(r['escalated'] for r in rows)}")
    print("\nlatency (ms)        p50     p90")
    for label, key in [("search", "search_ms"), ("first highlight", "first_claim_ms"), ("Claude call", "llm_ms"), ("to verdict", "total_ms")]:
        values = [float(r[key]) for r in rows if r[key] != ""]
        print(f"  {label:16} {pct(values, .5):>6}  {pct(values, .9):>6}")
    wrong = [r for r in rows if not r["kind_ok"] or r["verdict_ok"] is False]
    if wrong:
        print("\nmisses:")
        for r in wrong:
            print(f"  {r['id']}  expected {r['expected_kind']}/{r['expected'] or '-'}  got {r['kind']}/{r['verdict'] or '-'}  {r['text']}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-search", action="store_true", help="skip Exa; facts go to Claude's web search")
    args = ap.parse_args()
    items = [json.loads(line) for line in (HERE / "claims.jsonl").open()][: args.limit]
    search = None if args.no_search else ExaSearch(config.EXA_API_KEY, config.EXA_SEARCH_TYPE)
    backend = LiveBackend(LLM(), search)
    await backend.warm_up()  # schema compilation and connection setup shouldn't count against the first item
    rows = []
    for round_ in range(args.repeat):
        for item in items:
            row = await run_item(backend, item)
            rows.append({"round": round_, **row})
            mark = "ok " if row["kind_ok"] and row["verdict_ok"] is not False else "MISS"
            print(f"{mark} {row['total_ms']:>6} ms  {row['kind']:9} {row['verdict'] or '-':12} {item['text'][:60]}")
    out = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}{'-claude-search' if args.no_search else '-exa'}.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summarize(rows)
    print(f"\nmodel {config.LIVE_MODEL} (effort {config.CHECK_EFFORT}), fast mode "
          f"{'on' if backend.llm.uses_fast(config.LIVE_MODEL) else 'off'}; wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
