"""Context upkeep: keep a condensed copy of every owner session, and say when an audit is due.

Why a store of our own: Claude Code deletes old transcripts, and an audit has to read weeks of
them. So each owner session is condensed into `<git-common-dir>/context-kit/sessions/<id>.json`
(inside .git: never committed, shared by every worktree of the clone).

Two entry points, split by cost:
  attention  SessionStart. Must stay fast and quiet: decide the session kind from the
             environment, run two cheap git calls, record this session's start SHA, print the due
             line if the helper last found an audit due, and detach the helper. It never opens a
             transcript and never computes "due" — it only reads `due.json`.
  _helper    The detached worker. Backfills the store from the transcripts, prunes records an
             audit covered long ago, and writes `due.json` for the next `attention`.

The tier commands (`lookup move restore revise ignored seen promote tombstone pin unpin`) wire the guarded
moves of upkeep_tiers.py to the audit skill. Each prints JSON, or the guard's refusal with exit
code 3. `--owner` is the owner's approval, and is refused in a headless run.

The audit commands feed `/auditing-sessions`: `batch` writes the sessions past the watermark
to ./.context-kit-batch/ and prints them with the mechanical inputs (dangling references,
orphan surfaces, module drift, the restated-figures sweep); `ledger` prints the figures and
model families the ledger records.

usage: <python_cmd> .claude/hooks/context_upkeep.py attention        (payload on stdin)
       <python_cmd> .claude/hooks/context_upkeep.py _helper [--repo <dir>]
       <python_cmd> .claude/hooks/context_upkeep.py batch | ledger
       <python_cmd> .claude/hooks/context_upkeep.py ledger --seed [--models <family>]
       <python_cmd> .claude/hooks/context_upkeep.py accept [<n>y|<n>n ...] | dismiss
       <python_cmd> .claude/hooks/context_upkeep.py <tier command> -h
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path

import _kit

MAX_DAYS = 21
PRUNE_AFTER_DAYS = 30
BATCH_MAX = 40
BATCH_DIR = ".context-kit-batch"  # in cwd; it ignores itself
# Windows creation flags as numbers, so they exist (and are testable) on every OS.
DETACHED_PROCESS, NEW_PROCESS_GROUP, BREAKAWAY_FROM_JOB = 0x8, 0x200, 0x01000000
_LEDGER_DIR = "docs/references/session-audits"
_LEDGER_NAME = re.compile(r"^\d{4}-\d\d-\d\d-\d{4}\.md$")
_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z?$")


# --- small helpers ---------------------------------------------------------------------------

def _fold(s: str) -> str:
    """Windows and macOS file systems ignore case; Linux does not, so only fold there."""
    return s.casefold() if os.name == "nt" or sys.platform == "darwin" else s


def _slug(path) -> str:
    """How Claude Code names a project's transcript dir: every non-alphanumeric becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _parse(ts: str):
    """A transcript or ledger timestamp as an aware datetime; None when it is not one."""
    m = _TS.match(ts or "")
    if not m:
        return None
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return base + timedelta(seconds=float(m.group(2) or 0))


