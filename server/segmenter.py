"""When does a statement get sent for checking? Deepgram streaming results in, statements out.

Interim results are never checked; they only drive the live caption. Final words collect into
the current speaker's statement, which is sent as soon as one of these happens:

1. Another speaker starts talking (diarization). A change of turn ends a statement.
2. A word ends a sentence (. ? !) and the statement has at least 3 words. Abbreviations such as
   "U.S." or "Dr." don't count as sentence ends.
3. The statement reaches 35 words without a sentence end, so run-on speech still gets checked.
4. Deepgram reports a pause (speech_final: 300 ms of silence) and the statement has 6+ words.
5. Deepgram reports the utterance is over (UtteranceEnd: 1 s without words). Whatever is left goes.

Sentences are the unit because a claim rarely spans two of them, and waiting for the end of a
whole turn would hold every claim until the speaker stops. The previous lines travel with each
statement as context, so "He was the 45th too." still resolves who "he" is.
"""
from __future__ import annotations

import time

MIN_SENTENCE_WORDS = 3
PAUSE_MIN_WORDS = 6
MAX_WORDS = 35
ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "sr.", "jr.", "vs.", "etc.", "e.g.", "i.e.",
    "u.s.", "u.k.", "u.n.", "a.m.", "p.m.", "no.", "inc.", "ltd.", "co.", "corp.", "approx.",
    "dept.", "gov.", "sen.", "rep.", "gen.", "col.", "lt.", "mt.", "jan.", "feb.", "aug.",
    "sept.", "oct.", "nov.", "dec.",
}
_CLOSERS = "\"')]”’"


def speaker_name(number) -> str:
    return f"Speaker {int(number) + 1}" if number is not None else "Speaker"


def ends_sentence(word: str) -> bool:
    word = word.rstrip(_CLOSERS)
    return word.endswith((".", "?", "!")) and word.lower() not in ABBREVIATIONS


def _text(word: dict) -> str:
    return word.get("punctuated_word") or word.get("word", "")


class Segmenter:
    def __init__(self, started_at: float | None = None):
        self.started_at = started_at or time.time()  # wall clock at audio time zero
        self.words: list[dict] = []
        self.speaker: str | None = None

    def feed(self, message: dict) -> tuple[dict | None, list[dict]]:
        """Returns (caption to show or None, statements ready to check)."""
        kind = message.get("type")
        if kind == "UtteranceEnd":
            ready = self._flush()
            return self._caption(), ready
        if kind != "Results":
            return None, []
        alternatives = (message.get("channel") or {}).get("alternatives") or [{}]
        words = alternatives[0].get("words") or []
        if not message.get("is_final"):
            speaker = speaker_name(words[0].get("speaker")) if words else self.speaker
            return self._caption(" ".join(_text(w) for w in words), speaker), []
        ready: list[dict] = []
        for word in words:
            speaker = speaker_name(word.get("speaker"))
            if self.words and speaker != self.speaker:
                ready += self._flush()
            self.speaker = speaker
            self.words.append(word)
            if (ends_sentence(_text(word)) and len(self.words) >= MIN_SENTENCE_WORDS) or len(self.words) >= MAX_WORDS:
                ready += self._flush()
        if message.get("speech_final") and len(self.words) >= PAUSE_MIN_WORDS:
            ready += self._flush()
        return self._caption(), ready

    def _caption(self, live: str = "", speaker: str | None = None) -> dict:
        pending = " ".join(_text(w) for w in self.words)
        text = " ".join(part for part in (pending, live) if part)
        return {"type": "caption", "speaker": speaker or self.speaker or "", "text": text}

    def _flush(self) -> list[dict]:
        if not self.words:
            return []
        statement = {
            "speaker": self.speaker,
            "text": " ".join(_text(w) for w in self.words),
            "spoken_at": self.started_at + float(self.words[-1].get("end", 0)),
        }
        self.words = []
        return [statement]


