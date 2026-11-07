"""Stage B, the headless audit: its state, the fetch, the gate, the run and its validation, and
reaping.

The detached `_helper` calls `launch`. Under `launch.lock` (O_EXCL) it reaps a dead or stale run
and an orphan branch; then, once the audit is due and the local checks pass, it fetches outside any lock (a fetch may take
FETCH_TIMEOUT_S), and back under the lock runs `gate`, adds the worktree and marks the state
`running` before letting go, so no other helper can see an idle state beside a fresh branch.
The run itself (`run_audit`, up to WRAP_TIMEOUT_S) holds no lock.

State lives in `<git-common-dir>/context-kit/state.json` (STATE_DEFAULTS), written only under
`state.lock`. `fails` lists the reasons of the failed runs, oldest first; a `ready` run clears it,
so FAIL_STREAK equal reasons in a row means the same thing failed twice running.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import _kit
import context_upkeep as cu

FETCH_TIMEOUT_S = 30
FAIL_STREAK = 2
FAILS_KEPT = 10  # state.json keeps the reasons of the last runs only
WRAP_TIMEOUT_S = 7200  # one headless run, then its process tree is killed
REAP_GRACE_S = 600     # past WRAP_TIMEOUT_S, a `running` state whose wrapper never wrote is dead
NO_RUN_S = 30          # a run that exits this fast with no JSON never reached the model
PROMPT = "/auditing-sessions"
HOOKS = Path(__file__).resolve().parent
ONEXC = "onexc" if sys.version_info >= (3, 12) else "onerror"  # onerror warns on 3.12+
# Who launched the helper (a session's identity), never passed on: CLAUDECODE makes claude
# refuse to start. Auth (ANTHROPIC_*, CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_USE_*) is kept.
SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_SESSION_ID",
                "CLAUDE_CODE_SESSION_ID", "CLAUDE_ENV_FILE", "CLAUDE_PROJECT_DIR")
LAUNCH_STALE_S = 300  # launch.lock outlives a slow `worktree add`; a crashed holder's lock expires
BRANCH = "context/audit"
REFSPEC = "+refs/heads/context/*:refs/remotes/origin/context/*"  # a glob: fine when none exist yet
KIT_FILES = (".claude/hooks/context_upkeep.py", ".claude/skills/auditing-sessions/SKILL.md")
STATE_DEFAULTS = {"status": "idle", "reason": "", "fails": [], "pid": 0, "started": "",
                  "base": "", "ledger": "", "month": "", "month_usd": 0.0,
                  "summary": {}, "covered": ""}  # covered: a watermark `accept`/`dismiss` moved


def load_state(repo, cdir=None) -> dict:
    state = copy.deepcopy(STATE_DEFAULTS)
    state.update(cu._load((cdir or _kit.upkeep_dir(repo)) / "state.json") or {})
    return state


def update_state(repo, *, expect: dict | None = None, **changes) -> bool:
    """Apply `changes` to state.json, unless a key of `expect` no longer holds (a newer run, or
    reap, owns the state now): then write nothing and return False."""
    cdir = _kit.upkeep_dir(repo)
    with _kit.locked(cdir / "state.lock"):
        state = load_state(repo)
        if any(state.get(k) != v for k, v in (expect or {}).items()):
            return False
        state.update(changes)
        _kit.atomic_write(cdir / "state.json", json.dumps(state) + "\n")
    return True


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill `proc` and what it started (git's ssh or credential helper), then reap it. POSIX
    needs `proc` started with start_new_session=True."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=_kit.NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _git_logged(repo, *args: str) -> bool:
    """A network git call: no stdin, no prompt, output to audit.log rather than a pipe (a hung
    grandchild holding a pipe would hang the read), tree-killed at FETCH_TIMEOUT_S."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cu._log(repo, f"{stamp} git {' '.join(args)}")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        with open(_kit.upkeep_dir(repo) / "audit.log", "a", encoding="utf-8") as out:
            proc = subprocess.Popen(["git", *args], cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=os.name != "nt",
                                    creationflags=_kit.NO_WINDOW)
            try:
                return proc.wait(timeout=FETCH_TIMEOUT_S) == 0
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                out.write(f"killed after {FETCH_TIMEOUT_S}s\n")
                return False
    except OSError:
        return False


def fetch(repo, main) -> bool:
    """Fetch `main` and every context/* branch, pruning ones the remote deleted. False without
    an origin remote, or on any failure: the caller then uses local refs."""
    if _kit.git(repo, "remote", "get-url", "origin").returncode != 0:
        return False
    return _git_logged(repo, "fetch", "--prune", "origin", main, REFSPEC)


def _exists(repo, ref: str) -> bool:
    return _kit.git(repo, "rev-parse", "--verify", "-q", ref).returncode == 0


def base_ref(repo, cfg: dict, fetched: bool) -> str:
    """`work_branch` when set, else `origin/<main>` when the fetch worked, else local `<main>`."""
    main = cfg.get("main_branch") or "main"
    if cfg.get("work_branch"):
        return cfg["work_branch"]
    return f"origin/{main}" if fetched and _exists(repo, f"refs/remotes/origin/{main}") else main


