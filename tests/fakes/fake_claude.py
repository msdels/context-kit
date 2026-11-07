"""Fake `claude` for tests: logs argv, cwd and env to FAKE_LOG; FAKE_CLAUDE_MODE picks what it does.

Modes:
- `ok` (the default): exit 0. Run as a headless audit (CONTEXT_KIT_HEADLESS=1), it also writes a
  ledger holding a non-ASCII dash (covered_until FAKE_COVERED) and an owner item that undoes the
  eviction of FAKE_EVICT through the kit's CLI, commits through
  `context_upkeep.py commit`, and prints a JSON result costing 1.5, as UTF-8 bytes
- `fail`: exit 1
- `showfile`: logs `git show HEAD:<FAKE_SHOW_PATH>` and whether `stray.txt` survived from the
  last run, then dirties the tree and prints a JSON result with a cost
- `nothing`: prints a JSON result costing 0.25 and changes nothing
- `contract`: an `ok` audit that also edits the operating contract
- `nocost`: an `ok` audit whose JSON result carries no cost
- `quickexit`: exits 0 at once with no output
- `hang`: starts a grandchild that sleeps (its pid goes to the log), then sleeps itself
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE") or "ok"
entry = {"tool": "claude", "argv": sys.argv[1:], "cwd": os.getcwd(),
         "headless": os.environ.get("CONTEXT_KIT_HEADLESS"),
         "claudecode": os.environ.get("CLAUDECODE"),
         "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
         "project_dir": os.environ.get("CLAUDE_PROJECT_DIR")}
LEDGER = "docs/references/session-audits/2026-11-01-0900.md"
CONTRACT = "docs/context/operating-contract.md"


def run(*argv):
    done = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL)
    if done.returncode:
        sys.stderr.write(done.stderr)
    return done


def audit() -> None:
    """What a headless audit leaves behind: the ledger (with an owner item undoing the eviction),
    one eviction, one kit commit. FAKE_COVERED sets the ledger's covered_until."""
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    body = "# Audit \u2014 one eviction\n"
    if os.environ.get("FAKE_EVICT"):
        upkeep = os.path.join(".claude", "hooks", "context_upkeep.py")
        done = run(sys.executable, upkeep, "move", os.environ["FAKE_EVICT"], "overflow", "evicted")
        try:
            moved = json.loads(done.stdout)["id"]
            body += ('\n## Owner decisions\n- [ ] 1. evicted "' + os.environ["FAKE_EVICT"]
                     + '" (applied, default yes) \u00b7 undo: `python .claude/hooks/context_upkeep.py '
                     + "restore " + moved + " --owner`\n")
        except (ValueError, KeyError):
            pass
    covered = os.environ.get("FAKE_COVERED") or "2026-11-01T09:00:00Z"
    with open(LEDGER, "w", encoding="utf-8", newline="\n") as f:
        f.write("---\ncovered_until: " + covered + "\nmodels: opus\n---\n" + body)
    paths = [LEDGER]
    if mode == "contract":
        os.makedirs(os.path.dirname(CONTRACT), exist_ok=True)
        with open(CONTRACT, "a", encoding="utf-8") as f:
            f.write("- the agent may now push\n")
    paths += ["docs", os.path.join(".claude", "rules")]  # by name, as the skill says: never -A
    run(sys.executable, os.path.join(".claude", "hooks", "context_upkeep.py"), "commit", "-m",
        "context: audit 2026-11-01-0900 [context-kit]", *paths)


if mode == "showfile":
    shown = subprocess.run(["git", "show", "HEAD:" + os.environ["FAKE_SHOW_PATH"]],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL)
    entry["head_file"] = shown.stdout
    entry["stray_before"] = os.path.exists("stray.txt")
    with open("stray.txt", "w", encoding="utf-8") as f:
        f.write("left by a run\n")
    with open(os.environ["FAKE_SHOW_PATH"], "a", encoding="utf-8") as f:
        f.write("- edited by a run\n")
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             stdin=subprocess.DEVNULL)
    entry["grandchild"] = child.pid
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
if mode == "hang":
    time.sleep(120)
headless = os.environ.get("CONTEXT_KIT_HEADLESS") == "1"
if headless and mode in ("ok", "contract", "nocost"):
    audit()
result = {"showfile": {"total_cost_usd": 0.5, "result": "done"},
          "nothing": {"total_cost_usd": 0.25, "result": "nothing to do"},
          "nocost": {"result": "done \u2014 no cost"}}.get(mode)
if headless and mode in ("ok", "contract"):
    result = {"total_cost_usd": 1.5, "result": "audit done \u2014 1 eviction"}
if result:  # claude writes UTF-8 whatever the console's code page
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
sys.exit(1 if mode == "fail" else 0)
