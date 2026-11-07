"""Copy the context kit into a repo, then hand the judgment work to Claude.

    python install.py <path-to-repo>        # default: the current directory

Mechanical only: files and a settings merge. It writes NOTHING about the project itself: the
scan, the interview and the CLAUDE.md files are Claude's job, driven by
`.claude/context-kit/INSTALL.md`, which this prints as the next step.

Everything it writes is repo-level and meant to be committed, so a teammate who clones gets the
whole system. Nothing goes in `~/.claude`, nothing in `settings.local.json`.

Re-runnable. The upgrade policy: `hooks/*.py` and `skills/**` are kit code and always replaced;
`context-kit.json` is the repo's and never replaced; templates and the other hook files are
copied only when absent (--force overwrites those). The hook entries the kit owns in
settings.json are replaced, everyone else's kept. A kit file that a newer kit no longer ships
is left on disk; its settings entry is removed.
"""
from __future__ import annotations

import argparse
import json
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
HOOK_DIR = "${CLAUDE_PROJECT_DIR}/.claude/hooks/"
# What an older kit installed and this one no longer ships: an upgrade unhooks them from
# settings.json, but deleting the repo's files is the owner's call, so it only names them.
RETIRED = (".claude/hooks/stop_guard.py", ".claude/hooks/pretooluse_guard.py",
           ".claude/hooks/contract_reminder.py", ".claude/hooks/session_guard.py",
           ".claude/hooks/setup_clone.py", ".claude/hooks/pre-push",
           ".claude/skills/ablating-a-context-line", ".claude/skills/prove-the-check-can-fail")


def _write(path: Path, text: str) -> None:
    """LF bytes on every OS. Not write_text(newline=): that keyword is 3.10+, and macOS still
    ships 3.9 as its system python."""
    path.write_bytes(text.encode("utf-8"))


def detect_python(repo: Path) -> str:
    """The command that starts Python on EVERY collaborator's machine, not just this one.

    The hooks are stdlib-only, so they never need the project's environment — only an
    interpreter. settings.json is shared, and no single bare name is right everywhere (macOS
    ships `python3` and no `python`; Windows the reverse), so a repo that already mandates a
    runner gets that runner: it is the one name the whole team is guaranteed to have."""
    if (repo / "uv.lock").exists() or (repo / ".python-version").exists() and shutil.which("uv"):
        return "uv run --no-project python"
    if platform.system() != "Windows" and shutil.which("python3"):
        return "python3"
    return "python"


def detect_main_branch(repo: Path) -> str:
    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else ""

    remote_head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if remote_head:
        return remote_head.split("/", 1)[1]
    branches = git("branch", "--format=%(refname:short)").splitlines()
    for name in ("main", "master", "trunk"):
        if name in branches:
            return name
    return ""  # never the current branch: that is where the work happens, not where PRs land


def copy_tree(src: Path, dst: Path, force: bool, python_cmd: str = "", replace=None) -> None:
    """Copy, substituting `<python_cmd>` in markdown so the skills name a command that RUNS.

    A placeholder left in a skill is a command an agent will type verbatim and watch fail.
    An existing file is kept unless `force` or `replace(path)`."""
    for path in sorted(src.rglob("*")):
        if path.is_dir() or {"__pycache__", ".ruff_cache"} & set(path.parts):
            continue
        target = dst / path.relative_to(src)
        if target.exists() and not (force or (replace and replace(path))):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if python_cmd and path.suffix == ".md":
            text = path.read_text(encoding="utf-8").replace("<python_cmd>", python_cmd)
            _write(target, text)
        else:
            shutil.copy2(path, target)


def _is_ours(hook: dict) -> bool:
    return any(HOOK_DIR in str(a) for a in hook.get("args", [])) or HOOK_DIR in hook.get("command", "")


