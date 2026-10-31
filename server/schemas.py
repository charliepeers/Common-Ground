"""JSON schemas for Claude's structured answers (every object closed with additionalProperties: false)."""

VERDICTS = ["true", "false", "misleading", "unverifiable"]
CONFIDENCE = ["high", "medium", "low"]
KINDS = ["fact", "contested", "value"]
STANCES = ["agrees", "disagrees", "complicates"]


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_VERDICT_FIELDS = {
    "verdict": {"type": "string", "enum": VERDICTS},
    "confidence": {"type": "string", "enum": CONFIDENCE},
    "explanation": {"type": "string"},
    "correction": {"type": "string"},
}

# Property order matters: the stream reader lights up a claim once quote, claim and kind are in.
CHECK_SCHEMA = _object({
    "claims": {
        "type": "array",
        "items": _object({
            "quote": {"type": "string"},
            "claim": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            **_VERDICT_FIELDS,
            "source_ids": {"type": "array", "items": {"type": "integer"}},
            "needs_search": {"type": "boolean"},
            "query": {"type": "string"},
            "voices": {"type": "array", "items": {"type": "string"}},
        }),
    }
})

VERIFY_SCHEMA = _object({
    **_VERDICT_FIELDS,
    "source_ids": {"type": "array", "items": {"type": "integer"}},
})

PERSPECTIVES_SCHEMA = _object({
    "perspectives": {
        "type": "array",
        "items": _object({
            "person": {"type": "string"},
            "stance": {"type": "string", "enum": STANCES},
            "summary": {"type": "string"},
            "quote": {"type": "string"},
            "source_id": {"type": "integer"},
        }),
    }
})


SPEAKER_NAMES_SCHEMA = _object({
    "guesses": {
        "type": "array",
        "items": _object({
            "speaker": {"type": "string"},
            "name": {"type": "string"},
            "evidence": {"type": "string"},
            "points_to": {"type": "string", "enum": ["current", "previous", "next"]},
        }),
    }
})


def submit_verdict_tool(with_note: bool) -> dict:
    """Client tool Claude calls to hand back a verdict after web searching (strict: schema-valid input)."""
    properties = {**_VERDICT_FIELDS, "source_urls": {"type": "array", "items": {"type": "string"}}}
    if with_note:
        properties["note"] = {"type": "string"}
    return {
        "name": "submit_verdict",
        "description": "Record your final verdict on the claim. Call it exactly once, after searching.",
        "strict": True,
        "input_schema": _object(properties),
    }
