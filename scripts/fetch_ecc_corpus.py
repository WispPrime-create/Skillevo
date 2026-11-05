"""Fetch ECC's skills as an OPTIONAL test corpus into ./corpus/ECC (git-ignored), pinned to a known commit.

    python scripts/fetch_ecc_corpus.py
    SKILLEVO_TEST_CORPUS=corpus/ECC/skills python -m unittest discover -s tests -t .
    skillevo curate corpus/ECC/skills --out out/ecc-report

ECC is MIT licensed (see NOTICE). It is never installed or executed by SkillEvo."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = "https://github.com/affaan-m/ECC.git"
COMMIT = "b38be6a7f6350964d59328b52f0624e1d1d552d1"  # v2.2.3; bump deliberately and re-run the curator tests
DEST = Path(__file__).resolve().parent.parent / "corpus" / "ECC"


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def main() -> int:
    if (DEST / ".git").exists():
        print(f"{DEST} already exists; delete it to refetch.")
        return 0
    DEST.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=DEST)
    git("remote", "add", "origin", REPO, cwd=DEST)
    git("fetch", "--depth", "1", "origin", COMMIT, cwd=DEST)
    git("checkout", "-q", "FETCH_HEAD", cwd=DEST)
    print(f"Fetched ECC {COMMIT[:7]} into {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
