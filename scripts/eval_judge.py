"""Measure duplicate-vs-complementary judging against hand-labelled skill pairs. Spends model quota (about 20-30 small calls).

    python scripts/fetch_ecc_corpus.py
    python scripts/eval_judge.py corpus/ECC/skills [--labels tests/fixtures/ecc_pair_labels.json] [--yes]

Compares: (1) TF-IDF threshold alone, (2) the first-pass model, (3) first pass with escalation to the second model.
Labels are from a single labeler (see the fixture); treat the numbers as indicative, not as a benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--labels", default=str(Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "ecc_pair_labels.json"))
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SKILLEVO_HOME"] = tmp
        from skillevo import config, curator, orchestrate, skills, smart
        from skillevo.store import Store

        cfg = config.Config()
        cfg.llm.enabled = True
        cfg.orchestration.max_calls_per_run = 100
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))["pairs"]
        by_name = {s.name: s for s in skills.discover([Path(args.corpus)])}
        pairs = [(by_name[p["a"]], by_name[p["b"]]) for p in labels]
        truth = [p["label"] == "duplicate" for p in labels]
        sims = {}
        items = list(by_name.values())
        idx = {s.name: i for i, s in enumerate(items)}
        for i, j, sim in curator.overlap_pairs(items, 0.0):
            sims[(items[i].name, items[j].name)] = sims[(items[j].name, items[i].name)] = sim
        tfidf = [sims.get((a.name, b.name), 0.0) >= cfg.curator.overlap_threshold for a, b in pairs]

        def score(name: str, pred: list[bool | None]) -> None:
            tp = sum(1 for p, t in zip(pred, truth) if p is True and t)
            fp = sum(1 for p, t in zip(pred, truth) if p is True and not t)
            fn = sum(1 for p, t in zip(pred, truth) if p is not True and t)
            und = sum(1 for p in pred if p is None)
            correct = sum(1 for p, t in zip(pred, truth) if (p is True) == t and p is not None)
            print(f"{name:<34} correct {correct}/{len(pred)}   duplicates found {tp}/{sum(truth)}   false merges {fp}   undecided {und}")

        with Store(Path(tmp) / "x.db") as store:
            dry = orchestrate.Orchestrator(store, cfg, dry_run=True)
            smart.judge_pairs(dry, pairs)
            print("planned:", dry.describe())
            if not args.yes and input("Run these model calls? [y/N] ").strip().lower() != "y":
                return 1
            first_only = orchestrate.Orchestrator(store, cfg)
            cfg1 = cfg.orchestration.max_calls_per_run
            res1 = smart.judge_pairs(first_only, pairs)  # includes escalation; derive first-pass-only from sources
            full = [res1[(a.name, b.name)] for a, b in pairs]
            haiku = [j.duplicate if j.source == first_only.model_for("judge") else None for j in full]
            print()
            score("TF-IDF threshold alone", [bool(x) for x in tfidf])
            score(f"{first_only.model_for('judge')} only (unsure = undecided)", haiku)
            score("with escalation", [j.duplicate for j in full])
            wanted = {(p["a"], p["b"]): p.get("keep") for p in labels if p["label"] == "duplicate"}
            kept = [(j.keep == wanted[(a.name, b.name)]) for (a, b), j in zip(pairs, full) if (a.name, b.name) in wanted and j.duplicate]
            print(f"survivor chosen correctly for {sum(kept)}/{len(wanted)} true duplicates")
            print("\ncalls:", first_only.describe())
            for (a, b), j, t in zip(pairs, full, truth):
                mark = "ok " if (j.duplicate is True) == t and j.duplicate is not None else "MISS"
                print(f"  {mark} {a.name} | {b.name}: truth={'dup' if t else 'comp'} model={j.duplicate} via {j.source}: {j.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
