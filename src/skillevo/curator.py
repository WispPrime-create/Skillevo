"""Evidence-based skill curation for ANY set of skills (user-made, third-party, plugin bundles).

Every verdict carries the evidence sources behind it: structural | overlap | usage | profile. A skill with no usage
data is "unmeasured", never "waste". Retirement needs two independent sources or a structurally empty skill.
The curator only recommends and drafts; it never edits a skill."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable
from pathlib import Path

from . import candidates, evalharness, skills, tokenize
from .config import Config


@dataclass
class PairVerdict:
    """A second opinion on a similar pair (from a model or a caller). duplicate=None means undecided."""
    duplicate: bool | None
    reason: str = ""
    keep: str | None = None
    source: str = ""


@dataclass
class Finding:
    name: str
    path: str
    verdict: str = "keep"                       # keep | improve | merge | retire | review
    merge_into: str | None = None
    evidence: list[tuple[str, str]] = field(default_factory=list)
    est_tokens: int = 0
    quality: float = 0.0
    usage: str = "unmeasured"

    def sources(self) -> set[str]:
        return {s for s, _ in self.evidence}


def _headings(body: str) -> str:
    return " ".join(re.findall(r"(?m)^#{1,3}\s+(.+)$", body))


def _doc_tokens(s: skills.Skill) -> list[str]:
    name_t = tokenize.tokens(s.name.replace("-", "_") + " " + s.name.replace("-", " "))
    return name_t * 2 + tokenize.tokens(s.description) * 3 + tokenize.tokens(_headings(s.body)) * 2 + tokenize.tokens(s.body[:3000])


def tfidf_vectors(items: list[skills.Skill], keep: int = 60) -> list[dict[str, float]]:
    docs = [Counter(_doc_tokens(s)) for s in items]
    df: Counter[str] = Counter()
    for d in docs:
        df.update(d.keys())
    n = max(1, len(docs))
    vecs = []
    for d in docs:
        w = {t: (1 + math.log(c)) * math.log((n + 1) / (df[t] + 0.5)) for t, c in d.items()}
        top = dict(sorted(w.items(), key=lambda kv: -kv[1])[:keep])
        norm = math.sqrt(sum(v * v for v in top.values())) or 1.0
        vecs.append({t: v / norm for t, v in top.items()})
    return vecs


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def overlap_pairs(items: list[skills.Skill], threshold: float) -> list[tuple[int, int, float]]:
    vecs = tfidf_vectors(items)
    index: dict[str, list[int]] = defaultdict(list)
    for i, v in enumerate(vecs):
        for t in v:
            index[t].append(i)
    cand: set[tuple[int, int]] = set()
    for ids in index.values():
        if len(ids) > 40:  # ubiquitous term: carries no similarity signal and would make this quadratic
            continue
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                cand.add((ids[x], ids[y]))
    out = []
    for i, j in cand:
        sim = _dot(vecs[i], vecs[j])
        if sim >= threshold:
            out.append((i, j, sim))
    return sorted(out, key=lambda t: -t[2])


def _broken_refs(s: skills.Skill) -> list[str]:
    refs = set(re.findall(r"\]\(((?:\./)?(?:references|scripts|assets|examples)/[^)#\s]+)\)", s.body))
    refs |= set(re.findall(r"`((?:\./)?(?:references|scripts|assets)/[\w./-]+\.\w+)`", s.body))
    return sorted(r for r in refs if not (s.path / r.removeprefix("./")).exists())


def analyze(items: list[skills.Skill], cfg: Config, usage: dict[str, dict] | None = None,
            span_days: float = 0.0, interests: list[str] | None = None,
            judge: Callable[[skills.Skill, skills.Skill], PairVerdict | bool | None] | None = None) -> list[Finding]:
    findings = {s.name: Finding(s.name, str(s.path), est_tokens=s.size // 4) for s in items}
    by_name = {s.name: s for s in items}
    interests = interests if interests is not None else cfg.curator.interests
    measured = span_days >= cfg.curator.min_age_days and usage is not None

    # structural
    for s in items:
        f = findings[s.name]
        res = evalharness.evaluate(s.text, None, None, cfg.curator.max_skill_bytes, s.path.name)
        f.quality = res.score
        for e in res.errors:
            f.evidence.append(("structural", e))
        if res.parts["description"] < 0.5:
            f.evidence.append(("structural", "description does not say when to use the skill"))
        if s.size > cfg.curator.max_skill_bytes:
            f.evidence.append(("structural", f"{s.size} bytes (~{s.size // 4} tokens) exceeds budget {cfg.curator.max_skill_bytes}"))
        broken = _broken_refs(s)
        if broken:
            f.evidence.append(("structural", "broken file reference(s): " + ", ".join(broken[:3])))

    # overlap clusters (union-find over similar pairs)
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    sims: dict[tuple[int, int], float] = {}
    related: dict[int, list[tuple[float, int]]] = defaultdict(list)
    preferred: set[str] = set()
    for i, j, sim in overlap_pairs(items, cfg.curator.related_threshold):
        duplicate = sim >= cfg.curator.overlap_threshold
        note = ""
        if judge is not None:
            v = judge(items[i], items[j])
            if isinstance(v, bool) or v is None:
                v = PairVerdict(v)
            if v.duplicate is True and not duplicate:
                duplicate, note = True, f"judged a duplicate by {v.source or 'reviewer'} despite low similarity: {v.reason}"
            elif v.duplicate is False and duplicate:
                duplicate = False
                findings[items[i].name].evidence.append(
                    ("overlap", f"judged complementary to '{items[j].name}' (similarity {sim:.2f}) {v.reason}".rstrip()))
            elif v.duplicate is True:
                note = f"confirmed duplicate by {v.source or 'reviewer'}: {v.reason}"
            if v.duplicate is True and v.keep:
                preferred.add(v.keep)
        if duplicate:
            sims[(i, j)] = sim
            if note:
                findings[items[i].name].evidence.append(("overlap", note))
            parent[find(i)] = find(j)
        else:
            related[i].append((sim, j))
            related[j].append((sim, i))
    for i, rel in related.items():
        for sim, j in sorted(rel, reverse=True)[:3]:
            findings[items[i].name].evidence.append(("related", f"similar to '{items[j].name}' ({sim:.2f}); consider consolidating or cross-linking"))
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(i)
    for members in groups.values():
        if len(members) < 2:
            continue
        def rank(i: int):
            u = (usage or {}).get(items[i].name, {}).get("uses", 0)
            return (0 if items[i].name in preferred else 1, -u, -findings[items[i].name].quality, items[i].size, items[i].name)
        members.sort(key=rank)
        primary = items[members[0]]
        for i in members[1:]:
            f = findings[items[i].name]
            s_max = max((v for (a, b), v in sims.items() if i in (a, b) and (a in members and b in members)), default=0.0)
            f.merge_into = primary.name
            f.evidence.append(("overlap", f"similarity {s_max:.2f} with '{primary.name}'"))
        findings[primary.name].evidence.append(
            ("overlap", "absorbs: " + ", ".join(items[i].name for i in members[1:])))

    # usage
    for s in items:
        f = findings[s.name]
        if usage is None or not measured:
            f.usage = "unmeasured"
            continue
        u = usage.get(s.name, {}).get("uses", 0)
        f.usage = f"{u} use(s) in {span_days:.0f}d"
        if u == 0:
            f.evidence.append(("usage", f"no recorded use in a {span_days:.0f}-day observation window"))

    # profile (opt-in)
    if interests:
        want = set(tokenize.tokens(" ".join(interests)))
        for s in items:
            if not (want & set(tokenize.tokens(s.name.replace("-", " ") + " " + s.description + " " + _headings(s.body)))):
                findings[s.name].evidence.append(("profile", "no overlap with the interests you configured"))

    for f in findings.values():
        src = f.sources()
        structural_fatal = any(s == "structural" and d in ("empty body", "missing description") for s, d in f.evidence)
        is_primary = any(d.startswith("absorbs") for s, d in f.evidence)
        if structural_fatal or ("usage" in src and ("profile" in src or f.merge_into)):
            f.verdict = "retire"
        elif f.merge_into:
            f.verdict = "merge"
        elif "structural" in src:
            f.verdict = "improve"
        elif is_primary:
            f.verdict = "improve"  # consolidation target
        elif src & {"usage", "profile", "related"}:
            f.verdict = "review"
        else:
            f.verdict = "keep"
    return sorted(findings.values(), key=lambda f: (f.verdict, f.name))


def draft_merge(primary: skills.Skill, others: list[skills.Skill], max_bytes: int = 20000) -> str:
    """Deterministic consolidation draft: primary content plus the lines of the others that the primary lacks."""
    def sig(line: str) -> frozenset[str]:
        return frozenset(tokenize.tokens(line))

    seen = [sig(l) for l in primary.body.splitlines() if l.strip()]

    def novel(line: str) -> bool:
        s = sig(line)
        if len(s) < 3:
            return False
        return all(len(s & t) / max(1, len(s | t)) < 0.6 for t in seen)

    added: list[str] = []
    for o in others:
        lines = [l for l in o.body.splitlines() if l.strip() and not l.lstrip().startswith("#") and novel(l)]
        for l in lines:
            seen.append(sig(l))
        if lines:
            added.append(f"\n## Merged from {o.name}\n" + "\n".join(lines))
    text = primary.text.rstrip() + "\n" + "\n".join(added) + "\n"
    names = ", ".join(o.name for o in others)
    new_desc = (primary.description.strip() + f" Also covers: {names}.")[:1000]
    m = re.search(r"(?m)^description:[ \t]*(?![>|])\S.*$", text)
    if m:
        text = text[:m.start()] + "description: " + candidates.yaml_quote(new_desc) + text[m.end():]
    if len(text.encode("utf-8")) > max_bytes:
        text += f"\n<!-- merged draft exceeds {max_bytes} bytes; trim before use -->\n"
    return text


def report_markdown(findings: list[Finding], corpus: str, span_days: float) -> str:
    counts = Counter(f.verdict for f in findings)
    total_tokens = sum(f.est_tokens for f in findings)
    saved = sum(f.est_tokens for f in findings if f.verdict in ("merge", "retire"))
    lines = [f"# Skill curation report: {corpus}", "",
             f"{len(findings)} skills, ~{total_tokens:,} tokens of skill text. Observation window: "
             + (f"{span_days:.0f} days of telemetry." if span_days else "none (usage is unmeasured, so no usage-based verdicts)."), "",
             "| Verdict | Count |", "|---|---|"]
    lines += [f"| {v} | {counts.get(v, 0)} |" for v in ("keep", "improve", "merge", "retire", "review")]
    lines += ["", f"Merge/retire candidates account for ~{saved:,} tokens of skill text.", "",
              "Evidence sources: structural (file-level checks), overlap (near-duplicate by TF-IDF similarity), related (similar but "
              "not duplicate; needs a human or model judgement), usage (recorded invocations), "
              "profile (your configured interests). These are recommendations, not proof; read the evidence.", ""]
    for verdict in ("merge", "retire", "improve", "review"):
        group = [f for f in findings if f.verdict == verdict]
        if not group:
            continue
        lines += [f"## {verdict} ({len(group)})", ""]
        for f in group:
            tgt = f" -> {f.merge_into}" if f.merge_into else ""
            lines.append(f"- **{f.name}**{tgt} (~{f.est_tokens} tokens, quality {f.quality:.2f}, usage: {f.usage})")
            for src, detail in f.evidence:
                lines.append(f"  - [{src}] {detail}")
        lines.append("")
    return "\n".join(lines)


def scan_dirs(dirs: list[str], root: Path) -> list[Path]:
    out = []
    for d in dirs:
        p = Path(d).expanduser()
        out.append(p if p.is_absolute() else root / p)
    return out
