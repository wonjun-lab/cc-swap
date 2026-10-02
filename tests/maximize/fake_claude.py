"""A fake ``claude`` executable for primer tests (POSIX shebang script).

Every run appends one JSON line to ``calls.jsonl`` next to the script —
argv, the full environment, cwd, its pid, and whether stdin is /dev/null —
and then behaves as ``behavior.json`` (also next to the script) says: exit
code, stdout, stderr, a sleep (timeout tests), and whether to write a
``.credentials.json`` into ``$CLAUDE_CONFIG_DIR`` (cleanup tests).
``orphanSleep`` first starts a helper in its own session that inherits
stdout/stderr and sleeps that long — a grandchild a process-group kill
cannot reach, still holding the pipes (its pid is recorded as
``orphanPid``). ``behavior.json`` may override any of these per ``--model``
value.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

FAKE_CLAUDE_BODY = r'''
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def stdin_is_devnull():
    try:
        return os.path.samestat(os.fstat(0), os.stat(os.devnull))
    except OSError:
        return False


def main():
    argv = sys.argv[1:]
    model = argv[argv.index("--model") + 1] if "--model" in argv else ""
    try:
        with open(os.path.join(HERE, "behavior.json"), encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    behavior = dict(config.get("default", {}))
    behavior.update(config.get("byModel", {}).get(model, {}))
    record = {
        "argv": argv,
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "stdinIsDevnull": stdin_is_devnull(),
    }
    if behavior.get("orphanSleep"):
        orphan = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; time.sleep(float(sys.argv[1]))",
             str(behavior["orphanSleep"])],
            start_new_session=True,
        )
        record["orphanPid"] = orphan.pid
    with open(os.path.join(HERE, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if behavior.get("writeCredentials") and config_dir:
        with open(os.path.join(config_dir, ".credentials.json"), "w", encoding="utf-8") as f:
            json.dump({"claudeAiOauth": {"accessToken": "written-by-fake"}}, f)
    time.sleep(float(behavior.get("sleep", 0)))
    default_out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "OK"})
    sys.stdout.write(behavior.get("stdout", default_out))
    sys.stderr.write(behavior.get("stderr", ""))
    return int(behavior.get("exitCode", 0))


sys.exit(main())
'''


class FakeClaude:
    def __init__(self, directory: Path):
        self.dir = directory
        self.path = directory / "claude"

    @classmethod
    def install(cls, directory: Path) -> "FakeClaude":
        directory.mkdir(parents=True, exist_ok=True)
        fake = cls(directory)
        fake.path.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE_BODY}", encoding="utf-8")
        fake.path.chmod(0o755)
        return fake

    def behave(self, default: dict | None = None, by_model: dict | None = None) -> None:
        (self.dir / "behavior.json").write_text(
            json.dumps({"default": default or {}, "byModel": by_model or {}}),
            encoding="utf-8",
        )

    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def kill_orphans(self) -> None:
        """Test cleanup: SIGKILL every ``orphanSleep`` helper still running."""
        for call in self.calls():
            pid = call.get("orphanPid")
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` still exists (a reaped child no longer does)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
