#!/usr/bin/env python3
"""Engine `bin` paths: a leading ~ expands to the user's home; bare names stay bare."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import load_engines  # noqa: E402

ARGS = ["run", "{spec}"]


class EngineBinTildeTests(unittest.TestCase):
    def bin_for(self, bin_value: str, home: str) -> str:
        env = {"HOME": home, "USERPROFILE": home}
        with unittest.mock.patch.dict(os.environ, env):
            engines = load_engines({"tilde": {"bin": bin_value, "args_template": ARGS}})
        return engines["tilde"].bin

    def test_tilde_backslash_expands_to_home(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            got = self.bin_for("~\\x\\y.exe", home)
            self.assertEqual(str(Path(home) / "x" / "y.exe"), str(Path(got)))

    def test_tilde_forward_slash_expands_to_home(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            got = self.bin_for("~/x/y", home)
            self.assertTrue(got.startswith(home))
            self.assertFalse(got.startswith("~"))

    def test_bare_name_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual("cline", self.bin_for("cline", home))

    def test_absolute_path_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            absolute = str(Path(home) / "bin" / "tool.exe")
            self.assertEqual(absolute, self.bin_for(absolute, home))

    def test_built_in_default_bin_stays_bare(self) -> None:
        self.assertEqual("codex", load_engines(None)["codex"].bin)


if __name__ == "__main__":
    unittest.main(verbosity=2)
