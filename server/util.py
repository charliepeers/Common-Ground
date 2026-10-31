import re
import time

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalize(text: str) -> str:
    """Lowercase, punctuation-free, single-spaced: the key for matching repeated lines and claims."""
    return " ".join(_NON_WORD.sub(" ", text.lower().translate(_QUOTES)).split())


def squash(text: str) -> str:
    """Whitespace and typographic quotes flattened, case kept: for checking that a quote is verbatim."""
    return " ".join(text.translate(_QUOTES).split())


def ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