def split_sentences(text: str) -> list[str]:
    """Splits a finished turn into sentences, using the same rules as above (at least 3 words each)."""
    sentences, current = [], []
    for word in text.split():
        current.append(word)
        if ends_sentence(word) and len(current) >= MIN_SENTENCE_WORDS:
            sentences.append(" ".join(current))
            current = []
    if current:
        if sentences and len(current) < MIN_SENTENCE_WORDS:
            sentences[-1] += " " + " ".join(current)
        else:
            sentences.append(" ".join(current))
    return sentences


class TurnSegmenter:
    """The same decision for AssemblyAI, whose stream is organised in turns rather than segments.

    Each Turn message carries the turn so far. When the turn ends (end_of_turn), its punctuated
    text goes out sentence by sentence. A turn that runs past 35 final words without ending (a
    lecture, a speech) has those words sent early, so long monologues still get checked as they go.
    AssemblyAI labels speakers A, B, ...; turns under about a second come back UNKNOWN, and keep
    the last known speaker.
    """

    def __init__(self, started_at: float | None = None):
        self.started_at = started_at or time.time()
        self.turn = None
        self.sent = 0  # words of the current turn already sent early
        self.done = False  # the current turn has ended and gone out
        self.speaker: str | None = None

    def feed(self, message: dict) -> tuple[dict | None, list[dict]]:
        if message.get("type") != "Turn":
            return None, []
        if message.get("turn_order") != self.turn:
            self.turn, self.sent, self.done = message.get("turn_order"), 0, False
        label = message.get("speaker_label")
        if label and label not in ("UNKNOWN", "PENDING"):
            self.speaker = f"Speaker {label}"
        speaker = self.speaker or "Speaker"
        words = message.get("words") or []
        if self.done:
            return None, []
        if message.get("end_of_turn"):
            self.done = True
            rest = words[self.sent:]
            text = " ".join(w.get("text", "") for w in rest) if self.sent else message.get("transcript", "")
            spoken_at = self._at(rest[-1] if rest else None)
            ready = [{"speaker": speaker, "text": s, "spoken_at": spoken_at} for s in split_sentences(text)]
            return {"type": "caption", "speaker": speaker, "text": ""}, ready
        ready = []
        final = [w for w in words if w.get("word_is_final")]
        if len(final) - self.sent >= MAX_WORDS:
            chunk = final[self.sent:]
            ready.append({"speaker": speaker, "text": " ".join(w.get("text", "") for w in chunk), "spoken_at": self._at(chunk[-1])})
            self.sent = len(final)
        live = " ".join(w.get("text", "") for w in words[self.sent:])
        return {"type": "caption", "speaker": speaker, "text": live}, ready

    def _at(self, word: dict | None) -> float:
        return self.started_at + float(word.get("end", 0)) / 1000 if word else time.time()  # AssemblyAI times are in ms


class SentenceJoiner:
    """Puts back a sentence that speech-to-text cut in two.

    AssemblyAI ends a turn after a short silence, punctuation or not (768 ms with speaker labels), so
    a speaker who pauses mid-sentence arrives as two statements, and the second makes no sense alone.
    A statement with no sentence end waits here and joins what the same speaker says next. It goes
    out as it is when someone else talks, at 35 words, or on release().
    """

    def __init__(self) -> None:
        self.held: dict | None = None

    def feed(self, statement: dict) -> list[dict]:
        """Returns the statements ready to check."""
        ready = self.release() if self.held and self.held["speaker"] != statement["speaker"] else []
        if self.held:
            statement = {**statement, "text": f"{self.held['text']} {statement['text']}"}
            self.held = None
        words = statement["text"].split()
        if words and (ends_sentence(words[-1]) or len(words) >= MAX_WORDS):
            ready.append(statement)
        else:
            self.held = statement
        return ready

    def release(self) -> list[dict]:
        held, self.held = self.held, None
        return [held] if held else []

    def caption(self, caption: dict) -> dict:
        """A held statement stays on screen, ahead of what the same speaker is saying now."""
        if self.held and self.held["speaker"] == caption["speaker"]:
            return {**caption, "text": f"{self.held['text']} {caption['text']}".rstrip()}
        return caption
