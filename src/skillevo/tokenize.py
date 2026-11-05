"""Language-light tokenizer. Latin words plus CJK character bigrams, so Chinese/Japanese text is searchable
without a segmentation library and without relying on FTS5's trigram tokenizer being compiled in."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]+")

STOPWORDS = frozenset(
    "a an and are as at be but by do does for from has have how i if in into is it its me my no not of on or "
    "please so that the their then there this to use used using was we what when where which will with you your "
    "can could should would just also make get got let want need ok okay yes thanks thank continue go ahead sure".split()
)


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text):
        w = word.lower()
        parts = [p for p in w.split("_") if p]
        if len(parts) > 1:
            out.append(w)
        for p in parts:
            if len(p) >= 2 and p not in STOPWORDS and not p.isdigit():
                out.append(p)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def unique_tokens(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for t in tokens(text):
        seen.setdefault(t, None)
    return list(seen)


def index_text(*parts: str) -> str:
    return " ".join(tokens(" ".join(parts)))


def fts_query(text: str, max_terms: int = 24) -> str:
    """Safe FTS5 MATCH expression: every term quoted, joined with OR. Never pass raw user text to MATCH."""
    terms = unique_tokens(text)[:max_terms]
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