def _load(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def projects_root() -> Path:
    return Path(os.environ.get("CONTEXT_KIT_PROJECTS") or Path.home() / ".claude" / "projects")


def _sessions(repo, cdir: Path | None = None) -> Path:
    return (cdir or _kit.upkeep_dir(repo)) / "sessions"


def model_family(m) -> str:
    """`claude-sonnet-4-5-20250929` -> `claude-sonnet-4-5`; the synthetic placeholder is no model."""
    m = str(m or "")
    return "" if m.startswith("<") else re.sub(r"-\d{8}$", "", m)


# --- reading a transcript --------------------------------------------------------------------

def read_record(transcript) -> dict | None:
    """Condense one transcript into a record. None for an sdk session or one with no owner turn.
    Bad lines (a half-written last line while the session is live) are skipped."""
    from audit_sessions import condense_records, files_of, records_of  # lazy: attention skips it

    transcript = Path(transcript)
    seen = {"entry": "", "cwd": "", "sdk": False}
    stamps: list[str] = []
    models: Counter = Counter()
    files: dict[str, None] = {}

    def scan():
        """Feed `condense_records` the main-thread records while noting what a record needs.
        One streamed pass: a transcript can be tens of MB and is never held whole."""
        for rec in records_of(transcript):
            if rec.get("isSidechain"):
                continue
            seen["entry"] = seen["entry"] or str(rec.get("entrypoint") or "")
            if seen["entry"].startswith("sdk-"):
                seen["sdk"] = True
                return
            seen["cwd"] = seen["cwd"] or str(rec.get("cwd") or "")
            kind = rec.get("type")
            if kind in ("user", "assistant"):
                if rec.get("timestamp"):
                    stamps.append(rec["timestamp"])
                message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                content = message.get("content")
                if kind == "assistant":
                    family = model_family(message.get("model"))
                    if family:
                        models[family] += 1
                    files.update(dict.fromkeys(files_of(content)))
            yield rec

    try:
        text, owner_turns, turns = condense_records(scan())
    except OSError:
        return None
    if seen["sdk"] or owner_turns == 0:
        return None
    return {"id": transcript.stem, "entrypoint": seen["entry"], "cwd": seen["cwd"],
            "ts_start": stamps[0] if stamps else "", "ts_end": stamps[-1] if stamps else "",
            "model_family": models.most_common(1)[0][0] if models else "",
            "owner_turns": owner_turns, "turns": turns, "files": list(files), "text": text,
            "start_sha": ""}


# --- the store -------------------------------------------------------------------------------

def _worktrees(repo) -> list[Path]:
    done = _kit.git(repo, "worktree", "list", "--porcelain")
    return [Path(line[9:]).resolve() for line in done.stdout.splitlines()
            if done.returncode == 0 and line.startswith("worktree ")]


def project_dirs(repo) -> list[Path]:
    """Transcript dirs that may hold this repo's sessions: the slug of the repo and of each
    worktree, plus every dir whose name merely starts with one (a worktree nested under the
    repo, or a sibling like `repo-2` — the cwd filter in backfill tells those apart)."""
    root = projects_root()
    if not root.is_dir():
        return []
    repo = Path(repo)
    slugs = {_fold(_slug(p)) for p in (repo, repo.resolve(), *_worktrees(repo))}
    return [d for d in sorted(root.iterdir())
            if d.is_dir() and any(_fold(d.name).startswith(s) for s in slugs)]


def _within(cwd: str, roots: list[str]) -> bool:
    c = _fold(str(Path(cwd).resolve()))
    return any(c == r or c.startswith(r + os.sep) for r in roots)


def record_start(repo, sid: str, sha: str, cdir: Path | None = None) -> None:
    """Remember the HEAD this session started at, as a stub backfill later fills in. A resumed
    session keeps its first start."""
    path = _sessions(repo, cdir) / f"{sid}.json"
    rec = _load(path) or {"id": sid}
    if not rec.get("start_sha"):
        rec["start_sha"] = sha
        _kit.atomic_write(path, json.dumps(rec) + "\n")


def backfill(repo) -> int:
    """Write a record for every owner transcript of this repo (worktrees included) that is new
    or has grown since it was captured. Returns how many were written."""
    repo = Path(repo).resolve()
    out = _sessions(repo)
    roots = [_fold(str(p)) for p in (repo, *_worktrees(repo))]
    covered = _parse(watermark(repo))  # an audit has read everything up to here: skip it
    written = 0
    for d in project_dirs(repo):
        for path in sorted(d.glob("*.jsonl")):
            try:
                st = path.stat()
            except OSError:
                continue
            if covered and datetime.fromtimestamp(st.st_mtime, timezone.utc) <= covered:
                continue
            target = out / f"{path.stem}.json"
            old = _load(target) or {}
            src = [st.st_size, st.st_mtime_ns]
            if old.get("ts_end") and old.get("src") == src:
                continue
            try:  # one unreadable transcript must not stop the rest being captured
                rec = read_record(path)
                if rec is None or not rec["cwd"] or not _within(rec["cwd"], roots):
                    continue
                rec.update(src=src, start_sha=old.get("start_sha", ""))
                _kit.atomic_write(target, json.dumps(rec) + "\n")
                written += 1
            except Exception:
                _log(repo, f"backfill {path}: {traceback.format_exc()}")
    return written


def records_after(repo, after: str) -> list[dict]:
    """Captured records that end after `after` ("" for all), oldest first, ties by id. Stubs are
    not records."""
    cutoff, found = _parse(after), []
    for path in _sessions(repo).glob("*.json"):
        rec = _load(path)
        end = _parse(rec.get("ts_end", "")) if rec else None
        if end and (not cutoff or end > cutoff):
            found.append((end, rec))
    return [rec for _, rec in sorted(found, key=lambda pair: (pair[0], str(pair[1].get("id"))))]


def prune(repo, covered_until: str, now: datetime) -> int:
    """Delete records an audit covered and that ended over PRUNE_AFTER_DAYS ago, and stubs that
    never got a transcript in that long."""
    covered, old = _parse(covered_until), now - timedelta(days=PRUNE_AFTER_DAYS)
    gone = 0
    for path in _sessions(repo).glob("*.json"):
        rec = _load(path)
        end = _parse(rec.get("ts_end", "")) if rec else None
        if end:
            stale = bool(covered) and end <= covered and end < old
        else:
            stale = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < old
        if stale:
            path.unlink(missing_ok=True)
            gone += 1
    return gone


def _ledgers(repo) -> list[tuple[str, str]]:
    """(ledger name, the commit that added it), newest first, over every local branch and
    `origin/<main_branch>`: an audit committed on another branch still covers its sessions, and
    one merged on the remote (PR mode) covers them before the owner pulls. Other fetched
    remote-tracking refs, the stash and the audit branch (context/*) are not this clone's
    sessions. HEAD's listing is the fallback."""
    main = _kit.config(repo).get("main_branch") or "main"
    merged = f"refs/remotes/origin/{main}"
    extra = [merged] if _kit.git(repo, "rev-parse", "--verify", "-q", merged).returncode == 0 else []
    log = _kit.git(repo, "-c", "core.quotePath=false", "log", "--exclude=context/*",
                   "--branches", *extra, "--diff-filter=A",
                   "--name-only", "--format=commit %H", "--", f"{_LEDGER_DIR}/")
    found: dict = {}
    sha = ""
    for line in log.stdout.splitlines() if log.returncode == 0 else []:
        if line.startswith("commit "):
            sha = line[7:].strip()
            continue
        head, _, name = line.strip().rpartition("/")
        if head == _LEDGER_DIR and _LEDGER_NAME.match(name):
            found.setdefault(name, sha)  # log is newest first: the latest add of that name
    if not found:
        listing = _kit.git(repo, "ls-tree", "--name-only", f"HEAD:{_LEDGER_DIR}/")
        if listing.returncode == 0:
            found = {n: "HEAD" for n in listing.stdout.splitlines() if _LEDGER_NAME.match(n)}
    return sorted(found.items(), reverse=True)


def watermark(repo) -> str:
    """`covered_until` of the newest ledger on any local ref or origin/<main_branch>, or the mark
    `accept` or `dismiss` left in state.json when that is later (a dismissed audit has no ledger
    on any of those, and an accepted PR's has none until it merges)."""
    mark = ""
    for name, sha in _ledgers(repo):
        shown = _kit.git(repo, "show", f"{sha}:{_LEDGER_DIR}/{name}")
        m = re.search(r"^covered_until:\s*['\"]?([^\s'\"]+)", shown.stdout, re.MULTILINE)
        if shown.returncode == 0 and m:
            mark = m.group(1)
            break
    kept = (_load(_kit.upkeep_dir(repo) / "state.json") or {}).get("covered") or ""
    return kept if _parse(kept) and (not _parse(mark) or _parse(kept) > _parse(mark)) else mark


def due(repo, cfg: dict, now: datetime) -> tuple[bool, int]:
    """(is an audit due, owner records past the watermark): enough records, or MAX_DAYS since
    the watermark (the oldest record, before the first audit) with at least one."""
    mark = watermark(repo)
    records = records_after(repo, mark)
    n = len(records)
    if n >= int(cfg.get("audit_every_sessions", 10)):
        return True, n
    since = _parse(mark) or (_parse(records[0]["ts_start"]) if records else None)
    return bool(n and since and now - since >= timedelta(days=MAX_DAYS)), n


# --- the two entry points --------------------------------------------------------------------

def _log(repo, text: str) -> None:
    """Append to C/audit.log. Logging never raises."""
    try:
        log = _kit.upkeep_dir(repo) / "audit.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8", newline="\n") as f:
            f.write(text.rstrip("\n") + "\n")
    except Exception:
        pass