def remote_audit(repo, base) -> str:
    """`none` without origin/context/audit; `merged` when its newest ledger is on origin/<base>
    (the remote branch is then deleted, best-effort); otherwise `unmerged`."""
    tip = f"refs/remotes/origin/{BRANCH}"
    if not _exists(repo, tip):
        return "none"
    listing = _kit.git(repo, "ls-tree", "--name-only", f"{tip}:{cu._LEDGER_DIR}/")
    names = sorted(n for n in listing.stdout.splitlines() if cu._LEDGER_NAME.match(n))
    if not names or _kit.git(repo, "cat-file", "-e", f"refs/remotes/origin/{base}:"
                             f"{cu._LEDGER_DIR}/{names[-1]}").returncode != 0:
        return "unmerged"
    # --no-verify: a deletion pushes no content, so the repo's pre-push hook, if any, has
    # nothing to check and must not run unattended in the owner's tree
    _git_logged(repo, "push", "--no-verify", "origin", "--delete", BRANCH)  # a failure retries
    return "merged"


def kit_on_base(repo, base: str) -> bool:
    """The run starts from `base`, so the hooks and the skill it calls must be committed there."""
    return all(_kit.git(repo, "cat-file", "-e", f"{base}:{rel}").returncode == 0
               for rel in KIT_FILES)


def month_ok(state: dict, cfg: dict, now: datetime) -> bool:
    """This month's spend plus a whole next run stays within the monthly cap."""
    spent = state.get("month_usd", 0) if state.get("month") == now.strftime("%Y-%m") else 0
    return spent + float(cfg.get("audit_budget_usd", 25)) <= float(cfg.get("audit_month_usd", 100))


def streak_stopped(state: dict) -> bool:
    last = state.get("fails", [])[-FAIL_STREAK:]
    return len(last) == FAIL_STREAK and len(set(last)) == 1


def _local_why(repo, cfg: dict, now: datetime) -> str:
    """The gate's conditions that need no network and no base ref; `launch` asks them before
    it fetches, so a waiting state makes no network call."""
    state = load_state(repo)
    if not _kit.auto_audit(repo):
        return "contextkit.autoAudit is off"
    if state["status"] not in ("idle", "failed"):  # a failed run cleaned up after itself
        return f"status is {state['status']}"
    if _exists(repo, f"refs/heads/{BRANCH}"):
        return f"a local {BRANCH} exists"
    if streak_stopped(state):
        return f"the last {FAIL_STREAK} runs failed: {state['fails'][-1]}"
    if not month_ok(state, cfg, now):
        return "the monthly cap would be passed"
    if not cu.due(repo, cfg, now)[0]:
        return "not due"
    return ""


def _why_not(repo, cfg: dict, now: datetime, base: str) -> str:
    why = _local_why(repo, cfg, now)
    if why:
        return why
    if not kit_on_base(repo, base):
        return f"the kit is not committed on {base}"
    if remote_audit(repo, cfg.get("main_branch") or "main") == "unmerged":
        return f"origin/{BRANCH} is not merged"
    return ""


def gate(repo, cfg: dict, now: datetime, fetched: bool = False) -> tuple[bool, str, str]:
    """(launch, why not, base). The caller holds launch.lock; `fetched` says origin is fresh."""
    base = base_ref(repo, cfg, fetched)
    why = _why_not(repo, cfg, now, base)
    return not why, why, base




# --- the worktree ----------------------------------------------------------------------------

def worktree_path(repo) -> Path:
    """Where a new audit worktree goes: beside the repo, outside .git. Only `prepare_worktree`
    uses it; everything after asks git (`audit_worktree`)."""
    repo = Path(repo).resolve()
    return repo.parent / f".context-kit-wt-{repo.name}"


def _branch_worktree(repo) -> Path | None:
    """Whichever worktree has context/audit checked out, from `git worktree list --porcelain`:
    the first entry is the main checkout, so it is skipped (it is never the kit's)."""
    path, first = None, True
    for line in _kit.git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            if first:
                path, first = None, False
        elif line == f"branch refs/heads/{BRANCH}" and path:
            return Path(path).resolve()
    return None


def audit_worktree(repo) -> Path | None:
    """The kit's own audit worktree (`worktree_path`) when it has context/audit checked out.
    One the owner made, or the main checkout, is never returned: callers delete what they get."""
    found = _branch_worktree(repo)
    if found and os.path.normcase(found) == os.path.normcase(worktree_path(repo)):
        return found
    return None


def _rmtree(path: Path) -> bool:
    """Remove `path`, retrying: on Windows a just-killed process can hold a file for a moment."""
    def writable(func, p, _exc):  # read-only files (git objects) refuse deletion on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    for _ in range(5):
        if not os.path.lexists(path):
            return True
        try:
            shutil.rmtree(path, **{ONEXC: writable})
        except OSError:
            time.sleep(0.5)
    return not os.path.lexists(path)


def prepare_worktree(repo, base) -> Path | None:
    """A fresh worktree on a new context/audit branch at `base`. Whatever a crashed run left
    there goes first, registered or not. A failed add is logged and left as it is (that branch
    may hold an audit nobody has read): the next start retries."""
    repo, path = Path(repo).resolve(), worktree_path(repo)
    for old in {p for p in (audit_worktree(repo), path) if p}:
        _kit.git(repo, "worktree", "remove", "--force", str(old))
    _kit.git(repo, "worktree", "prune")
    _rmtree(path)
    _kit.git(repo, "worktree", "prune")  # a registration whose dir the rmtree took
    done = _kit.git(repo, "worktree", "add", "-q", "-b", BRANCH, str(path), base)
    if done.returncode != 0:
        cu._log(repo, f"worktree add failed: {done.stderr.strip()}")
        return None
    return audit_worktree(repo)


