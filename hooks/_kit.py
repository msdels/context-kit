"""Shared helpers for the context-kit hooks: where the repo is, and what this repo configured.

Every hook in this directory reads `.claude/context-kit.json` through `config()`, so a new repo
customises the system by editing ONE json file instead of editing five python files. Missing
file, missing key and unparseable json all fall back to DEFAULTS — a hook that dies on its own
config is a hook that blocks every turn.

The rest is what the upkeep machinery shares: one way to run git (never waits on stdin, never
dies on a bad byte), where per-clone state lives, which kind of session is calling, how to find
an external tool, and two small file primitives (an atomic write, a lock file).
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The two double-encoding families as raw bytes: a-circumflex + euro (curly quotes / em dashes
# re-encoded) and A-tilde + A-circumflex (accented letters). Spelled as escapes on purpose —
# the literal characters here would make this file its own first finding.
MOJIBAKE = (b"\xc3\xa2\xe2\x82\xac", b"\xc3\x83\xc2")
# Windows: a console child of the detached helper (which has no console) would open a window.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

DEFAULTS: dict = {
    # git: where `accept` lands a headless audit
    "main_branch": "main",
    "work_branch": "",            # merge into this instead of main_branch, when set
    "require_pr_to_main": False,  # True: `accept` pushes the audit branch for a PR instead
    # context surface
    "contract_path": "",          # optional: one more file whose lines never move, like the root
    "root_claude_md": "CLAUDE.md",
    # glob -> max LINES. Root CLAUDE.md and the @-imported context files only.
    "size_limits": {
        "CLAUDE.md": 60,
        "docs/context/mission.md": 80,
        "docs/context/domain-model.md": 80,
        "docs/context/design-decisions.md": 100,
    },
    # module CLAUDE.md files are capped in CHARACTERS: a line cap is satisfied by longer lines.
    "claude_md_globs": ["**/CLAUDE.md"],
    "claude_md_max_chars": 5000,
    "rules_glob": ".claude/rules/*.md",
    "rules_max_chars": 6000,
    "rules_max_line": 300,
    # everything that loads before the first prompt: root CLAUDE.md + its @imports + every
    # rules file with no `paths:` frontmatter. ~6k tokens. Per-file caps alone do not bound it.
    "always_loaded_budget_chars": 24000,
    "text_hygiene": True,        # block a BOM / mojibake marker in a context file
    "python_cmd": "python",
    # context upkeep
    "audit_every_sessions": 10,
    "audit_budget_usd": 25,
    "audit_month_usd": 100,
    "audit_model": "",
}


def repo_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here.parents[1]


def config(root=None) -> dict:
    """The merged config of `root` (default: this hook's repo)."""
    cfg = dict(DEFAULTS)
    path = Path(root or repo_root()) / ".claude" / "context-kit.json"
    try:
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def git(root, *args: str, env: dict | None = None, timeout: float | None = None):
    """Run `git <args>` in `root`. stdin is closed (a prompt must fail, not hang the hook) and
    output is decoded as UTF-8 with replacement. A missing git or a timeout comes back as a
    returncode-1 result, so callers test `.returncode` and never need a try block."""
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, env=env, timeout=timeout,
            creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def git_common_dir(root) -> Path:
    """The shared .git directory — the same for every linked worktree of the clone."""
    done = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    out = done.stdout.strip()
    return Path(out).resolve() if done.returncode == 0 and out else (Path(root) / ".git").resolve()


def upkeep_dir(root) -> Path:
    """Per-clone state for the upkeep machinery: inside .git, so never committed, never synced."""
    return git_common_dir(root) / "context-kit"


def auto_audit(root) -> bool:
    """True only in the clone whose owner ran `git config contextkit.autoAudit true`. The
    setting lives in .git/config, so a teammate's clone or a CI checkout never opts in."""
    done = git(root, "config", "--get", "contextkit.autoAudit")
    return done.returncode == 0 and done.stdout.strip().lower() in ("true", "1", "yes", "on")


def session_kind(payload: dict, read_transcript: bool = True) -> str:
    """`headless` (a run the kit launched itself), `sdk` (someone's script driving Claude Code),
    or `owner` (a person at a terminal). Hooks that talk to the owner exit early otherwise.

    The environment decides first. The transcript's first `entrypoint` is the fallback, for hooks
    whose env lacks CLAUDE_CODE_ENTRYPOINT; it reads at most the first 50 records, so the
    per-prompt hooks keep it. `read_transcript=False` is for `attention`, the session-start
    hook that never opens a transcript."""
    if os.environ.get("CONTEXT_KIT_HEADLESS") == "1":
        return "headless"
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk-"):
        return "sdk"
    path = payload.get("transcript_path") if read_transcript and isinstance(payload, dict) else None
    if path:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(50), f):  # the entrypoint is on the first records
                    try:
                        entry = json.loads(line).get("entrypoint")
                    except (ValueError, AttributeError):
                        continue
                    if entry:
                        return "sdk" if str(entry).startswith("sdk-") else "owner"
        except OSError:
            pass
    return "owner"


def tool(name: str) -> list[str]:
    """argv prefix for an external tool. `CONTEXT_KIT_<NAME>=<file>.py` names a stand-in script
    (tests) and runs under this interpreter; otherwise PATH plus ~/.local/bin, where installers
    for claude put things that a hook's trimmed PATH lacks."""
    override = os.environ.get(f"CONTEXT_KIT_{name.upper()}", "")
    if override.endswith(".py"):
        return [sys.executable, override]
    search = os.environ.get("PATH", "") + os.pathsep + str(Path.home() / ".local" / "bin")
    return [shutil.which(name, path=search) or name]


def atomic_write(path, text: str) -> None:
    """UTF-8, LF, all-or-nothing: write `<name>.<pid>.tmp`, then os.replace over the target.
    Windows refuses the replace while a reader has the target open, so retry briefly."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.1)


@contextlib.contextmanager
def locked(path, wait: float = 2.0, stale: float = 30.0):
    """Mutual exclusion by file existence: an O_EXCL create (fd closed at once, so Windows can
    unlink it), removed on exit. A lock older than `stale` seconds belongs to a crashed holder
    and is broken. Raises TimeoutError after `wait` seconds."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):  # Windows: PermissionError while it is deleted
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass  # it vanished between the create and the stat, or cannot be read: wait
            if time.monotonic() > deadline:
                raise TimeoutError(f"lock held: {path}") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":  # `python _kit.py main_branch` — so shell hooks can read the config
    print(config().get(sys.argv[1], "") if len(sys.argv) > 1 else json.dumps(config(), indent=2))
