#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# The nudge is suppressed only for a session whose payload `cwd` is inside a
# LIVE run's recorded `workdir` (active-runs.json) - that session is the run's
# own worker. A live run elsewhere on the box (e.g. an Open Engine lane) must not
# silence an unrelated interactive session.
NUDGE_TEXT = (
    "Ringer routing check: this looks like swarm-shaped work happening inline "
    "(model call/harness/edit loop outside a live Ringer run). Load the ringer "
    "skill and route it as a manifest — a single task is a one-task manifest. "
    "If the user explicitly asked for inline work, proceed."
)

PROVIDER_RE = re.compile(
    r"(api\.anthropic\.com|api\.openai\.com|openrouter\.ai|"
    r"generativelanguage\.googleapis|/v1/chat/completions|/v1/messages)",
    re.IGNORECASE,
)
HARNESS_RE = re.compile(
    r"\b(?:node|python3?|bun|deno)\s+\S*"
    r"(?:simulat|probe|smoke|harness|persona|grader|eval)\S*"
    r"\.(?:mjs|js|ts|py)\b",
    re.IGNORECASE,
)


def ringer_home() -> Path:
    value = os.environ.get("RINGER_HOME")
    if value and value.strip():
        return Path(value).expanduser()
    return Path.home() / ".ringer"


def pid_is_alive(pid: Any, start_time: Any = None, boot_id: Any = None) -> bool:
    try:
        parsed = int(pid)
    except (TypeError, ValueError):
        return False
    if parsed <= 0:
        return False
    # ringer_pid owns the platform-correct probe: on Windows os.kill(pid, 0)
    # raises OSError (WinError 87) for a dead pid, which this hook would swallow
    # without filtering the entry out, and is a Ctrl+C to a pid sharing the console.
    # It is a tiny stdlib-only module, so importing it costs next to nothing
    # (importing ringer.py itself would cost ~300 ms per call).
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from ringer_pid import pid_is_alive as ringer_pid_is_alive

    # A recorded process start time (entry["pid_start"]) tells a reused pid
    # from the run's own process; absent or malformed means pid-only (the
    # coercion lives in ringer_pid.pid_is_alive).
    return ringer_pid_is_alive(parsed, start_time, boot_id if isinstance(boot_id, str) else None)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp_path, path)


def read_live_active_runs(home: Path) -> dict[str, dict[str, Any]]:
    path = home / "active-runs.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        return {}

    # Read-only: dead entries are filtered in memory, never written back. The
    # hook holds no _active_runs_lock, so a prune-write here could drop a run
    # registered between this read and that write; ringer's locked
    # register/unregister paths do the pruning.
    live: dict[str, dict[str, Any]] = {}
    for run_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        # Legacy entries carry neither key: both read as None (pid-only check).
        if pid_is_alive(entry.get("pid"), entry.get("pid_start"), entry.get("pid_boot")):
            live[str(run_id)] = entry
    return live


def safe_session_id(value: Any) -> str:
    text = str(value or "unknown-session")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return safe or "unknown-session"


def state_dir(home: Path) -> Path:
    return home / "nudge-state"


def marker_path(home: Path, session_id: Any, event: str) -> Path:
    key = f"{session_id}\0{event}".encode("utf-8", errors="replace")
    digest = hashlib.sha256(key).hexdigest()
    return state_dir(home) / f"{digest}.{event}.nudged"


def claim_dedupe_marker(home: Path, session_id: Any, event: str) -> bool:
    directory = state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    marker = marker_path(home, session_id, event)
    try:
        with marker.open("x", encoding="utf-8") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
            fh.write("\n")
    except FileExistsError:
        return False
    return True


def output_nudge(event_name: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": NUDGE_TEXT,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def path_is_inside(path: Any, root: Any) -> bool:
    """True when `path` is `root` or lies beneath it.

    Compared component-wise (so a work2 sibling is not inside work), with `..`
    and doubled/trailing separators normalised. A Windows-style path (drive
    letter, UNC, or backslash on either side) is compared with ntpath
    semantics: case-insensitive, a forward slash equals a backslash, drive
    letters compared too. Anything else uses posixpath. A missing/blank side
    is never inside.
    """
    if not isinstance(path, str) or not isinstance(root, str):
        return False
    path, root = path.strip(), root.strip()
    if not path or not root:
        return False
    windows = any(re.match(r"^[A-Za-z]:|^\\|\\", value) for value in (path, root))
    mod = ntpath if windows else posixpath
    norm_path = mod.normcase(mod.normpath(path))
    norm_root = mod.normcase(mod.normpath(root))
    try:
        return mod.commonpath([norm_path, norm_root]) == norm_root
    except ValueError:  # different drives, or absolute vs relative
        return False


def session_inside_live_run(payload: dict[str, Any], active_runs: dict[str, dict[str, Any]]) -> bool:
    """True when the hook's session cwd is inside a live run's recorded workdir,
    i.e. the session is that run's own worker. A run elsewhere on the box, or a
    legacy entry with no `workdir`, never silences the nudge.
    """
    cwd = payload.get("cwd")
    return any(path_is_inside(cwd, entry.get("workdir")) for entry in active_runs.values())


def should_nudge_pre_bash(payload: dict[str, Any], home: Path) -> bool:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    if "ringer.py" in command:
        return False
    if not (PROVIDER_RE.search(command) or HARNESS_RE.search(command)):
        return False

    return not session_inside_live_run(payload, read_live_active_runs(home))


def post_edit_state_path(home: Path, session_id: Any) -> Path:
    return state_dir(home) / f"{safe_session_id(session_id)}.json"


def load_post_edit_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"count": 0, "file_paths": []}
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        return {"count": 0, "file_paths": []}
    count = raw.get("count")
    file_paths = raw.get("file_paths")
    if not isinstance(count, int):
        count = 0
    if not isinstance(file_paths, list):
        file_paths = []
    return {"count": count, "file_paths": [str(path) for path in file_paths]}


def record_post_edit(payload: dict[str, Any], home: Path) -> tuple[int, int]:
    path = post_edit_state_path(home, payload.get("session_id"))
    state = load_post_edit_state(path)
    count = int(state["count"]) + 1
    files = set(str(item) for item in state["file_paths"])

    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and file_path.strip():
            files.add(file_path)

    next_state = {"count": count, "file_paths": sorted(files)}
    write_json_atomic(path, next_state)
    return count, len(files)


def should_nudge_post_edit(payload: dict[str, Any], home: Path) -> bool:
    count, distinct_files = record_post_edit(payload, home)
    if count < 8 or distinct_files < 3:
        return False
    return not session_inside_live_run(payload, read_live_active_runs(home))


def load_stdin_payload() -> dict[str, Any] | None:
    text = sys.stdin.read()
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else None


def run(argv: list[str]) -> int:
    if len(argv) != 2:
        return 0
    mode = argv[1]
    if mode not in {"pre-bash", "post-edit"}:
        return 0

    payload = load_stdin_payload()
    if payload is None:
        return 0

    home = ringer_home()
    session_id = payload.get("session_id")

    if mode == "pre-bash":
        if should_nudge_pre_bash(payload, home) and claim_dedupe_marker(home, session_id, mode):
            output_nudge("PreToolUse")
        return 0

    if should_nudge_post_edit(payload, home) and claim_dedupe_marker(home, session_id, mode):
        output_nudge("PostToolUse")
    return 0


def main() -> int:
    try:
        return run(sys.argv)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
