#!/usr/bin/env python3
"""Engine `bin` paths: a relative path with a directory part is anchored to Ringer's cwd."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ringer import (  # noqa: E402
    build_worker_command,
    load_engines,
    preflight_engine_bins,
)

ARGS = ["run", "{spec}"]


class EngineBinRelativeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Registered after tmp.cleanup so it runs first (LIFO): Windows cannot
        # remove a directory that is still the process cwd.
        self.addCleanup(os.chdir, os.getcwd())
        # realpath: abspath does not resolve symlinks (macOS /var -> /private/var).
        self.root = Path(os.path.realpath(tmp.name))
        os.chdir(self.root)

    def bin_for(self, bin_value: str) -> str:
        engines = load_engines({"rel": {"bin": bin_value, "args_template": ARGS}})
        return engines["rel"].bin

    def test_dot_slash_anchors_to_cwd(self) -> None:
        self.assertEqual(str(self.root / "tool"), str(Path(self.bin_for("./tool"))))

    def test_dot_backslash_anchors_to_cwd_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("backslash is an ordinary filename char on POSIX")
        self.assertEqual(str(self.root / "tool"), str(Path(self.bin_for(".\\tool"))))

    def test_subdir_path_anchors_to_cwd(self) -> None:
        got = self.bin_for("sub/tool")
        self.assertTrue(os.path.isabs(got), got)
        self.assertEqual(str(self.root / "sub" / "tool"), str(Path(got)))

    def test_bare_name_is_untouched(self) -> None:
        self.assertEqual("tool", self.bin_for("tool"))

    def test_absolute_path_is_untouched(self) -> None:
        absolute = str(self.root / "bin" / "tool")
        self.assertEqual(absolute, self.bin_for(absolute))

    def test_tilde_still_expands_to_home(self) -> None:
        home = str(self.root / "home")
        env = {"HOME": home, "USERPROFILE": home}
        if os.name == "nt":
            drive, tail = os.path.splitdrive(home)
            env.update({"HOMEDRIVE": drive, "HOMEPATH": tail})
        with unittest.mock.patch.dict(os.environ, env):
            got = self.bin_for("~/x")
        self.assertEqual(str(Path(home) / "x"), str(Path(got)))

    def test_loaded_bin_survives_a_later_chdir(self) -> None:
        tool_dir = self.root / "a"
        tool_dir.mkdir()
        other = self.root / "b"
        other.mkdir()
        tool = tool_dir / "tool"
        tool.write_text("", encoding="utf-8")
        tool.chmod(0o755)
        os.chdir(tool_dir)
        engine = load_engines({"rel": {"bin": "./tool", "args_template": ARGS}})["rel"]
        os.chdir(other)
        self.assertEqual(str(tool), str(Path(engine.bin)))
        self.assertTrue(Path(engine.bin).is_file())
        command = build_worker_command(
            engine, taskdir=other, spec="s", full_access=False
        )
        self.assertEqual(str(tool), str(Path(command[0])))

    def preflight(self, bin_value: str) -> None:
        engines = load_engines({"rel": {"bin": bin_value, "args_template": ARGS}})
        config = SimpleNamespace(engines=engines)
        manifest = SimpleNamespace(tasks=[SimpleNamespace(engine="rel")])
        preflight_engine_bins(manifest, config)

    def test_windows_extensionless_anchored_bin_finds_exe(self) -> None:
        if os.name != "nt":
            self.skipTest("CreateProcess appends .exe on Windows only")
        (self.root / "tools").mkdir()
        (self.root / "tools" / "tool.exe").write_text("", encoding="utf-8")
        self.preflight("./tools/tool")

    def test_windows_extensionless_anchored_bin_without_exe_is_missing(self) -> None:
        if os.name != "nt":
            self.skipTest("CreateProcess appends .exe on Windows only")
        (self.root / "tools").mkdir()
        with self.assertRaises(ValueError):
            self.preflight("./tools/tool")


if __name__ == "__main__":
    unittest.main(verbosity=2)