def helper(repo, now: datetime | None = None) -> None:
    repo, now = Path(repo).resolve(), now or datetime.now(timezone.utc)
    cdir = _kit.upkeep_dir(repo)
    try:  # one capturer at a time; a second helper just reads what the first writes
        with _kit.locked(cdir / "backfill.lock", wait=0, stale=300):
            backfill(repo)
            prune(repo, watermark(repo), now)
    except TimeoutError:
        pass
    finally:  # whatever capture hit, the next `attention` should read a fresh answer
        is_due, n = due(repo, _kit.config(repo), now)
        _kit.atomic_write(cdir / "due.json",
                          json.dumps({"due": is_due, "n": n, "since": watermark(repo)}) + "\n")
    import upkeep_audit  # here, not at the top: it imports this module
    upkeep_audit.launch(repo, now, is_due)  # Stage B: reap, then the gate once due


def _launch(root: Path) -> None:
    """Start the helper so it outlives this hook: no window, own process group, no inherited
    stdio. On Windows also leave the hook's job object (it is killed with the hook otherwise);
    a job that forbids breakaway raises OSError, so retry without it."""
    argv = [sys.executable, str(Path(__file__).resolve()), "_helper", "--repo", str(root)]
    stdio = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name != "nt":
        subprocess.Popen(argv, start_new_session=True, **stdio)
        return
    flags = DETACHED_PROCESS | NEW_PROCESS_GROUP
    try:
        subprocess.Popen(argv, creationflags=flags | BREAKAWAY_FROM_JOB, **stdio)
    except OSError:
        subprocess.Popen(argv, creationflags=flags, **stdio)


def _stage_b(root, cdir: Path) -> list[str]:
    """Stage B's prompt lines; none (and no import) until a headless run has left a state."""
    try:
        if not (cdir / "state.json").exists():
            return []
        import upkeep_audit
        return upkeep_audit.prompt_lines(root, _kit.config(root), datetime.now(timezone.utc), cdir)
    except Exception:  # the prompt never blocks a session
        _log(root, f"prompt: {traceback.format_exc()}")
        return []


def _headless_running(cdir: Path) -> bool:
    """A headless audit is running now, so "audit due" would send the owner to a `batch` that
    refuses. Reads state.json only: `attention` stays one file read here."""
    return (_load(cdir / "state.json") or {}).get("status") == "running"


