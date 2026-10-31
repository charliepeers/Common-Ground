import asyncio

from server.backends import NotRecorded, search_query
from server.session import Session, with_receipts

SRC = {"title": "t", "url": "https://u.example", "published": "", "snippet": "s"}


def fact(quote, verdict="false", sources=(SRC,), needs_search=False):
    return {"quote": quote, "claim": quote + ".", "kind": "fact", "verdict": verdict, "confidence": "high",
            "explanation": "e", "correction": "c", "sources": list(sources), "needs_search": needs_search,
            "query": "q" if needs_search else "", "voices": []}


class FakeBackend:
    evidence = "fake"

    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    async def check(self, line, speaker, context, voices, on_early, on_done):
        self.calls.append(("check", line, context, voices))
        if line not in self.lines:
            raise NotRecorded(line)
        for i, claim in enumerate(self.lines[line]):
            await on_early(i, {k: claim[k] for k in ("quote", "claim", "kind")})
            await on_done(i, {**claim, "search_ms": 5, "llm_ms": 9})
        return {}

    async def escalate(self, claim):
        self.calls.append(("escalate", claim["claim"]))
        return {"verdict": "true", "confidence": "high", "explanation": "found it", "correction": "", "sources": [SRC], "method": "fake"}

    async def perspectives(self, claim, voices):
        self.calls.append(("perspectives", claim["claim"], tuple(voices)))
        return {"perspectives": [{"person": "P", "stance": "disagrees", "summary": "s", "quote": "q", "source": SRC}], "timings": {}}

    async def pushback(self, claim, verdict, argument):
        return {"verdict": "misleading", "confidence": "medium", "explanation": "x", "correction": "y", "sources": [SRC], "note": "fair point"}


async def run(lines, says, voices=()):
    events = []

    async def emit(event):
        events.append(event)

    backend = FakeBackend(lines)
    session = Session(emit, backend)
    session.set_voices(list(voices))
    for speaker, text in says:
        await session.hear(speaker, text)
        await asyncio.sleep(0.01)
    while session._tasks:
        await asyncio.sleep(0.01)
    return session, backend, events


def kinds(events):
    return [e["type"] for e in events]


async def test_a_sourced_fact_goes_line_claim_verdict():
    _, _, events = await run({"Biden is 47th": [fact("Biden is 47th")]}, [("Alex", "Biden is 47th")])
    assert kinds(events) == ["line", "claim", "verdict"]
    verdict = events[2]
    assert verdict["verdict"] == "false" and verdict["provisional"] is False
    assert verdict["timings"]["search_ms"] == 5 and "total_ms" in verdict["timings"]


async def test_unsourced_or_unsure_claims_show_a_first_read_then_escalate():
    lines = {"x": [fact("a", sources=()), fact("b", needs_search=True)]}
    _, backend, events = await run(lines, [("A", "x")])
    verdicts = [e for e in events if e["type"] == "verdict"]
    assert [v["provisional"] for v in verdicts] == [True, True, False, False]
    assert all(v["verdict"] == "true" for v in verdicts[2:])
    assert [c for c in backend.calls if c[0] == "escalate"] == [("escalate", "a."), ("escalate", "b.")]


async def test_contested_claims_get_perspectives_not_verdicts():
    contested = {**fact("LLMs can't reason", verdict="unverifiable", sources=()), "kind": "contested",
                 "explanation": "crux", "query": "llm reasoning", "voices": ["Gary Marcus"]}
    _, backend, events = await run({"talk": [contested]}, [("Yann", "talk")], voices=["Andrej Karpathy"])
    assert kinds(events) == ["line", "claim", "debate", "perspectives"]
    assert events[2]["crux"] == "crux"
    assert backend.calls[0][3] == ["Andrej Karpathy"]
    assert backend.calls[1] == ("perspectives", "LLMs can't reason.", ("Andrej Karpathy",))


async def test_repeated_claims_reuse_the_settled_verdict():
    lines = {"one": [fact("Biden is 47th")], "two": [fact("Biden is 47th", needs_search=True)]}
    _, backend, events = await run(lines, [("A", "one"), ("B", "two")])
    verdicts = [e for e in events if e["type"] == "verdict"]
    assert verdicts[1].get("repeat") is True and not any(c[0] == "escalate" for c in backend.calls)


