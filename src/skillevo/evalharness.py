"""Evaluation of skill text, deterministic by default.

What the deterministic score measures: structural validity, description quality, size budget, coverage of the
evidence a skill claims to encode, and the pass rate of user-written assertions on the skill text. It does NOT
prove a skill improves task outcomes. Outcome-level checks need `run_live` (opt-in, spends model quota)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from . import skills, tokenize

PASS_THRESHOLD = 0.7


@dataclass
class EvalResult:
    score: float
    parts: dict[str, float]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed_assertions: set[str] = field(default_factory=set)
    failed_assertions: set[str] = field(default_factory=set)
    live: dict[str, float] | None = None

    def to_json(self) -> dict:
        return {"score": self.score, "parts": self.parts, "errors": self.errors, "warnings": self.warnings,
                "passed_assertions": sorted(self.passed_assertions), "failed_assertions": sorted(self.failed_assertions),
                "live": self.live}


def load_suite(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        suite = json.load(fh)
    if not isinstance(suite, dict) or not isinstance(suite.get("cases", []), list):
        raise ValueError("suite must be an object with a 'cases' list")
    return suite


def check_assertion(a: dict, text: str) -> bool:
    kind, value = a.get("type"), str(a.get("value", ""))
    low = text.lower()
    if kind == "contains":
        return value.lower() in low
    if kind == "not_contains":
        return value.lower() not in low
    if kind == "regex":
        try:
            return re.search(value, text, re.I | re.S) is not None
        except re.error:
            return False
    if kind == "max_chars":
        return len(text) <= int(value)
    return False


def _assertion_id(case: dict, i: int, a: dict) -> str:
    return f"{case.get('id', 'case')}#{i}:{a.get('type')}:{str(a.get('value', ''))[:30]}"


def _description_quality(desc: str) -> float:
    if not desc:
        return 0.0
    score = 0.0
    if 40 <= len(desc) <= 500:
        score += 0.5
    elif len(desc) >= 20:
        score += 0.25
    if re.search(r"(?i)\b(use when|use this when|when the user|when working|when you|whenever|if the user)\b", desc):
        score += 0.5
    return score


def _size_score(size: int, budget: int) -> float:
    if size <= budget * 0.5:
        return 1.0
    if size <= budget:
        return 0.7
    return max(0.0, 0.7 - (size - budget) / budget)


def _evidence_coverage(text: str, evidence_terms: list[str]) -> float:
    if not evidence_terms:
        return 1.0
    have = set(tokenize.tokens(text))
    return sum(1 for t in evidence_terms if t in have) / len(evidence_terms)


def evaluate(text: str, suite: dict | None = None, evidence_terms: list[str] | None = None,
             max_bytes: int = 20000, dir_name: str = "") -> EvalResult:
    errors, warnings = skills.validate(text, dir_name)
    meta, _ = skills.parse(text, dir_name)
    desc = meta.get("description", "") if isinstance(meta.get("description"), str) else ""
    parts = {
        "validity": 0.0 if errors else (0.9 if warnings else 1.0),
        "description": _description_quality(desc),
        "size": _size_score(len(text.encode("utf-8")), max_bytes),
        "evidence": _evidence_coverage(text, evidence_terms or []),
    }
    weights = {"validity": 0.3, "description": 0.2, "size": 0.15, "evidence": 0.2}
    passed: set[str] = set()
    failed: set[str] = set()
    if suite:
        for case in suite.get("cases", []):
            for i, a in enumerate(case.get("assertions", [])):
                if a.get("target", "skill") != "skill":
                    continue
                aid = _assertion_id(case, i, a)
                (passed if check_assertion(a, text) else failed).add(aid)
        if passed or failed:
            parts["assertions"] = len(passed) / (len(passed) + len(failed))
            weights["assertions"] = 0.15
    total_w = sum(weights.values())
    score = round(sum(parts[k] * weights[k] for k in weights) / total_w, 4)
    if errors:
        score = min(score, 0.4)
    return EvalResult(score, parts, errors, warnings, passed, failed)


@dataclass
class Verdict:
    promote: bool
    reason: str
    candidate: EvalResult
    current: EvalResult | None


def compare(candidate: EvalResult, current: EvalResult | None, min_improvement: float = 0.02) -> Verdict:
    if candidate.errors:
        return Verdict(False, "candidate has validation errors: " + "; ".join(candidate.errors), candidate, current)
    if current is None:
        ok = candidate.score >= PASS_THRESHOLD and not candidate.failed_assertions
        return Verdict(ok, f"new skill score {candidate.score} (threshold {PASS_THRESHOLD})"
                       + ("" if ok else "; below threshold or failing assertions"), candidate, None)
    regressed = current.passed_assertions - candidate.passed_assertions
    if regressed:
        return Verdict(False, f"regression: {len(regressed)} previously passing assertion(s) now fail", candidate, current)
    if candidate.live is not None and current.live is not None and candidate.live.get("with_skill", 0) < current.live.get("with_skill", 0):
        return Verdict(False, "live evaluation is worse than the current version", candidate, current)
    gain = candidate.score - current.score
    if gain >= min_improvement:
        return Verdict(True, f"score {current.score} -> {candidate.score}", candidate, current)
    return Verdict(False, f"no sufficient improvement ({gain:+.3f} < {min_improvement})", candidate, current)


def run_live(skill_text: str, suite: dict, runner: Callable[[str], str], repeats: int = 1) -> dict[str, float]:
    """Opt-in outcome evaluation. `runner(prompt) -> output` is supplied by the caller (library use only: there is no CLI path yet).
    Output-targeted assertions are graded with and without the skill text in the prompt."""
    def rate(prefix: str) -> float:
        ok = total = 0
        for case in suite.get("cases", []):
            outs = [runner(prefix + case.get("prompt", "")) for _ in range(max(1, repeats))]
            for a in case.get("assertions", []):
                if a.get("target") != "output":
                    continue
                for out in outs:
                    total += 1
                    ok += check_assertion(a, out)
        return ok / total if total else 0.0
    return {"with_skill": rate(f"Follow these skill instructions:\n{skill_text}\n\nTask:\n"), "without_skill": rate("")}