def attention() -> None:
    try:  # a terminal (run by hand) or no stdin sends nothing: reading it would block forever
        payload = json.load(sys.stdin) if sys.stdin and not sys.stdin.isatty() else {}
    except (ValueError, OSError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if _kit.session_kind(payload, read_transcript=False) != "owner":
        return
    root = _kit.repo_root()
    if not _kit.auto_audit(root):  # git call 1: nothing is written before this passes
        return
    # git call 2: HEAD, and the common dir in the same breath (the state lives there)
    done = _kit.git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", "HEAD")
    lines = done.stdout.split("\n")
    if done.returncode != 0 or len(lines) < 2:
        return
    cdir = Path(lines[0].strip()).resolve() / "context-kit"
    if payload.get("session_id"):
        record_start(root, str(payload["session_id"]), lines[1].strip(), cdir)
    state = _load(cdir / "due.json") or {}
    lines = _stage_b(root, cdir)  # an audit to review, a stopped streak, the monthly cap
    if not lines and state.get("due") and not _headless_running(cdir):
        since = str(state.get("since", ""))[:10] or "capture began"
        lines = [f"context: audit due \u2014 {state.get('n', '?')} sessions since {since}. "
                 "Run /auditing-sessions"]
    if lines:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({"systemMessage": "\n".join(lines)}, ensure_ascii=False))
        sys.stdout.flush()
    _launch(root)


# --- the audit's batch, inputs and figures ---------------------------------------------------

_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree
_FIGURE = re.compile(r"\b\d+(\.\d+)?\s*(%|ms|s|MB|GB|x|of \d+)(?![A-Za-z])")
_STATUS = re.compile(r"(?i)\b(green|red|pending)\b")  # not `live`: that names the tier
_BACKTICK = re.compile(r"`([^`\s]+)`")
# ponytail: a first-words heuristic for "the owner is correcting or re-asking"; the rubric's
# correction-or-re-ask count is the judged figure, this one is the mechanical one audits compare.
_CORRECTION = re.compile(
    r"(?i)^\W*(no\b|nope\b|not (?:that|what|quite)\b|wrong\b|that'?s (?:not|wrong)\b"
    r"|i (?:said|asked|meant|told you)\b|again\b|still\b|stop\b|why (?:did|didn'?t) you\b"
    r"|you (?:didn'?t|missed|forgot)\b)")


def batch_records(repo) -> list[dict]:
    """The records the next audit reads: at most BATCH_MAX past the watermark, oldest first."""
    return records_after(repo, watermark(repo))[:BATCH_MAX]


def batch(repo, out: Path | None = None) -> dict:
    """Write one `<id>.txt` per batch record (a stale batch is cleared first) and return the
    files, the `covered_until` the ledger will carry, `inputs()`, and `u`: the exact command
    prefix the headless run is allowed (`claude_argv` builds its allowlist from the same
    `python_cmd`)."""
    out = Path(out or Path.cwd() / BATCH_DIR)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.txt"):
        old.unlink()
    if not (out / ".gitignore").exists():
        _kit.atomic_write(out / ".gitignore", "*\n")
    records, files = batch_records(repo), []
    for rec in records:
        path = out / f"{rec['id']}.txt"
        head = (f"session {rec['id']}  {rec.get('ts_start', '')} .. {rec['ts_end']}  "
                f"model {rec.get('model_family') or '?'}\n"
                f"files: {', '.join(rec.get('files') or []) or '-'}\n")
        _kit.atomic_write(path, head + rec.get("text", ""))
        files.append(str(path))
    return {"files": files, "covered_until": records[-1]["ts_end"] if records else watermark(repo),
            "inputs": inputs(repo), "headless": os.environ.get("CONTEXT_KIT_HEADLESS") == "1",
            "u": f"{_kit.config(repo).get('python_cmd') or 'python'} .claude/hooks/context_upkeep.py"}


def _base(repo: Path) -> str:
    """The commit that added the last ledger (on whichever ref), so the last audit's own moves,
    restores and promotions do not count as added since; the empty tree before the first audit."""
    names = _ledgers(repo)
    if not names:
        return _EMPTY_TREE
    # the merge-base with HEAD, so an unmerged audit branch's own adds are not diffed as orphans
    done = _kit.git(repo, "merge-base", names[0][1], "HEAD")
    if done.returncode != 0:
        done = _kit.git(repo, "rev-parse", "--verify", "-q", f"{names[0][1]}^{{commit}}")
    return (done.stdout.strip() if done.returncode == 0 else "") or _EMPTY_TREE


def _globbed(repo: Path, glob: str) -> bool:
    try:
        return any(repo.glob(glob.lstrip("/")))
    except (ValueError, NotImplementedError):
        return False


def _glob_dir(glob: str) -> str:
    """The fixed directory a `paths:` glob names: `src/store/**/*.py` -> `src/store`."""
    fixed = []
    for part in glob.strip("/").split("/"):
        if re.search(r"[*?[{]", part):
            return "/".join(fixed)
        fixed.append(part)
    return "/".join(fixed[:-1])  # no wildcard: the glob names a file


