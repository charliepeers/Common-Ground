import asyncio

from server.backends import search_query
from server.session import Session, distinct_takes, with_receipts

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

    async def check(self, line, speaker, context, on_early, on_done):
        self.calls.append(("check", line, context))
        if line not in self.lines:
            raise LookupError(line)
        for i, claim in enumerate(self.lines[line]):
            await on_early(i, {k: claim[k] for k in ("quote", "claim", "kind")})
            await on_done(i, {**claim, "search_ms": 5, "llm_ms": 9})
        return {}

    async def escalate(self, claim):
        self.calls.append(("escalate", claim["claim"]))
        return {"verdict": "true", "confidence": "high", "explanation": "found it", "correction": "", "sources": [SRC], "method": "fake"}

    async def perspectives(self, claim):
        self.calls.append(("perspectives", claim["claim"], tuple(claim["voices"])))
        if claim["claim"].startswith("Unreachable"):
            raise ConnectionError("search is down")
        return {"perspectives": [{"person": "P", "stance": "disagrees", "summary": "s", "quote": "q", "source": SRC}], "timings": {}}

    async def pushback(self, claim, verdict, argument):
        return {"verdict": "misleading", "confidence": "medium", "explanation": "x", "correction": "y", "sources": [SRC], "note": "fair point"}


async def run(lines, says):
    events = []

    async def emit(event):
        events.append(event)

    backend = FakeBackend(lines)
    session = Session(emit, backend)
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
    lines = {"a b": [fact("a", sources=())], "c d": [fact("c", needs_search=True)]}
    _, backend, events = await run(lines, [("A", "a b"), ("A", "c d")])
    verdicts = [e for e in events if e["type"] == "verdict"]
    assert [v["provisional"] for v in verdicts] == [True, False, True, False]
    assert all(v["verdict"] == "true" for v in verdicts if not v["provisional"])
    assert [c for c in backend.calls if c[0] == "escalate"] == [("escalate", "a."), ("escalate", "c.")]


async def test_contested_claims_get_perspectives_not_verdicts():
    contested = {**fact("LLMs can't reason", verdict="unverifiable", sources=()), "kind": "contested",
                 "explanation": "crux", "query": "llm reasoning", "voices": ["Gary Marcus"]}
    _, backend, events = await run({"LLMs can't reason": [contested]}, [("Yann", "LLMs can't reason")])
    assert kinds(events) == ["line", "claim", "debate", "perspectives"]
    assert events[2]["crux"] == "crux"
    assert backend.calls[1] == ("perspectives", "LLMs can't reason.", ("Gary Marcus",))  # the voices Claude chose


async def test_a_value_gets_what_is_at_stake_and_no_voices():
    value = {**fact("we should tax it", verdict="unverifiable", sources=()), "kind": "value", "explanation": "at stake"}
    _, backend, events = await run({"we should tax it": [value]}, [("Sam", "we should tax it")])
    assert kinds(events) == ["line", "claim", "debate"] and events[2]["crux"] == "at stake"
    assert not any(c[0] == "perspectives" for c in backend.calls)


def test_a_name_shows_once_even_from_different_sources():
    take = lambda person, url: {"person": person, "stance": "disagrees", "summary": "s", "quote": "q", "source": {"url": url}}
    takes = [take("John Carreyrou", "https://npr.org/a"), take("john carreyrou", "https://gsb.stanford.edu/b"),
             take("Carreyrou", "https://wsj.com/c"), take("Tim Draper", "https://cnbc.com/d"), take("Holmes", "https://x.com/e")]
    assert [t["person"] for t in distinct_takes(takes)] == ["John Carreyrou", "Tim Draper"]  # and at most two


async def test_a_failed_perspectives_search_shows_an_empty_card_with_the_reason():
    contested = {**fact("Unreachable claim", verdict="unverifiable", sources=()), "kind": "contested", "voices": []}
    _, _, events = await run({"Unreachable claim": [contested]}, [("Yann", "Unreachable claim")])
    assert kinds(events) == ["line", "claim", "debate", "perspectives"]
    assert events[-1]["perspectives"] == [] and "search is down" in events[-1]["note"]


