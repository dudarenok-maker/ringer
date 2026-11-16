#!/usr/bin/env python3
"""Engine `bin` paths: only `~`, `~/...` (and `~\\...` on Windows) expand to home."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import built_in_codex_engine, load_engines  # noqa: E402

ARGS = ["run", "{spec}"]


class EngineBinTildeTests(unittest.TestCase):
    def bin_for(self, bin_value: str, home: str) -> str:
        env = {"HOME": home, "USERPROFILE": home}
        if os.name == "nt":
            drive, tail = os.path.splitdrive(home)
            env.update({"HOMEDRIVE": drive, "HOMEPATH": tail})
        with unittest.mock.patch.dict(os.environ, env):
            engines = load_engines({"tilde": {"bin": bin_value, "args_template": ARGS}})
        return engines["tilde"].bin

    def test_tilde_backslash_form(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            got = self.bin_for("~\\x\\y.exe", home)
            if os.name == "nt":
                self.assertEqual(str(Path(home) / "x" / "y.exe"), str(Path(got)))
            else:
                # Backslash is an ordinary filename char on POSIX: not a home form.
                self.assertEqual("~\\x\\y.exe", got)

    def test_tilde_forward_slash_expands_to_home(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(
                str(Path(home) / "x" / "y"), str(Path(self.bin_for("~/x/y", home)))
            )

    def test_bare_tilde_expands_to_home(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(str(Path(home)), str(Path(self.bin_for("~", home))))

    def test_other_user_and_double_tilde_forms_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            for value in ("~otheruser\\x", "~otheruser/x", "~~\\x", "~~/x"):
                self.assertEqual(value, self.bin_for(value, home), value)

    def test_relative_path_with_dir_part_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            for value in ("./tool", "bin/tool.exe", "bin\\tool.exe"):
                self.assertEqual(value, self.bin_for(value, home), value)

    def test_bare_name_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual("cline", self.bin_for("cline", home))

    def test_absolute_path_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            absolute = str(Path(home) / "bin" / "tool.exe")
            self.assertEqual(absolute, self.bin_for(absolute, home))

    def test_inherited_bin_goes_through_loop_unchanged(self) -> None:
        engines = load_engines({"codex": {}})
        self.assertEqual(built_in_codex_engine().bin, engines["codex"].bin)

    def test_unexpandable_home_names_the_key(self) -> None:
        boom = RuntimeError("Could not determine home directory.")
        with unittest.mock.patch.object(Path, "expanduser", side_effect=boom):
            with self.assertRaises(ValueError) as ctx:
                load_engines({"tilde": {"bin": "~/x", "args_template": ARGS}})
        self.assertIn("engines.tilde.bin", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