def _known(repo: Path, rows: list[dict], ut) -> list[dict]:
    """The rows that still resolve nowhere once three more things count: the tail of a tracked
    path or directory (`store/db.py` cited by its module), a branch, tag or remote ref name, and
    a gitignored path (a data or log dir that is never tracked)."""
    files = _kit.git(repo, "ls-files", "-z").stdout.split("\0")
    dirs = {"/".join(parts[:j]) for parts in (f.split("/") for f in files)
            for j in range(1, len(parts))}
    def tails_of(paths):
        return {_fold("/".join(q.split("/")[i:])) for q in paths if q for i in range(q.count("/") + 1)}
    dir_tails, tails = tails_of(dirs), tails_of((*files, *dirs))  # tails: for globs only
    refs = set()
    for line in _kit.git(repo, "show-ref").stdout.splitlines():
        name = line.partition(" ")[2]
        refs |= {name, re.sub(r"^refs/(heads|tags|remotes)/", "", name)}

    def tracked(ref):
        key = ref.strip("/")
        if "*" in key:
            return any(fnmatch(t, _fold(key)) for t in tails)
        return _fold(key) in dir_tails or ut.tail_match(files, key)
    left = [r for r in rows if r["ref"].strip("/") not in refs and not tracked(r["ref"])]
    # gitignored, from the root or from the citing file's dir; `..` would be outside the repo
    asked = {}
    for r in left:
        for c in (r["ref"], f"{Path(r['file']).parent.as_posix()}/{r['ref']}"):
            c = os.path.normpath(c).replace(os.sep, "/") + ("/" if c.endswith("/") else "")
            if not c.startswith(".."):
                asked.setdefault(c, []).append(id(r))
    ignored, names = set(), sorted(asked)
    for i in range(0, len(names), 100):  # ponytail: chunked to stay under Windows' command line
        done = _kit.git(repo, "-c", "core.quotePath=false", "check-ignore", "--", *names[i:i + 100])
        for name in done.stdout.splitlines():
            ignored.update(asked.get(name, []))
    return [r for r in left if id(r) not in ignored]


def _dangling(repo: Path, cc, ut) -> list[dict]:
    """Backticked paths (a `/` or a known extension) that resolve nowhere, and `paths:` globs
    that match nothing. A path resolves from the root, from the citing file's dir, from a dir
    its `paths:` globs name, or as `_known` says (most rows are module-relative)."""
    out, globs_out = [], []
    for rel in ut.live_files(repo):
        text = ut.read(repo / rel)
        globs = ut._paths_globs(text)
        bases = {repo, repo / Path(rel).parent, *(repo / _glob_dir(g) for g in globs)}
        for n, line in enumerate(text.split("\n"), 1):
            for tok in _BACKTICK.findall(line):
                ref = tok.split("::")[0].rstrip(".,;")
                if (not ref or ref[0] in "-~/@" or "://" in ref or re.search(r"[:<>{}$()=|]", ref)
                        or not ("/" in ref or Path(ref).suffix.lower() in cc._SUFFIXES)):
                    continue
                if not any(_globbed(b, ref) if "*" in ref else (b / ref).exists() for b in bases):
                    out.append({"file": rel, "line": n, "ref": ref})
        globs_out += [{"file": rel, "line": 0, "ref": g} for g in globs if not _globbed(repo, g)]
    return (_known(repo, out, ut) if out else []) + globs_out


def _orphans(repo: Path, base: str, ut) -> list[dict]:
    """Surfaces that are gone with no tombstone: the sources of tier entries, and live files
    deleted or renamed since `base`. `renamed_to` follows git's rename history to a file that
    exists. (`git log --follow` cannot start from a path that no longer exists, so the whole
    history's renames are read once instead.)"""
    tombs = {_fold(k) for k in ut.tombstones(repo)}
    gone = {e.source.split("#")[0] for e in ut.load_entries(repo) if e.tier != "gap"}
    cfg = _kit.config(repo)
    globs = [cfg["rules_glob"], *cfg["claude_md_globs"]]
    diff = _kit.git(repo, "-c", "core.quotePath=false", "diff", "--name-status", "-M",
                    "--diff-filter=DR", base, "HEAD")
    for row in diff.stdout.splitlines():
        parts = row.split("\t")
        if len(parts) >= 2 and any(fnmatch(parts[1], g) for g in globs) and (
                not parts[1].startswith(".claude/") or fnmatch(parts[1], cfg["rules_glob"])):
            gone.add(parts[1])  # not .claude/**/CLAUDE.md: copies there are not modules
    gone = sorted(p for p in gone if p and not (repo / p).exists() and _fold(p) not in tombs)
    if not gone:
        return []
    log = _kit.git(repo, "-c", "core.quotePath=false", "log", "-M", "--diff-filter=R",
                   "--name-status", "--format=", "HEAD")
    renames: dict = {}
    for row in log.stdout.splitlines():
        parts = row.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            renames.setdefault(_fold(parts[1]), parts[2])  # newest first: the latest rename wins
    out = []
    for p in gone:
        new, seen = renames.get(_fold(p)), set()
        while new and not (repo / new).exists() and _fold(new) not in seen:
            seen.add(_fold(new))
            new = renames.get(_fold(new))
        out.append({"path": p, "renamed_to": new if new and (repo / new).exists() else None})
    return out