def cleanup(repo, keep_branch: bool) -> None:
    """Remove the audit worktree, and the branch unless it holds a run to keep."""
    wt = audit_worktree(repo)
    if wt:
        _kit.git(repo, "worktree", "remove", "--force", str(wt))
        _rmtree(wt)
    elif _branch_worktree(repo):  # the owner's checkout: their files and branch are not ours
        cu._log(repo, f"{BRANCH} is checked out in {_branch_worktree(repo)}: left alone")
        return
    _kit.git(repo, "worktree", "prune")
    if not keep_branch:
        _kit.git(repo, "branch", "-D", BRANCH)


# --- the run ---------------------------------------------------------------------------------

def clean_env(env, wt) -> dict:
    """The run's environment: no trace of the launching session, auth kept, the worktree as the
    project, headless, and no git prompt that would wait for nobody."""
    out = {k: v for k, v in env.items() if k.upper() not in SESSION_VARS}
    out.update(CLAUDE_PROJECT_DIR=str(wt), CONTEXT_KIT_HEADLESS="1", GIT_TERMINAL_PROMPT="0",
               GCM_INTERACTIVE="never")
    return out


def claude_argv(cfg: dict, budget: float, py: str) -> list[str]:
    """The headless audit's command line. It may write only the ledger by hand; every tier
    change goes through the kit's CLI, and it commits through `context_upkeep.py commit` (no
    repo hooks, so no paid review). The prompt goes last, after `--`: `--allowedTools` is
    variadic and would swallow it."""
    u = f"{py} .claude/hooks/context_upkeep.py"
    allowed = [f"Write({cu._LEDGER_DIR}/**)", f"Edit({cu._LEDGER_DIR}/**)",
               *(f"Bash(git {c} *)" for c in ("status", "diff", "log", "show")),  # read-only
               f"Bash({u} commit *)", f"Bash({py} .claude/hooks/context_check.py)",
               *(f"Bash({u} {c})" for c in ("batch", "ledger")),
               *(f"Bash({u} {c} *)" for c in cu.TIER_COMMANDS)]
    # On Windows the model may reach for the PowerShell tool instead: the same commands, allowed
    # the same way, or a run's first `batch` is refused.
    allowed += [a.replace("Bash(", "PowerShell(", 1) for a in allowed if a.startswith("Bash(")]
    disallowed = ["Bash(git push *)", "Bash(git merge *)", "Bash(git commit *)", "Bash(gh *)"]
    disallowed += [d.replace("Bash(", "PowerShell(", 1) for d in disallowed]
    model = ["--model", cfg["audit_model"]] if cfg.get("audit_model") else []
    return [*_kit.tool("claude"), "-p", "--setting-sources", "project",
            "--permission-mode", "dontAsk", "--allowedTools", *allowed,
            "--disallowedTools", *disallowed, "--max-budget-usd", str(budget), *model,
            "--output-format", "json", "--no-session-persistence", "--", PROMPT]


def pid_alive(pid: int) -> bool:
    """Is process `pid` running? Windows: OpenProcess with the least right, then exit code 259
    (STILL_ACTIVE); access denied means it exists. POSIX: signal 0."""
    if not pid or pid < 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        k.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        k.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        code = ctypes.c_ulong()
        try:
            return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(repo, base: str) -> Path | None:
    """Add the worktree at `base` and mark the state `running` for this process. The caller
    holds launch.lock, so the branch and the `running` state appear together."""
    sha = _kit.git(repo, "rev-parse", "--verify", "-q", f"{base}^{{commit}}").stdout.strip()
    wt = prepare_worktree(repo, sha) if sha else None
    if wt:
        update_state(repo, status="running", pid=os.getpid(), started=_stamp(), base=sha,
                     reason="", ledger="")
    return wt


def _result(text: str):
    """claude's JSON result: the whole output, else its last line that parses; None if none."""
    for chunk in (text, *reversed(text.splitlines())):
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _cost(data, elapsed: float, budget: float) -> float:
    """What the month is charged: the reported cost; nothing for a run that quit at once with no
    JSON (it never reached the model); otherwise the run may have spent it all."""
    try:
        return float(data["total_cost_usd"])
    except (TypeError, KeyError, ValueError):
        pass
    return 0.0 if data is None and elapsed < NO_RUN_S else budget


def _protected(wt: Path, rev: str, rel: str) -> Counter:
    """The protected lines of `rel` at `rev`, each with its indented block, CRs dropped."""
    import upkeep_tiers as ut
    text = _kit.git(wt, "show", f"{rev}:{rel}").stdout.replace("\r", "")
    lines = text.split("\n")
    return Counter("\n".join([lines[x.index], *ut._children(lines, x.index)])
                   for x in ut._parse_text(rel, text, False) if x.protected)


def _name_status(wt: Path, base: str, tip: str) -> list:
    out = [x for x in _kit.git(wt, "diff", "--name-status", "--no-renames", "-z", base, tip)
           .stdout.split("\0") if x]
    return list(zip(out[::2], out[1::2]))


def _audit_paths(wt: Path):
    """(the protected files, folded; the live files; the tier files' pattern): what an audit
    may change is the live and tier files (the kit writes only `kit-*.md` and `_gaps.md`)."""
    import upkeep_tiers as ut
    cfg = _kit.config(wt)
    tier = re.compile(rf"({re.escape(ut.OVERFLOW_DIR)}|{re.escape(ut.ARCHIVE_DIR)})/"
                      r"(kit-[^/]+|_gaps)\.md")
    return ({cu._fold(p) for p in (cfg["contract_path"], cfg["root_claude_md"]) if p},
            set(ut.live_files(wt)), tier)


