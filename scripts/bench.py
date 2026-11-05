"""Measure retrieval and full-hook latency on synthetic data. Uses a temporary data directory only.

    python scripts/bench.py [--experiences 5000] [--queries 200]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

WORDS = ("build test deploy docker cache migrate database schema index query python rust java react vue lint format "
         "branch merge rebase commit release version config secret token auth login session timeout retry error "
         "exception stack trace log metric alert queue worker thread async await module import package install").split()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiences", type=int, default=5000)
    ap.add_argument("--queries", type=int, default=200)
    args = ap.parse_args()
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SKILLEVO_HOME"] = str(Path(tmp) / "data")
        os.environ["CLAUDE_PROJECT_DIR"] = tmp
        from skillevo import config, paths, retrieve
        from skillevo.store import Store

        project = paths.project_id(paths.project_root())
        cfg = config.load()
        with Store(paths.db_path()) as s:
            s.db.execute("BEGIN")
            for i in range(args.experiences):
                title = " ".join(rng.sample(WORDS, 4))
                body = " ".join(rng.choices(WORDS, k=24)) + f" note{i}"
                s.add_experience(project, "lesson", title, body, salience=rng.random())
            s.db.commit()
            lat, chars = [], []
            for _ in range(args.queries):
                prompt = " ".join(rng.choices(WORDS, k=8))
                t = time.perf_counter()
                text = retrieve.retrieve(s, cfg, project, prompt)
                lat.append((time.perf_counter() - t) * 1000)
                chars.append(len(text))
        lat.sort()
        print(f"in-process retrieval over {args.experiences} experiences, {args.queries} queries:")
        print(f"  p50 {statistics.median(lat):.2f} ms   p95 {lat[int(len(lat) * .95) - 1]:.2f} ms   max {lat[-1]:.2f} ms")
        print(f"  injected chars: avg {statistics.mean(chars):.0f}, max {max(chars)} (cap {cfg.retrieval.max_chars})")
        payload = json.dumps({"session_id": "b", "cwd": tmp, "prompt": "docker cache build deploy error"})
        env = dict(os.environ, PYTHONPATH=str(SRC))
        runs = []
        for _ in range(15):
            t = time.perf_counter()
            subprocess.run([sys.executable, "-m", "skillevo", "hook", "UserPromptSubmit"], input=payload, text=True,
                           capture_output=True, env=env)
            runs.append((time.perf_counter() - t) * 1000)
        runs.sort()
        print(f"full hook process (interpreter start + import + retrieval), 15 runs: median {statistics.median(runs):.0f} ms, max {runs[-1]:.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
