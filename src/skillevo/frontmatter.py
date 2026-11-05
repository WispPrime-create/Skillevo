"""Minimal YAML-subset frontmatter parser (stdlib only).

Supports what SKILL.md files and ECC instinct files use in practice: scalars, quoted strings, inline lists,
dash lists, one level of nested mappings and `|` / `>` block scalars. Anything else is kept as a raw string."""

from __future__ import annotations

import re

_FM = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def split(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    return parse(m.group(1)), text[m.end():]


def _scalar(raw: str):
    raw = raw.strip()
    if raw == "":
        return ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(p) for p in _split_inline(inner)] if inner else []
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def _split_inline(s: str) -> list[str]:
    parts, buf, quote = [], [], ""
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in (x.strip() for x in parts) if p != ""]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse(block: str) -> dict:
    lines = [ln.rstrip("\r") for ln in block.split("\n")]
    value, _ = _parse_map(lines, 0, 0)
    return value


def _parse_map(lines: list[str], i: int, base: int) -> tuple[dict, int]:
    out: dict = {}
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        ind = _indent(line)
        if ind < base:
            break
        if ind > base:
            i += 1  # stray over-indented line; skip rather than fail
            continue
        m = re.match(r"([^\s:#][^:]*?):(?:\s+(.*)|\s*)$", line.strip())
        if not m:
            i += 1
            continue
        key, rest = m.group(1).strip().strip("\"'"), (m.group(2) or "")
        i += 1
        if rest in ("|", ">", "|-", ">-", "|+", ">+"):
            buf = []
            while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > base):
                buf.append(lines[i].strip())
                i += 1
            sep = "\n" if rest.startswith("|") else " "
            out[key] = sep.join(buf).strip()
        elif rest == "":
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _indent(lines[j]) > base and lines[j].strip().startswith("- "):
                items = []
                while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("- ") and _indent(lines[j]) > base):
                    if lines[j].strip():
                        items.append(_scalar(lines[j].strip()[2:]))
                    j += 1
                out[key] = items
                i = j
            elif j < len(lines) and _indent(lines[j]) > base:
                child, i = _parse_map(lines, j, _indent(lines[j]))
                out[key] = child
            else:
                out[key] = ""
        else:
            # plain multi-line scalar continuation (indented lines that are not keys)
            val = rest
            while i < len(lines) and lines[i].strip() and _indent(lines[i]) > base and not re.match(r"\s*[^\s:#][^:]*:(\s|$)", lines[i]):
                val += " " + lines[i].strip()
                i += 1
            out[key] = _scalar(val)
    return out, i