def changes_why(wt: Path, base: str, tip: str, ledger: str, changes=None) -> str:
    """Why `base..tip` is not a change an audit may make, or "": only tier files and `ledger`
    change, never a protected file, no tier file is deleted, and every protected line
    stays byte-identical. Checked on the audit commit, and on the answers commit before a merge."""
    special, live, tier = _audit_paths(wt)
    for status, rel in changes if changes is not None else _name_status(wt, base, tip):
        if cu._fold(rel) in special:
            return f"{rel} changed: the protected files never change headless"
        if rel != ledger and rel not in live and not tier.fullmatch(rel):
            return f"{rel} changed: only tier files and the ledger may"
        if status == "D" and tier.fullmatch(rel):
            return f"{rel} deleted: tier files are moved into, never removed"
        if rel in live:
            gone = _protected(wt, base, rel) - _protected(wt, tip, rel)
            if gone:
                return f"{rel}: a protected line changed: {next(iter(gone))[:80]!r}"
    return ""


def validate(repo, wt, base_sha: str) -> tuple[str, str]:
    """(why the run is not ready, or ""; the ledger it added). Ready means one `[context-kit]`
    commit that adds one ledger and touches nothing but tier files and that ledger, leaves the
    protected files and every protected line as they were, holds the net-removal cap,
    and leaves `context_check` passing in the worktree."""
    import upkeep_tiers as ut  # lazy: `attention` never pays for it

    wt = Path(wt)
    tip = _kit.git(wt, "rev-parse", "HEAD").stdout.strip()
    commits = _kit.git(wt, "rev-list", f"{base_sha}..{tip}").stdout.split()
    if len(commits) != 1:
        return f"{len(commits)} commits on {BRANCH}, not one audit commit", ""
    if "[context-kit]" not in _kit.git(wt, "log", "-1", "--format=%s", tip).stdout:
        return "the audit commit's subject lacks [context-kit]", ""
    changes = _name_status(wt, base_sha, tip)
    ledgers = [p for s, p in changes if s == "A" and p.startswith(cu._LEDGER_DIR + "/")
               and cu._LEDGER_NAME.match(p[len(cu._LEDGER_DIR) + 1:])]
    if len(ledgers) != 1:
        return f"the audit commit adds {len(ledgers)} ledgers, not one", ""
    why = changes_why(wt, base_sha, tip, ledgers[0], changes)
    if why:
        return why, ""
    at_base, at_tip = len(ut.live_at(wt, base_sha)), len(ut.live_at(wt, tip))
    limit = math.ceil(ut.MOVE_CAP_PCT * at_base / 100)
    if at_base - at_tip > limit:
        return f"net-removal cap: {at_base - at_tip} removals > {limit}", ""
    done = cu._run([sys.executable, str(HOOKS / "context_check.py")], wt, 300,
                   {**os.environ, "CLAUDE_PROJECT_DIR": str(wt)})
    if done.returncode != 0:
        cu._log(repo, done.stdout + done.stderr)
        return "context_check fails in the worktree", ""
    return "", ledgers[0]


def _summary(wt: Path, base: str, ledger: str) -> dict:
    """What the prompt line says: the moves and restores the audit commit added (rows in the tier
    files; an entry moved between tiers re-adds its old rows, so this can overcount) and the
    ledger's owner items."""
    added = [x[1:] for x in _kit.git(wt, "diff", "-U0", base, "HEAD").stdout.splitlines()
             if x.startswith("+") and not x.startswith("+++")]
    count = lambda what: sum(bool(re.match(rf"- \S+ {what}", x)) for x in added)  # noqa: E731
    text = _kit.git(wt, "show", f"HEAD:{ledger}").stdout
    items = owner_items(text)
    return {"moves": count(r"moved (overflow|archive) \u00b7 "), "restores": count(r"restored \u00b7 "),
            "items": len(items),  # the defaults: keep what is applied, apply nothing by paste
            "answers": " ".join(f"{x['n']}{'y' if x['kind'] == 'undo' else 'n'}" for x in items)}


