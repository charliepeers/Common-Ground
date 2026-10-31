from server.segmenter import Segmenter, ends_sentence


def results(words, is_final=True, speech_final=False):
    """A Deepgram Results message. words: list of (text, speaker, end_seconds)."""
    return {
        "type": "Results",
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{
            "transcript": " ".join(w for w, _, _ in words),
            "words": [{"word": w.lower().strip(".?!,"), "punctuated_word": w, "speaker": s, "end": e} for w, s, e in words],
        }]},
    }


def words(text, speaker=0, start=0.0):
    return [(w, speaker, start + 0.3 * (i + 1)) for i, w in enumerate(text.split())]


def test_interim_results_only_update_the_caption():
    seg = Segmenter(started_at=1000.0)
    caption, ready = seg.feed(results(words("Joe Biden is the"), is_final=False))
    assert ready == [] and caption == {"type": "caption", "speaker": "Speaker 1", "text": "Joe Biden is the"}


def test_sentence_end_sends_the_statement_and_keeps_the_rest():
    seg = Segmenter(started_at=1000.0)
    _, ready = seg.feed(results(words("Joe Biden is the 47th president. And also")))
    assert [r["text"] for r in ready] == ["Joe Biden is the 47th president."]
    assert ready[0]["speaker"] == "Speaker 1"
    assert abs(ready[0]["spoken_at"] - (1000.0 + 0.3 * 6)) < 1e-9
    assert seg.feed({"type": "UtteranceEnd"})[1][0]["text"] == "And also"


def test_abbreviations_do_not_end_a_sentence():
    seg = Segmenter()
    _, ready = seg.feed(results(words("The U.S. spends more on defense than anyone.")))
    assert [r["text"] for r in ready] == ["The U.S. spends more on defense than anyone."]
    assert not ends_sentence("Dr.") and ends_sentence('true."') and ends_sentence("right?")


def test_speaker_change_ends_a_statement():
    seg = Segmenter()
    _, ready = seg.feed(results(words("No it is not", 0) + words("Yes it is", 1, 2.0)))
    assert [(r["speaker"], r["text"]) for r in ready] == [("Speaker 1", "No it is not")]
    assert seg.feed({"type": "UtteranceEnd"})[1] == [{"speaker": "Speaker 2", "text": "Yes it is", "spoken_at": seg.started_at + 2.9}]


def test_pause_sends_long_statements_but_waits_on_fragments():
    seg = Segmenter()
    assert seg.feed(results(words("so um the thing"), speech_final=True))[1] == []
    _, ready = seg.feed(results(words("is that nuclear power is safest", start=2.0), speech_final=True))
    assert [r["text"] for r in ready] == ["so um the thing is that nuclear power is safest"]


def test_run_on_speech_is_cut_at_35_words():
    seg = Segmenter()
    _, ready = seg.feed(results(words(" ".join(["word"] * 40))))
    assert len(ready) == 1 and len(ready[0]["text"].split()) == 35


def test_short_sentences_wait_for_more_words():
    seg = Segmenter()
    assert seg.feed(results(words("Right.")))[1] == []
    _, ready = seg.feed(results(words("He was the 45th.", start=1.0)))
    assert [r["text"] for r in ready] == ["Right. He was the 45th."]


from server.segmenter import TurnSegmenter, split_sentences


def turn(order, words, end=False, label="A", transcript=None):
    """An AssemblyAI Turn message. words: list of (text, is_final, end_ms)."""
    return {
        "type": "Turn", "turn_order": order, "end_of_turn": end, "speaker_label": label,
        "transcript": transcript if transcript is not None else " ".join(w for w, _, _ in words),
        "words": [{"text": w, "word_is_final": f, "end": e, "speaker": label} for w, f, e in words],
    }


def test_split_sentences_keeps_abbreviations_and_merges_stubs():
    assert split_sentences("The U.S. is big. Yes. It is huge.") == ["The U.S. is big.", "Yes. It is huge."]
    assert split_sentences("no punctuation at all") == ["no punctuation at all"]


def test_turn_end_sends_each_sentence_with_the_speaker():
    seg = TurnSegmenter(started_at=100.0)
    caption, ready = seg.feed(turn(0, [("Biden", False, 300), ("is", False, 500)]))
    assert ready == [] and caption["text"] == "Biden is" and caption["speaker"] == "Speaker A"
    _, ready = seg.feed(turn(0, [("Biden", True, 300), ("is", True, 500), ("47th.", True, 900), ("Right?", True, 1300)], end=True,
                             transcript="Biden is 47th. Right?"))
    assert [(r["speaker"], r["text"]) for r in ready] == [("Speaker A", "Biden is 47th. Right?")]
    assert abs(ready[0]["spoken_at"] - 101.3) < 1e-9
    assert seg.feed(turn(0, [], end=True, transcript="Biden is 47th. Right?"))[1] == []  # a repeated end isn't sent twice


def test_long_turns_are_cut_and_the_rest_goes_at_turn_end():
    seg = TurnSegmenter()
    words = [(f"w{i}", True, i * 100) for i in range(40)]
    _, early = seg.feed(turn(3, words))
    assert len(early) == 1 and len(early[0]["text"].split()) == 40
    _, rest = seg.feed(turn(3, words + [("done.", True, 4100)], end=True))
    assert [r["text"] for r in rest] == ["done."]  # only the words not already sent


def test_unknown_short_turns_keep_the_last_speaker():
    seg = TurnSegmenter()
    seg.feed(turn(0, [("Hello", True, 100), ("there", True, 300), ("friend.", True, 500)], end=True, label="B"))
    _, ready = seg.feed(turn(1, [("Yes", True, 100), ("it", True, 200), ("is.", True, 300)], end=True, label="UNKNOWN"))
    assert ready[0]["speaker"] == "Speaker B"
