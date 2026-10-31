"""Incremental reader for the streamed {"claims": [...]} answer.

Claude writes the claims one after another. Rather than wait for the whole answer, we
announce each claim as soon as its quote, claim and kind are complete (so the words light up
on screen), and hand over the full object the moment its closing brace arrives.
"""
import json
import re

EARLY_FIELDS = ("quote", "claim", "kind")
_FIELD = r'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)"'


class ClaimStream:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.text = ""
        self.completed = 0  # claims handed over in full
        self.announced: set[int] = set()

    def feed(self, delta: str) -> list[tuple[str, int, dict]]:
        """Adds a text delta; returns ("early", i, fields) and ("done", i, claim) events in order."""
        self.text += delta
        events = []
        for i, (start, end) in enumerate(self._claim_spans()):
            if end is not None:
                if i < self.completed:
                    continue
                claim = json.loads(self.text[start:end])
                if i not in self.announced:
                    self.announced.add(i)
                    events.append(("early", i, {k: claim[k] for k in EARLY_FIELDS if k in claim}))
                events.append(("done", i, claim))
                self.completed = i + 1
            elif i not in self.announced:
                fields = _early_fields(self.text[start:])
                if fields:
                    self.announced.add(i)
                    events.append(("early", i, fields))
        return events

    def _claim_spans(self) -> list[tuple[int, int | None]]:
        """(start, end) of each object inside the claims array; end is None while it's still arriving."""
        spans: list[tuple[int, int | None]] = []
        depth, in_string, escaped, start = 0, False, False, None
        for pos, ch in enumerate(self.text):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch in "{[":
                depth += 1
                if depth == 3 and ch == "{":  # root object > claims array > claim
                    start = pos
            elif ch in "}]":
                if depth == 3 and ch == "}" and start is not None:
                    spans.append((start, pos + 1))
                    start = None
                depth -= 1
        if start is not None:
            spans.append((start, None))
        return spans


def _early_fields(partial: str) -> dict | None:
    fields = {}
    for name in EARLY_FIELDS:
        match = re.search(_FIELD.format(name=name), partial)
        if not match:
            return None
        fields[name] = json.loads(f'"{match.group(1)}"')
    return fields