def _drift(repo: Path, since: str, ut) -> list[dict]:
    """Modules whose code has commits since their CLAUDE.md's last change and, by commit date,
    after the last audit's `covered_until`. By date, not from the ledger's commit: a seed ledger
    is committed after the code it does not cover. Oldest drift first: a run syncs only
    the first few, and a module never synced must not wait behind newer ones."""
    after = _parse(since)
    dated = [f"--since={after.strftime('%Y-%m-%d %H:%M:%S')} +0000"] if after else []
    out = []
    for rel in ut.live_files(repo):
        if Path(rel).name != "CLAUDE.md":
            continue
        mod = Path(rel).parent.as_posix()
        # not the kit's own commits (adopt, setup, audit): they edit the file, not describe the code
        last, _, when = _kit.git(repo, "log", "-1", "--format=%H %ct", "--fixed-strings",
                                 "--invert-grep", "--grep=[context-kit]", "--",
                                 rel).stdout.strip().partition(" ")
        revs = ["HEAD", *([f"^{last}"] if last else []), *dated]
        done = _kit.git(repo, "rev-list", "--count", *revs, "--", mod, f":(exclude){rel}")
        n = int(done.stdout.strip() or 0) if done.returncode == 0 else 0
        if n:
            out.append((int(when or 0), {"module": mod, "commits": n}))
    return [row for _, row in sorted(out, key=lambda t: t[0])]


def _restated(repo: Path, base: str, ut) -> list[dict]:
    """Lines added to live files since `base` that state a figure or a status, or (a new
    bullet) that `lookup` finds elsewhere: the restated-figures sweep. A renamed file's old
    path joins the diff, so its lines are not mistaken for added ones."""
    files = ut.live_files(repo)
    live = {_fold(r) for r in files}
    renamed = _kit.git(repo, "-c", "core.quotePath=false", "diff", "--name-status", "-M",
                       "--diff-filter=R", base, "HEAD")
    olds = [p[1] for p in (row.split("\t") for row in renamed.stdout.splitlines())
            if len(p) == 3 and _fold(p[2]) in live]
    if not files:
        return []
    diff = _kit.git(repo, "-c", "core.quotePath=false", "diff", "-M", "-U0", "--no-color",
                    base, "HEAD", "--", *files, *olds)
    out, rel, n = [], "", 0
    for line in diff.stdout.split("\n"):
        if line.startswith("+++ "):
            name = line[4:].rstrip("\t")
            rel = name[2:] if name.startswith("b/") and _fold(name[2:]) in live else ""
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            n = int(m.group(1)) if m else 0
            continue
        if not (rel and line.startswith("+")):
            continue
        text, n = line[1:].strip(), n + 1
        why = [w for w, rx in (("figure", _FIGURE), ("status", _STATUS)) if rx.search(text)]
        hits = []
        if line[1:].startswith("- "):  # top-level only: a nested bullet is no live line
            tag = ut.TAG.search(text)
            body, own = ut.TAG.sub("", text[2:]).strip(), tag.group(1) if tag else None
            # ponytail: the 5 longest words stand for the line; lookup wants all of them
            words = sorted(set(re.findall(r"[a-z][a-z0-9_-]{3,}", body.casefold())),
                           key=lambda w: (-len(w), w))[:5]
            if len(words) >= 2:
                seen, itself = set(), bool(own)  # a tagged line is found by its ID
                for h in ut.lookup(repo, words):  # live hits come first
                    tier = "live" if h.get("live") else h["tier"]  # a live line's history is it
                    key = (tier, h.get("id"))
                    same = not itself and h["tier"] == "live" and h["text"].strip() == body
                    itself = itself or same  # one same-text live hit is the line; more are twins
                    if (own and h.get("id") == own) or (h.get("id") and key in seen) or same:
                        continue  # the line itself, its own history, or a hit already listed
                    seen.add(key)
                    hits.append({"tier": tier, "id": h.get("id"), "text": h["text"]})
        if hits:
            why.append("lookup")
        if why:
            out.append({"file": rel, "line": n - 1, "text": text, "why": why, "hits": hits})
    return out


def inputs(repo) -> dict:
    """What the audit checks besides the sessions (`/auditing-sessions` step 5)."""
    import context_check as cc  # lazy: it reconfigures stdout when imported
    import upkeep_tiers as ut

    repo = Path(repo).resolve()
    base, mark = _base(repo), watermark(repo)  # sweeps diff from base; drift from mark
    return {"dangling": _dangling(repo, cc, ut), "orphans": _orphans(repo, base, ut),
            "drift": _drift(repo, mark, ut), "restated": _restated(repo, base, ut),
            "live_lines": len(ut._all_live(repo))}  # what the 10% net-removal cap is of


def figures(repo, records: list[dict]) -> dict:
    """The ledger's mechanical figures: owner asks, the first-words corrections among them (a
    cross-check on the rubric's judged count), and their rate."""
    owner = [t.get("owner", "") for r in records for t in r.get("turns") or []]
    corrections = sum(bool(_CORRECTION.match(t)) for t in owner)
    return {"asks": len(owner), "corrections": corrections,
            "rate": round(corrections / len(owner), 3) if owner else 0.0}


def ledger(repo) -> dict:
    """What the ledger records about the batch: figures, model families by use, the last ledger."""
    records = batch_records(repo)
    models = Counter(r.get("model_family") for r in records if r.get("model_family"))
    names = _ledgers(repo)
    return {"figures": figures(repo, records), "models": [m for m, _ in models.most_common()],
            "last": f"{_LEDGER_DIR}/{names[0][0]}" if names else ""}


