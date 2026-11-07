"""Opt-in probe of the real headless path: the real `claude`, haiku, $0.05. Not part of the suite.

    CONTEXT_KIT_PROBE=1 python tests/probe_headless.py

Builds a temp repo with the kit installed and a STUB auditing-sessions skill (read one file,
write a ledger, commit it through the kit), then runs `upkeep_audit.launch` as the helper would.
Unlike the unit tests it does NOT import `_util`: that swaps HOME and the claude tool for fakes.
It prints what the unit tests cannot know: claude's JSON, the permission denials (a Read or Write
that `dontAsk` refused shows up there) and the tail of audit.log.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "hooks"))
os.environ.pop("CONTEXT_KIT_CLAUDE", None)  # the real claude, for this probe only
for _k in ("CLAUDECODE", "CLAUDE_PROJECT_DIR", "CONTEXT_KIT_HEADLESS"):
    os.environ.pop(_k, None)

import _kit  # noqa: E402
import upkeep_audit as ua  # noqa: E402

BUDGET = 0.05
ROOT_MD = "# Project\n\n| Topic | Read first |\n|---|---|\n| api | `.claude/rules/api.md` |\n"
RULES = "# API\n\n## Errors\n\n" + "".join(f"- api rule number {i} says something\n" for i in range(10))
SKILL = """---
name: auditing-sessions
description: Probe stub of the session audit. Use when asked to run /auditing-sessions.
---

# Auditing sessions (probe stub)

Do exactly these three steps, in order, and nothing else:

1. Use the Read tool on `README.md`.
2. Use the Write tool to create `docs/references/session-audits/{name}.md` with exactly:

```
---
covered_until: {covered}
---
probe
```

3. Run this one command with the Bash tool:
   `python .claude/hooks/context_upkeep.py commit -m "context: audit {name} [context-kit]" docs/references/session-audits/{name}.md`

Then stop.
"""


def git(repo, *args):
    done = _kit.git(repo, *args)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def build(tmp: Path, now: datetime) -> tuple[Path, str]:
    repo = (tmp / "probe repo").resolve()  # a space in the path, as the unit tests have
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Probe")
    git(repo, "config", "user.email", "probe@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.hooksPath", str(tmp / "nohooks"))
    git(repo, "config", "contextkit.autoAudit", "true")
    name = now.strftime("%Y-%m-%d-%H%M")
    covered = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    files = {
        "README.md": "# probe\n", "CLAUDE.md": ROOT_MD, ".claude/rules/api.md": RULES,
        "docs/references/session-audits/2020-01-01-0000.md":
            "---\ncovered_until: 2020-01-01T00:00:00Z\n---\nseed\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(text.encode("utf-8"))
    done = subprocess.run([sys.executable, str(KIT / "install.py"), str(repo), "--python", "python"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    cfg_path = repo / ".claude" / "context-kit.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg.update(python_cmd="python", audit_model="haiku", audit_budget_usd=BUDGET)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (repo / ".claude" / "skills" / "auditing-sessions" / "SKILL.md").write_bytes(
        SKILL.format(name=name, covered=covered).encode("utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "kit and context")
    sessions = _kit.upkeep_dir(repo) / "sessions"  # enough owner records to be due
    sessions.mkdir(parents=True)
    for i in range(int(cfg.get("audit_every_sessions", 10)) + 1):
        end = (now - timedelta(minutes=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (sessions / f"s{i}.json").write_text(
            json.dumps({"id": f"s{i}", "ts_start": end, "ts_end": end}), encoding="utf-8")
    return repo, name


def main() -> int:
    if os.environ.get("CONTEXT_KIT_PROBE") != "1":
        print("skipped: set CONTEXT_KIT_PROBE=1 (spends up to $0.05 on the real claude)")
        return 0
    now = datetime.now(timezone.utc)
    tmp = Path(tempfile.mkdtemp(prefix="ckit-probe-")).resolve()
    try:
        repo, name = build(tmp, now)
        with mock.patch.object(ua, "WRAP_TIMEOUT_S", 600):
            ua.launch(repo, now, True)
        state, cdir = ua.load_state(repo), _kit.upkeep_dir(repo)
        out = (cdir / "run.out").read_text(encoding="utf-8", errors="replace") \
            if (cdir / "run.out").exists() else ""
        log = (cdir / "audit.log").read_text(encoding="utf-8", errors="replace") \
            if (cdir / "audit.log").exists() else ""
        print("STATE", json.dumps(state))
        print("RUN.OUT", out)
        print("AUDIT.LOG TAIL", "\n".join(log.splitlines()[-15:]))
        branch = _kit.git(repo, "log", "--format=%s", f"refs/heads/{ua.BRANCH}", "-3").stdout
        print("BRANCH LOG", branch)
        assert state["status"] == "ready", state
        assert f"context: audit {name} [context-kit]" in branch, branch
        assert state["month_usd"] <= BUDGET + 0.01, state["month_usd"]
        print("PROBE OK")
        return 0
    finally:
        _kit.git(tmp / "probe repo", "worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(ua.worktree_path(tmp / "probe repo"), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
