"""Shared test helpers. Every test isolates HOME/SKILLEVO_HOME into a temp dir; nothing touches the real user profile."""

import os
import tempfile
import unittest
from pathlib import Path


class IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)  # registered first, so it runs last (after stores are closed; matters on Windows)
        self.tmp = Path(self._tmp.name).resolve()
        self._saved = dict(os.environ)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        for k in ("SKILLEVO_DISABLED", "SKILLEVO_POLICY", "CLAUDE_PROJECT_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        os.environ["SKILLEVO_HOME"] = str(self.tmp / "data")
        os.environ["SKILLEVO_CONFIG"] = str(self.tmp / "config.toml")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.proj)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