async def test_context_is_the_previous_lines():
    _, backend, _ = await run({"a": [], "b": []}, [("A", "a"), ("B", "b")])
    assert backend.calls[1][2] == [("A", "a")]


async def test_unrecorded_lines_produce_a_notice():
    _, _, events = await run({}, [("A", "hello")])
    assert kinds(events) == ["line", "notice"]


async def test_pushback_records_what_changed():
    session, _, events = await run({"x": [fact("a")]}, [("A", "x")])
    claim_id = events[1]["id"]
    await session.pushback(claim_id, "context matters")
    final = events[-1]
    assert final["verdict"] == "misleading"
    assert final["pushback"] == {"argument": "context matters", "note": "fair point", "previous": "false", "changed": True}


def test_no_receipts_no_red():
    assert with_receipts({"verdict": "false", "sources": []}) == {"verdict": "unverifiable", "sources": [], "leaning": "false"}
    assert with_receipts({"verdict": "false", "sources": [SRC]})["verdict"] == "false"
    assert with_receipts({"verdict": "unverifiable", "sources": []})["verdict"] == "unverifiable"


def test_search_query_borrows_context_for_short_or_referential_lines():
    assert search_query("He was the 45th too.", [("A", "Trump is the 47th president.")]) == "Trump is the 47th president. He was the 45th too."
    long_line = "The first crewed Moon landing happened in July 1969 during the Apollo 11 mission"
    assert search_query(long_line, [("A", "x")]) == long_line


async def test_renamed_speakers_reach_the_model():
    events = []

    async def emit(event):
        events.append(event)

    backend = FakeBackend({"a": [], "b": [{**fact("x", verdict="unverifiable", sources=()), "kind": "contested"}]})
    session = Session(emit, backend)
    await session.hear("Speaker 1", "a")
    session.rename("Speaker 1", "Yann LeCun")
    await session.hear("Speaker 1", "b")
    while session._tasks:
        await asyncio.sleep(0.01)
    check_b = [c for c in backend.calls if c[0] == "check" and c[1] == "b"][0]
    assert check_b[2] == [("Yann LeCun", "a")]
    assert events[0]["speaker"] == "Speaker 1"  # the page keeps the raw id and shows its own label


def test_bench_scoring_counts_false_alarms():
    from bench.run_bench import score
    true_item = {"kind": ["fact"], "verdicts": ["true"]}
    assert score(true_item, "fact", "false") == {"kind_ok": True, "verdict_ok": False, "false_alarm": True, "missed_false": False}
    assert score(true_item, "fact", "unverifiable")["false_alarm"] is False
    assert score({"kind": ["value"], "verdicts": []}, "value", "")["verdict_ok"] is None
    assert score({"kind": ["fact"], "verdicts": ["false", "misleading"]}, "fact", "true")["missed_false"] is True


async def test_anonymous_speakers_get_name_suggestions_with_evidence():
    events = []

    async def emit(event):
        events.append(event)

    class Namer(FakeBackend):
        async def name_speakers(self, lines):
            self.calls.append(("name", len(lines)))
            return [{"speaker": "Speaker B", "name": "Yann", "evidence": "Thanks, Yann"},
                    {"speaker": "Speaker A", "name": "Lex", "evidence": "I'm Lex"}]

    backend = Namer({})
    session = Session(emit, backend)
    session.rename("Speaker A", "Lex Fridman")  # already named by hand: no suggestion
    for i, (who, text) in enumerate([("Speaker A", "Welcome."), ("Speaker A", "Thanks, Yann, for coming."),
                                     ("Speaker B", "Happy to be here."), ("Speaker B", "So, about LLMs.")]):
        await session.hear(who, text)
    while session._tasks:
        await asyncio.sleep(0.01)
    guesses = [e for e in events if e["type"] == "speaker_guess"]
    assert guesses == [{"type": "speaker_guess", "speaker": "Speaker B", "name": "Yann", "evidence": "Thanks, Yann"}]
    assert ("name", 4) in backend.calls


async def test_named_people_never_trigger_naming():
    backend = FakeBackend({})
    backend.name_speakers = None  # would raise if called

    async def emit(event):
        pass

    session = Session(emit, backend)
    for text in "abcdef":
        await session.hear("Alex", text)
    while session._tasks:
        await asyncio.sleep(0.01)