def seed(repo, models: str = "", now: datetime | None = None) -> Path:
    """Write the seed ledger setup commits. Its `covered_until` is now in a fresh repo, and
    1 s before the earliest captured session's start when the store has records (an adoption),
    so every one of them stays auditable. Backfills first: at setup the helper may not have run.
    Refused once a ledger gives a watermark, since a seed would move it back."""
    import upkeep_tiers as ut  # lazy: `attention` never pays for it

    repo, now = Path(repo).resolve(), now or datetime.now(timezone.utc)
    folder, mark = repo / _LEDGER_DIR, watermark(repo)
    if mark:
        raise ut.GuardError(f"a ledger in {_LEDGER_DIR} already covers sessions until {mark}, and a "
                            "seed would move that back: nothing to seed. To start over, delete "
                            "the ledgers and commit that")
    backfill(repo)
    records = records_after(repo, "")
    starts = [t for t in (_parse(r.get("ts_start", "")) for r in records) if t]
    until = min(starts) - timedelta(seconds=1) if starts else now
    fams = models or ",".join(m for m, _ in Counter(
        r.get("model_family") for r in records if r.get("model_family")).most_common())
    if not fams:
        raise ut.GuardError("no captured session names a model: pass --models <family>")
    path = folder / f"{now.strftime('%Y-%m-%d-%H%M')}.md"
    _kit.atomic_write(path, f"---\ncovered_until: {until.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                            f"models: {fams}\n---\nSeed ledger written by setup. No audit has run.\n")
    return path


# --- spend and subprocesses ------------------------------------------------------------------

def add_spend(repo, usd: float) -> float:
    """Add `usd` to this month's spend in state.json (`month` YYYY-MM, `month_usd`); a new month
    starts again from 0. Returns the month's total."""
    cdir = _kit.upkeep_dir(repo)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _kit.locked(cdir / "state.lock"):
        state = _load(cdir / "state.json") or {}
        if state.get("month") != month:
            state["month"], state["month_usd"] = month, 0
        state["month_usd"] = round(state.get("month_usd", 0) + usd, 6)
        _kit.atomic_write(cdir / "state.json", json.dumps(state) + "\n")
    return state["month_usd"]


def _run(argv, cwd, timeout, env=None, shell=False):
    """A subprocess that never hangs, never prompts, and never raises: failure is returncode 1."""
    try:
        return subprocess.run(argv, cwd=cwd, shell=shell, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              env=env, timeout=timeout, creationflags=_kit.NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


# --- the tier commands -----------------------------------------------------------------------

TIER_COMMANDS = ("lookup", "move", "restore", "revise", "ignored", "seen", "promote", "tombstone",
                 "pin", "unpin")


def _tier_parsers(sub) -> None:
    def cmd(name, help, *args):
        """`x` is a positional, `x?` an optional positional, `--x` an option with default ""."""
        p = sub.add_parser(name, help=help)
        for a in args:
            if a.startswith("--"):
                p.add_argument(a, default="")
            else:
                p.add_argument(a.rstrip("?"), nargs="?" if a.endswith("?") else None, default="")
        p.add_argument("--owner", action="store_true", help="the owner approved this")
        return p
    cmd("lookup", "search every tier, legacy files and tombstones").add_argument(
        "words", nargs="*")
    sub.choices["lookup"].add_argument("--files", nargs="*", default=[])
    cmd("move", "move a live line (ID or exact text), or an overflow entry",
        "ref", "tier", "reason", "--evidence")
    cmd("restore", "put a moved line back", "id", "session?", "turn_ts?", "quote?", "--text")
    cmd("revise", "reword a live line", "ref", "new_text", "--session", "--quote")
    cmd("ignored", "a session went against a live line", "ref", "session", "turn_ts", "quote")
    cmd("seen", "a lesson no live line covers", "title", "--source", "--session", "--quote")
    cmd("promote", "make a gap a live line", "gap_id", "path", "section", "text", "--mechanism")
    cmd("tombstone", "retire a moved or deleted surface", "old", "new")
    cmd("pin", "pin a live line (the owner's call)", "ref")
    cmd("unpin", "unpin a live line (the owner's call)", "ref")


def _tier_command(args) -> int:
    import upkeep_tiers as ut  # lazy: `attention` never pays for it

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root, today = _kit.repo_root().resolve(), datetime.now(timezone.utc)  # move rows want the time

    def record(sid):
        rec = _load(_sessions(root) / f"{sid}.json") if sid else None
        if sid and rec is None:
            raise ut.GuardError(f"no captured session {sid}")
        return rec
    a, owner = args, args.owner
    try:
        ut._check_owner(owner)
        if a.cmd == "lookup":
            out = ut.lookup(root, a.words, a.files)
        elif a.cmd == "move":
            out = ut.move(root, a.ref, a.tier, a.reason, a.evidence, today, owner)
        elif a.cmd == "restore":
            out = ut.restore(root, a.id, record(a.session), a.turn_ts, a.quote, today,
                             a.text or None, owner)
        elif a.cmd == "revise":
            out = ut.revise(root, a.ref, a.new_text, record(a.session), a.quote, today, owner)
        elif a.cmd == "ignored":
            out = ut.ignored(root, a.ref, record(a.session), a.turn_ts, a.quote, today, owner)
        elif a.cmd == "seen":
            out = ut.seen(root, a.title, a.source or "session", record(a.session), a.quote,
                          today, owner)
        elif a.cmd == "promote":
            out = ut.promote(root, a.gap_id, a.path, a.section, a.text, a.mechanism, today, owner)
        elif a.cmd in ("pin", "unpin"):
            out = getattr(ut, a.cmd)(root, a.ref, today, owner)
        else:
            out = ut.tombstone(root, a.old, a.new, today, owner)
    except ut.GuardError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(out, ensure_ascii=False))
    return 0


def commit(root: Path, msg: str, paths: list[str]) -> subprocess.CompletedProcess:
    """Commit exactly `paths` with the repo's git hooks switched off, so an audit or sync commit
    does not start the repo's commit-time tooling (a paid review bot, a slow lint). The tag in
    the subject is how a later audit's drift scan, and any tool's exclusions, recognise the
    kit's own commits."""
    if "[context-kit]" not in "".join(msg.splitlines()[:1]):
        raise ValueError("the commit subject must contain [context-kit]")
    empty = _kit.git_common_dir(root) / "context-kit" / "nohooks"
    empty.mkdir(parents=True, exist_ok=True)
    added = _kit.git(root, "add", "--", *paths)
    if added.returncode != 0:
        return added
    return _kit.git(root, "-c", f"core.hooksPath={empty}", "commit", "-q", "-m", msg, "--", *paths)


def _headless_pending(root: Path) -> str:
    """Why an owner's audit must not start now: a headless audit of the same sessions is running
    or waits for review. "" when none is."""
    import upkeep_audit as ua  # lazy: it imports this module
    state = ua.load_state(root)
    u = f"{_kit.config(root).get('python_cmd') or 'python'} .claude/hooks/context_upkeep.py"
    if state["status"] == "running":
        return (f"a headless audit of these sessions is running (pid {state['pid']}, started "
                f"{state['started']}); wait for it, then review it")
    if state["status"] == "ready" or (state["status"] == "failed"
                                      and state["reason"] == "orphan branch"):
        return (f"a headless audit of these sessions waits on `{ua.BRANCH}`: review `git diff "
                f"{state['base'][:12]}...{ua.BRANCH}`, then `{u} accept` or `{u} dismiss`")
    return ""


def parser() -> argparse.ArgumentParser:
    """The CLI, which the skills' `U <cmd>` lines must parse against."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("attention", help="SessionStart hook (payload on stdin)")
    sub.add_parser("_helper", help="detached worker").add_argument("--repo", default="")
    sub.add_parser("batch", help=f"write the audit batch to ./{BATCH_DIR}; print it and the inputs")
    lg = sub.add_parser("ledger", help="the batch's figures and model families, and the last ledger")
    lg.add_argument("--seed", action="store_true", help="write the setup's seed ledger instead")
    lg.add_argument("--models", default="", help="the seed's model families (default: the store's)")
    cm = sub.add_parser("commit", help="commit the named paths without the repo's hooks (no review)")
    cm.add_argument("-m", dest="msg", required=True, help="subject must contain [context-kit]")
    cm.add_argument("paths", nargs="+")
    ac = sub.add_parser("accept", help="take the audit waiting on context/audit: merge it, or push "
                                       "it for a PR; <n>y and <n>n answer its owner items")
    ac.add_argument("answers", nargs="*", help="<item number>y or <item number>n")
    sub.add_parser("dismiss", help="drop the waiting audit (or clear a stopped streak); go idle")
    _tier_parsers(sub)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd in TIER_COMMANDS:  # tools, not hooks: a refusal must reach the caller
        return _tier_command(args)
    if args.cmd in ("accept", "dismiss"):  # the owner's tools: a refusal is an exit code
        import upkeep_audit as ua
        root = _kit.repo_root().resolve()
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        return ua.accept(root, args.answers, _kit.config(root)) if args.cmd == "accept" \
            else ua.dismiss(root)
    if args.cmd == "commit":  # a tool, not a hook: a refusal must reach the caller
        try:
            done = commit(_kit.repo_root().resolve(), args.msg, args.paths)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr)
        return done.returncode
    if args.cmd == "ledger" and args.seed:  # a tool, not a hook: a refusal must reach the caller
        import upkeep_tiers as ut
        root = _kit.repo_root().resolve()
        try:
            path = seed(root, args.models)
        except ut.GuardError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3
        print(json.dumps({"seed": path.relative_to(root).as_posix()}))
        return 0
    if args.cmd in ("batch", "ledger"):
        root = _kit.repo_root().resolve()
        if args.cmd == "batch" and os.environ.get("CONTEXT_KIT_HEADLESS") != "1":
            why = _headless_pending(root)
            if why:  # two audits of one batch pay twice and can disagree
                print(f"refused: {why}", file=sys.stderr)
                return 3
        out = batch(root) if args.cmd == "batch" else ledger(root)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False))
        return 0
    try:
        if args.cmd == "attention":
            attention()
        elif args.cmd == "_helper":
            helper(args.repo or _kit.repo_root())
    except Exception:  # a hook that dies on its own bookkeeping blocks nobody
        _log(getattr(args, "repo", "") or _kit.repo_root(), f"{args.cmd}: {traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