def run_audit(repo, cfg: dict) -> None:
    """Run the headless audit in the worktree `start_run` added, validate it, clean up, book the
    spend, and write the end state, but only if this run still owns the state."""
    repo = Path(repo).resolve()
    me = {"pid": os.getpid(), "status": "running"}
    state = load_state(repo)
    if any(state.get(k) != v for k, v in me.items()):
        return  # start_run did not hand this run the state
    wt, budget = audit_worktree(repo), float(cfg.get("audit_budget_usd", 25))
    cdir = _kit.upkeep_dir(repo)
    why, ledger, data, cost, proc, summary = "", "", None, 0.0, None, {}
    if wt is None:
        why = "the audit worktree is gone"
    else:
        start = time.monotonic()
        try:
            with open(cdir / "run.out", "wb") as out, open(cdir / "audit.log", "ab") as err:
                # a file, not a pipe: a grandchild holding a pipe would hang the read
                proc = subprocess.Popen(
                    claude_argv(cfg, budget, cfg.get("python_cmd") or "python"), cwd=wt,
                    env=clean_env(os.environ, wt), stdin=subprocess.DEVNULL, stdout=out,
                    stderr=err, start_new_session=os.name != "nt", creationflags=_kit.NO_WINDOW)
                try:
                    proc.wait(timeout=WRAP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    kill_tree(proc)
                    why = f"timed out after {WRAP_TIMEOUT_S}s"
        except OSError as exc:
            if proc is None:
                why = f"claude did not start: {exc}"
        if proc is not None:
            try:
                data = _result((cdir / "run.out").read_bytes().decode("utf-8", "replace"))
            except OSError:
                pass
            cost = _cost(data, time.monotonic() - start, budget)
    try:
        if not why:
            why, ledger = validate(repo, wt, state["base"])
            subtype = data.get("subtype") if isinstance(data, dict) else None
            if why and subtype not in (None, "success"):
                why = f"claude: {subtype}"
            if not why:
                summary = _summary(wt, state["base"], ledger)
    except Exception:
        why = "validate crashed"
        cu._log(repo, traceback.format_exc())
    try:
        cleanup(repo, keep_branch=not why)
    finally:
        if cost:  # booked whoever owns the state now: the money is spent either way
            cu.add_spend(repo, cost)
        cu._log(repo, f"{_stamp()} run: {why or 'ready ' + ledger} (${cost})")
        end = (dict(status="ready", reason="", fails=[], ledger=ledger, summary=summary) if not why else
               dict(status="failed", reason=why,
                    fails=[*load_state(repo)["fails"], why][-FAILS_KEPT:]))
        update_state(repo, expect=me, **end)


# --- reaping ---------------------------------------------------------------------------------

def _audit_commit(repo) -> bool:
    """Does context/audit hold an audit commit that no other local branch has?"""
    subjects = _kit.git(repo, "log", "--format=%s", f"refs/heads/{BRANCH}", "--not",
                        f"--exclude={BRANCH}", "--branches").stdout.splitlines()
    return any(s.startswith("context: audit ") and "[context-kit]" in s for s in subjects)


def reap(repo, now: datetime) -> None:
    """A `running` run whose process is dead, or that outlived WRAP_TIMEOUT_S plus the grace,
    becomes `failed` (charged the budget: it may have spent it). With no run, a context/audit
    branch holding no audit commit is cleaned up; one with an audit commit is the owner's to
    accept or dismiss: `failed`, reason `orphan branch`. The caller holds launch.lock."""
    repo = Path(repo).resolve()
    state = load_state(repo)
    if state["status"] == "running":
        started = cu._parse(state["started"])
        stale = started is None or (now - started).total_seconds() > WRAP_TIMEOUT_S + REAP_GRACE_S
        dead = not pid_alive(int(state["pid"] or 0))
        if dead or stale:
            why = "the run died" if dead else "the run outlived its timeout"
            if update_state(repo, expect={"pid": state["pid"], "status": "running"},
                            status="failed", reason=why,
                            fails=[*state["fails"], why][-FAILS_KEPT:]):
                cu.add_spend(repo, float(_kit.config(repo).get("audit_budget_usd", 25)))
                cu._log(repo, f"{_stamp()} reap: {why}")
            state = load_state(repo)
    if state["status"] not in ("idle", "failed") or not _exists(repo, f"refs/heads/{BRANCH}"):
        return
    if not _audit_commit(repo):
        cleanup(repo, keep_branch=False)
        return
    cleanup(repo, keep_branch=True)
    if state["reason"] != "orphan branch":
        update_state(repo, expect={"status": state["status"]}, status="failed",
                     reason="orphan branch")


def launch(repo, now: datetime, due: bool = True) -> None:
    """The helper's Stage B step: reap, then, once due, fetch, gate, start and run."""
    repo = Path(repo).resolve()
    cfg = _kit.config(repo)
    lock = _kit.upkeep_dir(repo) / "launch.lock"
    try:
        with _kit.locked(lock, wait=0, stale=LAUNCH_STALE_S):
            reap(repo, now)
    except TimeoutError:
        pass  # another helper is reaping; it may not be due, so this one still decides
    if not due:
        return
    try:
        why = _local_why(repo, cfg, now)
        if why:  # no network call for a state that cannot launch anyway
            cu._log(repo, f"gate: {why}")
            return
        fetched = fetch(repo, cfg.get("main_branch") or "main")
        # a short wait outlasts another helper's reap; a second due helper then sees the run
        with _kit.locked(lock, wait=5, stale=LAUNCH_STALE_S):
            ok, why, base = gate(repo, cfg, now, fetched)
            cu._log(repo, f"gate: {'launch from ' + base if ok else why}")
            wt = start_run(repo, base) if ok else None
    except TimeoutError:
        return  # another helper holds it: that one decides
    if wt:
        run_audit(repo, cfg)


# --- the owner's side: the prompt, accept and dismiss ----------------------------------------

OWNER_HEAD = "## Owner decisions"
_ITEM = re.compile(r"^- \[([ xn])\] (\d+)[.)]\s+(.*?)(?: \u00b7 (undo|apply): (.*))?$")
_COVERED = re.compile(r"^covered_until:\s*['\"]?([^\s'\"]+)", re.MULTILINE)


def owner_items(text: str) -> list[dict]:
    """The ledger's `## Owner decisions` items: {i: line index, mark: ' ' open, 'x' kept or
    applied, 'n' undone or declined, n, kind: undo|apply|'', cmd: the backticked command or ''}."""
    out, inside = [], False
    for i, line in enumerate(text.replace("\r", "").split("\n")):
        if line.startswith("## "):
            inside = line.strip() == OWNER_HEAD
        m = _ITEM.match(line) if inside else None
        if m:
            cmd = re.fullmatch(r"`([^`]+)`", (m.group(5) or "").strip())
            out.append({"i": i, "mark": m.group(1), "n": int(m.group(2)), "kind": m.group(4) or "",
                        "cmd": cmd.group(1) if cmd else "", "text": line[6:]})
    return out


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def prompt_lines(repo, cfg: dict, now: datetime, cdir=None) -> list[str]:
    """The Stage B lines for `attention`: an audit waiting for review, a failure
    that stopped the runs, or a monthly cap that holds an audit due. Commands in full. `cdir`
    spares `attention` a git call."""
    cdir = cdir or _kit.upkeep_dir(repo)
    state = load_state(repo, cdir)
    u = f"{cfg.get('python_cmd') or 'python'} .claude/hooks/context_upkeep.py"
    diff = f"git diff {state['base'] or cfg.get('main_branch') or 'main'}...{BRANCH}"
    if state["status"] == "ready":
        s = state.get("summary") or {}
        return [f"context: audit ready \u2014 {_plural(s.get('moves', 0), 'move')}, "
                f"{_plural(s.get('restores', 0), 'restore')}, {_plural(s.get('items', 0), 'item')}. "
                f"Review: {diff} \u00b7 then: {u} accept {s.get('answers', '')}".rstrip()
                + f"   (or: {u} dismiss)"]
    if state["status"] == "failed" and state["reason"] == "orphan branch":
        return [f"context: an audit is waiting on {BRANCH} (no run owns it, and accept checks it "
                f"first). Review: {diff} \u00b7 then: {u} accept   (or: {u} dismiss)"]
    if streak_stopped(state):
        return [f"context: audit failed twice ({state['fails'][-1]}) \u2014 log "
                f"{cdir / 'audit.log'} \u00b7 then: {u} dismiss"]
    if (cu._load(cdir / "due.json") or {}).get("due") and not month_ok(state, cfg, now):
        return [f"context: monthly audit budget (${float(cfg.get('audit_month_usd', 100)):g}) "
                "reached \u2014 raise audit_month_usd or wait for the 1st"]
    return []


class Refused(Exception):
    pass


def _refuse(exc) -> int:
    print(f"refused: {exc}", file=sys.stderr)
    return 1


def _gate_owner(repo, verb: str) -> int:
    """0 to go on; 2 when headless, 1 while a run is going (its pid and start are printed)."""
    if os.environ.get("CONTEXT_KIT_HEADLESS") == "1":
        print(f"refused: {verb} is the owner's: it is refused in a headless run", file=sys.stderr)
        return 2
    state = load_state(repo)
    if state["status"] == "running":
        print(f"refused: an audit is running (pid {state['pid']}, started {state['started']}); "
              f"wait for it. Log: {_kit.upkeep_dir(repo) / 'audit.log'}", file=sys.stderr)
        return 1
    return 0


def _answers(tokens) -> dict:
    out = {}
    for t in tokens or []:
        m = re.fullmatch(r"(\d+)([yYnN])", t)
        if not m:
            raise Refused(f"{t!r} is not an answer: write <item number>y or <item number>n, e.g. 1y 2n")
        out[int(m.group(1))] = m.group(2).lower()
    return out


def _branch_ledger(repo, state: dict) -> str:
    """The ledger the audit branch added: the state's, else the newest one only that branch has."""
    rel = state.get("ledger") or ""
    if rel and _exists(repo, f"refs/heads/{BRANCH}:{rel}"):
        return rel
    names = _kit.git(repo, "log", "--format=", "--name-only", "--diff-filter=A",
                     f"refs/heads/{BRANCH}", "--not", f"--exclude={BRANCH}", "--branches", "--",
                     f"{cu._LEDGER_DIR}/").stdout.split()
    return max((n for n in names if cu._LEDGER_NAME.match(n.rpartition("/")[2])), default="")


def _later(*stamps: str) -> str:
    """The latest of the timestamps that parse, as written; "" if none does."""
    got = [(cu._parse(s), s) for s in stamps if cu._parse(s)]
    return max(got, key=lambda g: g[0])[1] if got else ""


def _covered(repo, ledger: str, state: dict) -> str:
    """The watermark after this audit: its ledger's covered_until, or what the state had."""
    text = _kit.git(repo, "show", f"refs/heads/{BRANCH}:{ledger}").stdout if ledger else ""
    m = _COVERED.search(text)
    return _later(state["covered"], m.group(1) if m else "")


def _checkout_answers(repo) -> Path:
    """The kit's own worktree path, on the existing audit branch: where answers are applied."""
    path = worktree_path(repo)
    for old in {p for p in (audit_worktree(repo), path) if p}:
        _kit.git(repo, "worktree", "remove", "--force", str(old))
    _kit.git(repo, "worktree", "prune")
    _rmtree(path)
    _kit.git(repo, "worktree", "prune")
    done = _kit.git(repo, "worktree", "add", "-q", str(path), BRANCH)
    if done.returncode != 0:
        raise Refused(f"cannot check out {BRANCH}: {done.stderr.strip()}")
    return audit_worktree(repo) or path


# What the skill lets each kind of item run: an undo restores an applied move; an apply archives
# a conflict's loser, rewords or pins.
ITEM_COMMANDS = {"undo": ("restore",), "apply": ("move", "revise", "pin")}


def _run_item(wt: Path, item: dict) -> None:
    """Run an item's recorded command in the worktree, with --owner: only the tier commands its
    kind allows, never a shell, and through this module's own copy of the script."""
    try:
        argv = shlex.split(item["cmd"])
    except ValueError:
        argv = []
    at = next((i for i, a in enumerate(argv) if a.replace("\\", "/").endswith("context_upkeep.py")), -1)
    sub = argv[at + 1:] if at >= 0 else []
    allowed = ITEM_COMMANDS.get(item["kind"], ())
    if not sub or sub[0] not in allowed or "--owner" not in sub:
        raise Refused(f"item {item['n']} ({item['kind']}) may run only {' or '.join(allowed)} with "
                      f"--owner, not {item['cmd']!r}")
    if item["kind"] == "undo" and any(a == "--text" or a.startswith("--text=") for a in sub):
        raise Refused(f"item {item['n']} (undo) puts back the moved words, not new ones: "
                      f"{item['cmd']!r}")
    env = {k: v for k, v in os.environ.items() if k != "CONTEXT_KIT_HEADLESS"}
    done = cu._run([sys.executable, str(HOOKS / "context_upkeep.py"), *sub], wt, 120,
                   {**env, "CLAUDE_PROJECT_DIR": str(wt)})
    if done.returncode != 0:
        raise Refused(f"item {item['n']} failed ({item['cmd']}): {(done.stderr or done.stdout).strip()}")


def _orphan_base(repo, state: dict, target: str) -> str:
    """Where an orphan audit started: the state's base when the branch descends from it, else
    where the branch left `target`."""
    base = state.get("base") or ""
    if base and _kit.git(repo, "merge-base", "--is-ancestor", base, BRANCH).returncode == 0:
        return base
    return _kit.git(repo, "merge-base", BRANCH, target).stdout.strip()


def _apply_answers(repo, ledger: str, answers: dict, check_base: str = "") -> list[str]:
    """In a temp worktree on the audit branch: run what the answers call for, mark the items in
    the ledger, and commit through `context_upkeep.py commit`. Returns the items left open. Any
    failure drops the whole worktree, so a refused accept changes nothing. `check_base` (an
    audit no run vouched for) is validated first, as a run's own result is."""
    wt = _checkout_answers(repo)
    try:
        if check_base:
            why, _ = validate(repo, wt, check_base)
            if why:
                raise Refused(f"{BRANCH} fails validation ({why}): it is not safe to merge. "
                              "Run dismiss to drop it")
        path = wt / ledger
        text = path.read_text(encoding="utf-8").replace("\r", "")
        items = owner_items(text)
        unknown = sorted(set(answers) - {x["n"] for x in items})
        if unknown:
            raise Refused(f"the ledger has no item {', '.join(map(str, unknown))}")
        bare = [x["n"] for x in items if x["mark"] == " " and not x["kind"]
                and answers.get(x["n"]) == "n"]
        if bare:
            raise Refused(f"item {', '.join(map(str, bare))} has no undo: or apply: command, so "
                          "`n` would do nothing: settle it by hand and leave it out of the answers")
        before = _kit.git(wt, "rev-parse", "HEAD").stdout.strip()
        lines, marks = text.split("\n"), {}
        for item in items:
            if item["mark"] != " ":  # an earlier try of this accept already settled it
                continue
            ans = answers.get(item["n"])
            if item["kind"] == "undo":  # applied, default yes: only an `n` has work to do
                if ans == "n":
                    _run_item(wt, item)
                marks[item["i"]] = "n" if ans == "n" else "x"
            elif ans == "y" and item["cmd"]:
                _run_item(wt, item)
                marks[item["i"]] = "x"
            elif ans == "n":
                marks[item["i"]] = "n"
        for i, mark in marks.items():
            lines[i] = lines[i].replace("[ ]", f"[{mark}]", 1)
        _kit.atomic_write(path, "\n".join(lines))
        _, live, tier = _audit_paths(wt)  # anything else in this scratch checkout is not ours
        changed = [p for p in (e[3:] for e in _kit.git(
            wt, "status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0") if e)
            if p == ledger or p in live or tier.fullmatch(p)]
        if changed:
            done = cu.commit(wt, f"context: answers {Path(ledger).stem} [context-kit]", changed)
            if done.returncode != 0:
                raise Refused(f"committing the answers failed: {done.stderr.strip()}")
            why = changes_why(wt, before, "HEAD", ledger)
            if why:
                _kit.git(wt, "reset", "-q", "--hard", before)  # the branch as the run left it
                raise Refused(f"the answers would change what an audit may not ({why}): "
                              "nothing was merged")
        return [x["text"] for x in owner_items("\n".join(lines)) if x["mark"] == " "]
    finally:
        cleanup(repo, keep_branch=True)


def _current_branch(repo) -> str:
    return _kit.git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()


def _make_current(repo, target: str, remote: bool) -> None:
    """Merge mode's preconditions: the owner is on `target`, and local `target` equals
    origin/`target` (a clean branch that is only behind is fast-forwarded)."""
    if _current_branch(repo) != target:
        raise Refused(f"check out {target} first (this checkout is on "
                      f"{_current_branch(repo) or 'no branch'}): the audit merges into it")
    rem = f"refs/remotes/origin/{target}"
    if not remote or not _exists(repo, rem):  # no remote, or an empty one the push will create
        return
    local, there = (_kit.git(repo, "rev-parse", r).stdout.strip() for r in (target, rem))
    if local == there:
        return
    if _kit.git(repo, "merge-base", "--is-ancestor", local, there).returncode != 0:
        raise Refused(f"local {target} has commits origin/{target} lacks (or the two diverged): "
                      "push or reconcile them first")
    done = _kit.git(repo, "merge", "--ff-only", rem)
    if done.returncode != 0:
        raise Refused(f"cannot fast-forward {target} to origin/{target}: {done.stderr.strip()}")


def _accept(repo, answers: dict, cfg: dict) -> int:
    state = load_state(repo)
    has_branch = _exists(repo, f"refs/heads/{BRANCH}")
    if state["status"] not in ("ready", "failed") or not has_branch:
        raise Refused(f"no audit to accept (status {state['status']}"
                      + ("" if has_branch else f", no {BRANCH} branch") + ")")
    elsewhere = _branch_worktree(repo)
    if elsewhere and audit_worktree(repo) is None:
        raise Refused(f"{BRANCH} is checked out in {elsewhere}: switch away from it first")
    target, pr = cfg.get("work_branch") or cfg.get("main_branch") or "main", \
        bool(cfg.get("require_pr_to_main"))
    remote = _kit.git(repo, "remote", "get-url", "origin").returncode == 0
    if pr and not remote:
        raise Refused("require_pr_to_main needs an origin remote to push the branch to")
    ledger = _branch_ledger(repo, state)
    if not ledger:
        raise Refused(f"{BRANCH} holds no audit ledger")
    # Everything that can refuse comes before anything is written, so a retry starts clean.
    if _kit.git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Refused("the working tree has uncommitted changes: commit or stash them first")
    if remote and not fetch(repo, target):
        raise Refused(f"fetch from origin failed (see {_kit.upkeep_dir(repo) / 'audit.log'}): "
                      "cannot tell whether main is current")
    if not pr:
        _make_current(repo, target, remote)
    open_items = _apply_answers(repo, ledger, answers,
                                _orphan_base(repo, state, target) if state["status"] != "ready" else "")
    pushed = True
    if pr:
        done = _kit.git(repo, "push", "origin", BRANCH, timeout=600)
        if done.returncode != 0:
            raise Refused(f"pushing {BRANCH} failed: {done.stderr.strip()}")
        note = f"pushed {BRANCH}. Open the PR: gh pr create --fill --head {BRANCH}"
    else:
        done = _kit.git(repo, "merge", "--no-ff", "--no-edit", "-m",
                        f"context: merge audit {Path(ledger).stem} [context-kit]", BRANCH)
        if done.returncode != 0:
            _kit.git(repo, "merge", "--abort")
            raise Refused(f"merging {BRANCH} into {target} failed and was aborted; resolve it by "
                          "hand and run accept again, or run dismiss to drop the audit: "
                          f"{(done.stderr or done.stdout).strip()}")
        note = f"merged {BRANCH} into {target}"
        if remote:
            done = _kit.git(repo, "push", "origin", target, timeout=600)
            pushed = done.returncode == 0
            note += ", pushed" if pushed else (f"; the push failed ({done.stderr.strip()}): run "
                                               f"git push origin {target}")
    update_state(repo, status="idle", reason="", fails=[], ledger="", summary={},
                 covered=_covered(repo, ledger, state))
    cleanup(repo, keep_branch=False)  # after idle: a helper that sees the branch is held off
    print(note)
    for text in open_items:
        print(f"still open: {text}")
    return 0 if pushed else 1


def accept(repo, answers, cfg: dict) -> int:
    """The owner takes the audit: apply the answers, commit them, then merge (and push) or push
    the branch for a PR, and go idle. 2 headless, 1 for any refusal (nothing changed)."""
    repo = Path(repo).resolve()
    code = _gate_owner(repo, "accept")
    if code:
        return code
    try:
        # launch.lock keeps a helper's reap off the kit's worktree and the branch meanwhile
        with _kit.locked(_kit.upkeep_dir(repo) / "launch.lock", wait=30, stale=LAUNCH_STALE_S):
            return _accept(repo, _answers(answers), cfg)
    except Refused as exc:
        return _refuse(exc)
    except TimeoutError:
        return _refuse("a helper holds launch.lock: try again in a moment")


def dismiss(repo) -> int:
    """The owner turns the audit down (or clears a stopped streak): count its sessions as covered, drop the branch, go idle, forget the failures."""
    repo = Path(repo).resolve()
    code = _gate_owner(repo, "dismiss")
    if code:
        return code
    try:
        with _kit.locked(_kit.upkeep_dir(repo) / "launch.lock", wait=30, stale=LAUNCH_STALE_S):
            state = load_state(repo)
            if state["status"] == "running":
                raise Refused("an audit started meanwhile")
            elsewhere = _branch_worktree(repo)
            if elsewhere and audit_worktree(repo) is None:
                raise Refused(f"{BRANCH} is checked out in {elsewhere}: switch away from it first")
            ledger = _branch_ledger(repo, state) if _exists(repo, f"refs/heads/{BRANCH}") else ""
            update_state(repo, status="idle", reason="", fails=[], ledger="", summary={},
                         covered=_covered(repo, ledger, state))
            cleanup(repo, keep_branch=False)
    except Refused as exc:
        return _refuse(exc)
    except TimeoutError:
        return _refuse("a helper holds launch.lock: try again in a moment")
    print("dismissed: the audit is dropped and the gate is open again")
    return 0
