"""Test harness: importing this module cuts the process off from the real machine.

A test that reads the developer's ~/.claude, git config, or runs the real `claude` or `gh`
passes here and fails in CI (or spends money). So on import: no Claude Code env, HOME in a tmp
dir, git config reduced to a known file, and the two external tools replaced by the fakes in
tests/fakes, failing by default. A test that wants a tool to work sets FAKE_<X>_MODE.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
sys.path.insert(0, str(KIT / "hooks"))

TMP = Path(tempfile.mkdtemp(prefix="ckit-")).resolve()
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

for _k in [k for k in os.environ if k.startswith("CLAUDE_CODE_")]:
    del os.environ[_k]
for _k in ("CLAUDECODE", "CLAUDE_PROJECT_DIR", "CONTEXT_KIT_HEADLESS"):
    os.environ.pop(_k, None)

(TMP / "home").mkdir()
os.environ["HOME"] = os.environ["USERPROFILE"] = str(TMP / "home")
_gitconfig = TMP / "gitconfig"
_gitconfig.write_text(
    "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
    "[commit]\n\tgpgsign = false\n" + ("[core]\n\tautocrlf = true\n" if os.name == "nt" else ""),
    encoding="utf-8")
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_CONFIG_GLOBAL"] = str(_gitconfig)

for _x in ("claude", "gh"):
    os.environ[f"CONTEXT_KIT_{_x.upper()}"] = str(HERE / "fakes" / f"fake_{_x}.py")
    os.environ[f"FAKE_{_x.upper()}_MODE"] = "fail"
os.environ["FAKE_LOG"] = str(TMP / "fake.log")
os.environ["CONTEXT_KIT_PROJECTS"] = str(TMP / "projects")

import _kit  # noqa: E402  (after sys.path and the environment are set)


def make_repo(tmp: Path, name: str = "sample repo", install: bool = False) -> Path:
    """A git repo with one commit, in a directory whose name has a space. Returns the resolved
    path. `install=True` runs install.py into it, so the hooks run from .claude/hooks as shipped."""
    repo = (Path(tmp) / name).resolve()
    repo.mkdir(parents=True)
    _kit.git(repo, "init", "-q")
    write(repo, "README.md", "# trial\n")
    commit(repo, "init")
    if install:
        import subprocess
        done = subprocess.run(
            [sys.executable, str(KIT / "install.py"), str(repo), "--python", "python"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        assert done.returncode == 0, done.stderr
    return repo


def write(repo: Path, rel: str, text: str, crlf: bool = False) -> Path:
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\r\n", "\n")
    path.write_bytes((data.replace("\n", "\r\n") if crlf else data).encode("utf-8"))
    return path


def commit(repo: Path, msg: str) -> str:
    """Stage everything, commit (empty allowed), return the new HEAD."""
    _kit.git(repo, "add", "-A", "--", ".")
    done = _kit.git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    assert done.returncode == 0, done.stderr
    return _kit.git(repo, "rev-parse", "HEAD").stdout.strip()


def project_slug(path: Path) -> str:
    """How Claude Code names a project's transcript dir: every non-alphanumeric becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def transcript(repo: Path, sid: str, turns, entrypoint: str = "cli",
               model: str = "claude-opus-4-5", files=(), cwd=None) -> Path:
    """Write <CONTEXT_KIT_PROJECTS>/<slug>/<sid>.jsonl. `turns` is a list of (role, text); each
    gets its own timestamp, one minute apart, ending now. `files` become Edit tool_use blocks in
    one assistant record at the end."""
    cwd = str(cwd or repo)
    path = Path(os.environ["CONTEXT_KIT_PROJECTS"]) / project_slug(repo) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [(role, text) for role, text in turns]
    if files:
        records.append(("assistant", [{"type": "tool_use", "name": "Edit", "id": f"t{i}",
                                       "input": {"file_path": str(f)}}
                                      for i, f in enumerate(files)]))
    now = datetime.now(timezone.utc)
    lines = []
    for i, (role, body) in enumerate(records):
        ts = now - timedelta(minutes=len(records) - i)
        message = {"role": role, "content": (
            body if role == "user" and isinstance(body, str)
            else [{"type": "text", "text": body}] if isinstance(body, str) else body)}
        if role == "assistant":
            message["model"] = model
        lines.append(json.dumps({
            "type": role, "message": message, "sessionId": sid, "cwd": cwd,
            "entrypoint": entrypoint, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