async def test_a_repeated_claim_gets_no_second_card():
    lines = {"Biden is 47th": [fact("Biden is 47th")], "well Biden is 47th": [fact("Biden is 47th", needs_search=True)]}
    _, backend, events = await run(lines, [("A", "Biden is 47th"), ("B", "well Biden is 47th")])
    assert kinds(events) == ["line", "claim", "verdict", "line"]
    assert not any(c[0] == "escalate" for c in backend.calls)


async def test_the_same_claim_in_different_words_gets_no_second_card():
    first, second = fact("Biden is 47th"), {**fact("he is the 47th"), "claim": "Biden is 47th."}
    _, _, events = await run({"Biden is 47th": [first], "he is the 47th": [second]}, [("A", "Biden is 47th"), ("A", "he is the 47th")])
    assert kinds(events) == ["line", "claim", "verdict", "line"]


async def test_a_quote_from_an_earlier_line_gets_no_card():
    lines = {"Kirk was killed by an extremist": [fact("Kirk was killed by an extremist")],
             "who had no plan": [{**fact("Kirk was killed by an extremist"), "claim": "Someone else's words."}]}
    _, _, events = await run(lines, [("A", "Kirk was killed by an extremist"), ("A", "who had no plan")])
    assert kinds(events) == ["line", "claim", "verdict", "line"]


async def test_a_line_gets_one_card_even_if_the_first_claim_is_dropped():
    lines = {"a b": [fact("not in this line"), fact("a"), fact("b")]}
    _, _, events = await run(lines, [("A", "a b")])
    assert kinds(events) == ["line", "claim", "verdict"] and events[1]["quote"] == "a"


async def test_a_sentence_cut_by_a_pause_is_checked_whole():
    events = []

    async def emit(event):
        events.append(event)

    whole = "Kirk was killed by an extremist who had no plan."
    session = Session(emit, FakeBackend({whole: []}), hold_seconds=0.05)
    first = {"speaker": "Speaker A", "text": "Kirk was killed by an", "spoken_at": 1.0}
    await session.transcribed([first], {"type": "caption", "speaker": "Speaker A", "text": ""})
    assert kinds(events) == ["caption"] and events[0]["text"] == "Kirk was killed by an"  # still on screen
    await session.transcribed([], {"type": "caption", "speaker": "Speaker A", "text": "extremist who"})
    assert events[-1]["text"] == "Kirk was killed by an extremist who"
    await asyncio.sleep(0.03)  # past nothing yet: the speech above restarted the wait
    rest = {"speaker": "Speaker A", "text": "extremist who had no plan.", "spoken_at": 3.0}
    await session.transcribed([rest], None)
    lines = [e for e in events if e["type"] == "line"]
    assert [l["text"] for l in lines] == [whole] and session.joiner.held is None


async def test_an_unfinished_sentence_goes_out_after_the_silence():
    events = []

    async def emit(event):
        events.append(event)

    session = Session(emit, FakeBackend({"So the thing is": []}), hold_seconds=0.01)
    await session.transcribed([{"speaker": "A", "text": "So the thing is", "spoken_at": 1.0}], None)
    assert kinds(events) == []
    while session._tasks:
        await asyncio.sleep(0.01)
    assert kinds(events) == ["line"]


async def test_context_is_the_previous_lines():
    _, backend, _ = await run({"a": [], "b": []}, [("A", "a"), ("B", "b")])
    assert backend.calls[1][2] == [("A", "a")]


async def test_a_failed_check_shows_an_error_and_no_claims():
    _, _, events = await run({}, [("A", "hello")])
    assert kinds(events) == ["line", "error"]


async def test_pushback_records_what_changed():
    session, _, events = await run({"a": [fact("a")]}, [("A", "a")])
    claim_id = events[1]["id"]
    await session.pushback(claim_id, "context matters")
    final = events[-1]
    assert final["verdict"] == "misleading"
    assert final["pushback"] == {"argument": "context matters", "note": "fair point", "previous": "false", "changed": True}


async def test_a_failed_pushback_says_why():
    session, backend, events = await run({"x": [fact("x")]}, [("A", "x")])

    async def down(claim, verdict, argument):
        raise ConnectionError("search is down")

    backend.pushback = down
    await session.pushback(events[1]["id"], "context matters")
    assert events[-1]["type"] == "pushback_failed" and "search is down" in events[-1]["message"]


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

    backend = FakeBackend({"a": [], "b": [{**fact("b", verdict="unverifiable", sources=()), "kind": "contested"}]})
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
