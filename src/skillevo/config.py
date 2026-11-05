"""Layered configuration: defaults < user config < project config < environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import paths

POLICY_MODES = ("review-only", "approval-required", "autonomous")


@dataclass
class Retrieval:
    max_chars: int = 1800
    max_items: int = 3
    min_terms: int = 2
    candidate_pool: int = 20
    time_budget_ms: int = 1500


@dataclass
class Collection:
    enabled: bool = True
    retention_days: int = 90
    max_payload_chars: int = 240
    max_events_per_project: int = 5000


@dataclass
class Detection:
    min_pattern_sessions: int = 3
    one_off_salience: float = 0.7
    min_cluster_size: int = 3
    candidate_salience: float = 0.9


@dataclass
class Policy:
    mode: str = "review-only"


@dataclass
class Llm:
    enabled: bool = False            # master opt-in for every model call (uses your own `claude -p` login)
    max_calls_per_day: int = 150
    timeout_s: int = 120
    claude_bin: str = "claude"


@dataclass
class Orchestration:
    """Tiered models, in the spirit of cheap workers + a stronger reviewer. Only used when [llm] enabled = true."""
    extract_model: str = "haiku"     # distil raw events into lessons
    judge_model: str = "haiku"       # first-pass duplicate/complementary triage
    escalate_model: str = "sonnet"   # second opinion when the first pass is unsure
    draft_model: str = "sonnet"      # write skill and merge drafts
    review_model: str = "opus"       # gate for consequential changes (merges, autonomous installs)
    max_calls_per_run: int = 40
    max_review_calls: int = 5
    max_parallel: int = 2
    budget_usd_per_run: float = 0.5  # stops a run once the (estimated) cost reaches this
    review_required: bool = True     # autonomous installs of model-drafted skills need a logged reviewer approval
    background: bool = False         # run the smart cycle from SessionStart in a detached process
    distill_batch: int = 8


@dataclass
class Skills:
    managed_dir: str = ".claude/skills"
    scan_dirs: list[str] = field(default_factory=lambda: [".claude/skills"])
    name_prefix: str = ""


@dataclass
class Evolution:
    max_attempts: int = 3
    min_improvement: float = 0.02
    auto_interval_hours: int = 24   # 0 disables the automatic cycle at SessionStart


@dataclass
class Curator:
    interests: list[str] = field(default_factory=list)
    overlap_threshold: float = 0.55
    related_threshold: float = 0.3
    max_skill_bytes: int = 20000
    min_age_days: int = 30


@dataclass
class Privacy:
    extra_redact_patterns: list[str] = field(default_factory=list)
    store_prompts: bool = True


@dataclass
class Config:
    retrieval: Retrieval = field(default_factory=Retrieval)
    collection: Collection = field(default_factory=Collection)
    detection: Detection = field(default_factory=Detection)
    policy: Policy = field(default_factory=Policy)
    llm: Llm = field(default_factory=Llm)
    orchestration: Orchestration = field(default_factory=Orchestration)
    skills: Skills = field(default_factory=Skills)
    evolution: Evolution = field(default_factory=Evolution)
    curator: Curator = field(default_factory=Curator)
    privacy: Privacy = field(default_factory=Privacy)


def _merge(obj: Any, data: dict[str, Any]) -> None:
    for f in fields(obj):
        if f.name not in data:
            continue
        cur = getattr(obj, f.name)
        val = data[f.name]
        if is_dataclass(cur) and isinstance(val, dict):
            _merge(cur, val)
        elif isinstance(cur, bool) and isinstance(val, bool):
            setattr(obj, f.name, val)
        elif isinstance(cur, (int, float)) and not isinstance(cur, bool) and isinstance(val, (int, float)) and not isinstance(val, bool):
            setattr(obj, f.name, type(cur)(val))
        elif isinstance(cur, str) and isinstance(val, str):
            setattr(obj, f.name, val)
        elif isinstance(cur, list) and isinstance(val, list):
            setattr(obj, f.name, [str(v) for v in val])


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load(root: Path | None = None) -> Config:
    cfg = Config()
    _merge(cfg, _read_toml(paths.user_config_path()))
    if root is not None:
        _merge(cfg, _read_toml(paths.project_config_path(root)))
    mode = os.environ.get("SKILLEVO_POLICY")
    if mode:
        cfg.policy.mode = mode
    if cfg.policy.mode not in POLICY_MODES:
        cfg.policy.mode = "review-only"
    return cfg


def disabled() -> bool:
    """Global kill switch. Checked first by every hook."""
    return os.environ.get("SKILLEVO_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")
