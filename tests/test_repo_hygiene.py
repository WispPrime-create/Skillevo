"""Guards for a public repository: no personal paths/identities, version consistency, required files."""

import os
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Generic patterns only. Maintainers can add personal strings (names, emails, handles) that must never be committed via
# the SKILLEVO_FORBIDDEN env var (comma separated) or an untracked .dev-notes/forbidden.txt (one per line).
GENERIC = ["/Us" + "ers/", "/ho" + "me/", "C:\\Us" + "ers\\"]


def forbidden():
    extra = [s for s in os.environ.get("SKILLEVO_FORBIDDEN", "").split(",") if s.strip()]
    f = ROOT / ".dev-notes" / "forbidden.txt"
    if f.is_file():
        extra += [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return GENERIC + extra


FORBIDDEN = forbidden()
SKIP_DIRS = {".git", ".dev-notes", "corpus", "out", "vendor", "__pycache__", "build", "dist"}


def project_files():
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.splitlines()
        files = [ROOT / p for p in out]
    except (OSError, subprocess.CalledProcessError):
        files = [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)]
    return [p for p in files if p.is_file() and not p.name.endswith((".pyc", ".png", ".jpg"))]


class HygieneTests(unittest.TestCase):
    def test_no_personal_identifiers_in_publishable_files(self):
        offenders = []
        for p in project_files():
            if p.name == "test_repo_hygiene.py":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for bad in FORBIDDEN:
                if bad in text:
                    offenders.append(f"{p.relative_to(ROOT)}: {bad!r}")
        self.assertEqual(offenders, [])

    def test_required_files_exist(self):
        for name in ("LICENSE", "NOTICE", "README.md", "CHANGELOG.md", "pyproject.toml", ".github/workflows/ci.yml",
                     "docs/ARCHITECTURE.md", "docs/USAGE.md", "docs/TROUBLESHOOTING.md", "docs/PRIVACY.md",
                     "examples/config.toml", "examples/evals.json"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_versions_agree(self):
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        init = (ROOT / "src" / "skillevo" / "__init__.py").read_text(encoding="utf-8")
        self.assertEqual(re.search(r'__version__ = "([^"]+)"', init).group(1), meta["project"]["version"])
        self.assertIn(meta["project"]["version"], (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_no_third_party_runtime_dependencies(self):
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(meta["project"]["dependencies"], [])

    def test_example_config_loads_and_matches_defaults(self):
        from skillevo import config
        data = tomllib.loads((ROOT / "examples" / "config.toml").read_text(encoding="utf-8"))
        cfg = config.Config()
        config._merge(cfg, data)
        self.assertEqual(cfg, config.Config())

    def test_ecc_pin_is_a_full_sha(self):
        text = (ROOT / "scripts" / "fetch_ecc_corpus.py").read_text(encoding="utf-8")
        self.assertRegex(text, r'COMMIT = "[0-9a-f]{40}"')


if __name__ == "__main__":
    unittest.main()
