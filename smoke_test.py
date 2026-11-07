"""End-to-end check: install into a throwaway repo, then prove the context check actually fires.

    python smoke_test.py

Asserts only what would break silently: the install lands the files and merges settings without
eating anyone else's, the capture hook stays silent outside the owner's clone, and the context
check FAILS on a repo with no CLAUDE.md, a dead route, a broken import and an over-budget launch
set, and PASSES once those are fixed. A check nobody has seen refuse is a claim, not a gate.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parent
PY = sys.executable


def run(*args: str, cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    # utf-8 explicitly: the hooks reconfigure their stdout to utf-8, and a Windows parent
    # would otherwise decode a non-ASCII character as cp1252 mojibake and an assertion would miss.
    return subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        run("git", "init", "-q", cwd=repo)
        run("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)  # whatever init.defaultBranch
        (repo / "README.md").write_text("# throwaway\n", encoding="utf-8")
        run("git", "add", "README.md", cwd=repo)
        run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=repo)

        foreign = {"type": "command", "command": "echo", "args": ["theirs"]}
        # what an older kit left: hooks on events this one no longer uses
        retired = {"type": "command", "command": "python",
                   "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/stop_guard.py"]}
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"deny": ["Bash(rm *)"]},
                        "hooks": {"SessionStart": [{"hooks": [foreign]}],
                                  "Stop": [{"hooks": [retired, foreign]}],
                                  "PreToolUse": [{"matcher": "Bash", "hooks": [retired]}]}}),
            encoding="utf-8")

        for _ in range(2):  # twice: a re-run must replace its own entries, never stack them
            done = run(PY, str(KIT / "install.py"), str(repo), "--python", "kitpy -X", cwd=KIT)
            assert done.returncode == 0, done.stderr
        for rel in (".claude/hooks/context_check.py", ".claude/hooks/context_upkeep.py",
                    ".claude/hooks/.gitignore", ".claude/context-kit.json",
                    ".claude/settings.json", ".claude/skills/context-sync/SKILL.md",
                    ".claude/skills/auditing-sessions/SKILL.md",
                    ".claude/context-kit/templates/CLAUDE.md.tmpl"):
            assert (repo / rel).exists(), f"install did not place {rel}"
        assert not list((repo / ".claude" / "hooks").rglob("__pycache__")), "copied a cache"
        # A placeholder left in a skill is a command an agent types verbatim and watches fail.
        for md in (repo / ".claude" / "skills").rglob("*.md"):
            assert "<python_cmd>" not in md.read_text(encoding="utf-8"), md
        installed = (repo / ".claude" / "context-kit" / "INSTALL.md").read_text(encoding="utf-8")
        assert "<python_cmd>" not in installed and "kitpy -X .claude/hooks" in installed

        settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert settings["permissions"]["deny"] == ["Bash(rm *)"], "merge ate a foreign key"
        starts = [h for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
        assert foreign in starts, "merge ate a foreign hook"
        ours = [h for h in starts if h != foreign]
        # Exec form: no shell, so it runs the same under sh, Git Bash and PowerShell.
        assert len(ours) == 1 and ours[0]["command"] == "kitpy" and ours[0]["args"] == [
            "-X", "${CLAUDE_PROJECT_DIR}/.claude/hooks/context_upkeep.py", "attention"], ours
        # the older kit's entries are gone; an event left empty goes with them
        assert settings["hooks"]["Stop"] == [{"hooks": [foreign]}], settings["hooks"]
        assert set(settings["hooks"]) == {"SessionStart", "Stop"}, settings["hooks"]
        # Not the owner's clone (no contextkit.autoAudit): silent, writes nothing, exits 0.
        quiet = run(PY, str(repo / ".claude" / "hooks" / "context_upkeep.py"), "attention",
                    cwd=repo, stdin="{}")
        assert quiet.returncode == 0 and quiet.stdout == "", quiet
        assert not (repo / ".git" / "context-kit").exists()
        cfg = json.loads((repo / ".claude" / "context-kit.json").read_text(encoding="utf-8"))
        assert cfg["main_branch"] == "main" and cfg["python_cmd"] == "kitpy -X", cfg

        check = repo / ".claude" / "hooks" / "context_check.py"
        red = run(PY, str(check), cwd=repo)
        assert red.returncode == 1 and "CLAUDE.md not found" in red.stdout, red.stdout

        routing = ("# throwaway\n\n## Context Routing\n\n"
                   "| Working on... | Read first... |\n|---|---|\n| Anything | README.md |\n")
        (repo / "CLAUDE.md").write_text(routing, encoding="utf-8")
        run("git", "add", "CLAUDE.md", cwd=repo)
        green = run(PY, str(check), cwd=repo)
        assert green.returncode == 0, green.stdout

        (repo / "CLAUDE.md").write_text("# x\n\n| Working on... | Read first... |\n|---|---|\n"
                                        "| Anything | docs/gone.md |\n", encoding="utf-8")
        dead = run(PY, str(check), cwd=repo)
        assert dead.returncode == 1 and "missing 'docs/gone.md'" in dead.stdout, dead.stdout

        (repo / "CLAUDE.md").write_text(routing + "\n@docs/gone.md\n", encoding="utf-8")
        broken = run(PY, str(check), cwd=repo)
        assert broken.returncode == 1 and "@docs/gone.md resolves to no file" in broken.stdout, \
            broken.stdout

        # A path inside a code span or a fenced block is literal text, not an import: the same
        # rule Claude Code's own parser uses. The fenced case is the one a lookbehind cannot see.
        (repo / "CLAUDE.md").write_text(
            routing + "\nmention `@docs/gone.md` only\n\n```\n@docs/gone.md\n```\n",
            encoding="utf-8")
        spanned = run(PY, str(check), cwd=repo)
        assert spanned.returncode == 0, spanned.stdout
        assert "load before every prompt" in spanned.stdout, spanned.stdout

        rules = repo / ".claude" / "rules"
        rules.mkdir(parents=True, exist_ok=True)
        (rules / "fat.md").write_text("# fat\n" + "x" * 30000, encoding="utf-8")
        over = run(PY, str(check), cwd=repo)
        assert "FAIL — Imports and launch budget" in over.stdout, over.stdout
        (rules / "fat.md").write_text('---\npaths:\n  - "src/**"\n---\n# fat\n' + "x" * 30000,
                                      encoding="utf-8")
        scoped = run(PY, str(check), cwd=repo)
        # `paths:` frontmatter takes it out of the launch budget; the per-file cap still bites.
        assert "FAIL — Imports and launch budget" not in scoped.stdout, scoped.stdout
        assert scoped.returncode == 1 and "chars > " in scoped.stdout, scoped.stdout

    print("smoke test: install (idempotent, non-clobbering), capture hook, context check: all OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