def merge_settings(repo: Path, python_cmd: str) -> None:
    """Add the kit's hooks to .claude/settings.json and leave everyone else's alone.

    Exec form (command + args, no shell): Claude Code runs a shell-form hook under `sh` on
    macOS/Linux and under Git Bash OR PowerShell on Windows, and no one quoting style survives
    all three. Re-running replaces the entries this kit owns and appends nothing twice, and an
    entry an older kit owned under an event this one no longer uses is removed with it."""
    runner = shlex.split(python_cmd)
    template = json.loads((KIT / "settings.hooks.json").read_text(encoding="utf-8"))
    path = repo / ".claude" / "settings.json"
    settings: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    settings.setdefault("env", {}).update(template["env"])
    hooks = settings.setdefault("hooks", {})
    for event in [*hooks, *(e for e in template["hooks"] if e not in hooks)]:
        kept = []
        for group in hooks.get(event, []):
            theirs = [h for h in group.get("hooks", []) if not _is_ours(h)]
            if theirs:
                kept.append({**group, "hooks": theirs})
        for group in template["hooks"].get(event, []):
            built = []
            for h in group["hooks"]:
                extra = {k: v for k, v in h.items() if k not in ("script", "script_args")}
                built.append({"type": "command", "command": runner[0],
                              "args": [*runner[1:], HOOK_DIR + h["script"], *h.get("script_args", [])],
                              **extra})
            kept.append({**group, "hooks": built})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, json.dumps(settings, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", default=".", help="target repo (default: cwd)")
    ap.add_argument("--python", default=None,
                    help='how EVERY collaborator starts python, e.g. "uv run --no-project python" '
                         "(default: detected from the repo)")
    ap.add_argument("--force", action="store_true", help="overwrite files the kit already placed")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"{repo} is not a git repo (no .git): the hooks need one.", file=sys.stderr)
        return 1
    python_cmd = args.python or detect_python(repo)

    copy_tree(KIT / "hooks", repo / ".claude" / "hooks", args.force,
              replace=lambda p: p.suffix == ".py" or p.name == "ruff.toml")
    copy_tree(KIT / "skills", repo / ".claude" / "skills", args.force, python_cmd,
              replace=lambda p: True)
    copy_tree(KIT / "templates", repo / ".claude" / "context-kit" / "templates", args.force)
    _write(repo / ".claude" / "context-kit" / "INSTALL.md",
           (KIT / "INSTALL.md").read_text(encoding="utf-8").replace("<python_cmd>", python_cmd))

    config_path = repo / ".claude" / "context-kit.json"
    if not config_path.exists():
        cfg = json.loads((KIT / "context-kit.example.json").read_text(encoding="utf-8"))
        cfg["python_cmd"] = python_cmd
        cfg["main_branch"] = detect_main_branch(repo)
        if not cfg["main_branch"]:
            print('main_branch could not be detected: set "main_branch" in .claude/context-kit.json'
                  " (the branch PRs merge into)")
        _write(config_path, json.dumps(cfg, indent=2) + "\n")
    merge_settings(repo, python_cmd)
    retired = [rel for rel in RETIRED if (repo / rel).exists()]
    hooks_path = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()

    print(f"\nContext kit installed into {repo}")
    if retired:
        print("  left from an older kit and no longer used (delete them, or move what you want")
        print("  to keep into the repo's own hooks):")
        print("".join(f"    {rel}\n" for rel in retired), end="")
    if hooks_path.replace("\\", "/").rstrip("/") == ".claude/hooks":
        print("  core.hooksPath is .claude/hooks, set by an older kit. Once its pre-push is gone,")
        print("  run `git config --unset core.hooksPath`, or the repo's .git/hooks never run.")
    print(f"  python for hooks: {python_cmd}   (must exist under that name for every collaborator)")
    print("  everything written is repo-level: commit `.claude/` so a clone gets the system")
    print("\nNext, in Claude Code from that repo:\n")
    print("  read .claude/context-kit/INSTALL.md and set this repo up\n")
    print("That scans the repo, asks you what the code cannot tell it, and writes CLAUDE.md and")
    print("the context files. Nothing about your project has been written yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
