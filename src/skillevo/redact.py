"""Secret redaction (applied when storing) and injection hygiene (applied before re-injecting)."""

from __future__ import annotations

import re

_SENSITIVE_NAME = r"[A-Za-z0-9_.-]*(?:secret|token|passwd|password|pwd|api[_-]?key|apikey|auth|credential|private[_-]?key)[A-Za-z0-9_.-]*"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "[REDACTED:private-key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack-token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:api-key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED:jwt]"),
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*(?:bearer|basic|token)?\s*[^\s'\"]+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(\b" + _SENSITIVE_NAME + r")(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(--(?:password|passwd|token|secret|api-key|apikey)[= ])\S+"), r"\1[REDACTED]"),
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1[REDACTED]@"),
]

_extra: list[re.Pattern[str]] = []


def set_extra_patterns(patterns: list[str]) -> None:
    _extra.clear()
    for p in patterns:
        try:
            _extra.append(re.compile(p))
        except re.error:
            continue


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    for pat in _extra:
        text = pat.sub("[REDACTED]", text)
    return text


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def clean(text: str, limit: int) -> str:
    """Redact first, then truncate, so a secret cut in half cannot leak its prefix."""
    return truncate(redact(text), limit)


_INJECTION = re.compile(
    r"(?i)(ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)|disregard (?:the |all )?(?:previous|prior|above)|"
    r"you are now\b|new system prompt|reveal (?:your )?system prompt|<\s*/?\s*(?:system|assistant|tool)[-_ ]?\w*\s*>|"
    r"\bsystem-reminder\b)"
)


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_for_injection(text: str) -> str:
    """Make stored text inert when it is placed back into the model's context."""
    text = _CONTROL.sub("", text)
    text = text.replace("<", "‹").replace(">", "›").replace("`", "'")
    return re.sub(r"\s*\n\s*", " ", text).strip()
