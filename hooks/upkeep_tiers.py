"""Context tiers: where a lesson lives, and the guarded moves between the places.

  live      rules files and module CLAUDE.md bullets: loaded as today
  overflow  docs/context/overflow/kit-<slug>.md, reached by one pointer line: read on demand
  archive   docs/references/lessons-archive/kit-<slug>.md, plus _gaps.md: never loaded

WHY THE GUARDS ARE CODE. An audit that judges a line obsolete is usually right and sometimes
badly wrong, and the line it drops is the one that would have stopped the next mistake. So no
line leaves the live tier on a judgement alone: each reason must carry evidence a script can
check (a cited file that exists, another live ID, the phrase in the line), and a line the owner
has already restored cannot be judged out again. Evictions for budget take the line the rule
picks, not the one the agent likes least. `owner=True` is the owner's approval: it skips the
judgement guards, never the mechanical ones, and a headless run cannot claim it.

WHY ENTRIES, NOT DELETIONS. A moved line keeps its history as an entry, one per ID:

    ## s:<id> — <title>
    - line: <first wording>
    - from: <path>#<section>
    - 2026-10-31 moved archive · enforced · cite hooks/x.py::merge
    - 2026-11-12 restored · <session> · <turn ts> · "<quote>"

so a restore knows where the line came from, a revision is capped against the FIRST wording
(rewording can not grow a line 20% at a time), and demotion can see what was restored lately.
A line carries its `[s:<id>]` tag only once it has such a history.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import _kit

MOVE_CAP_PCT = 10     # one headless run's net removals, as % of the live lines at its base
PROTECT_DAYS = 90     # a restore this recent protects a line from eviction
QUOTE_MAX = 200
GROWTH_MAX = 0.20     # a line may grow 20% over its first wording
ID_MAX = 20
PIN_CAP = 0.25        # pinned lines' share of a file's budget (checked by context_check)

OVERFLOW_DIR = "docs/context/overflow"
ARCHIVE_DIR = "docs/references/lessons-archive"
GAPS = ARCHIVE_DIR + "/_gaps.md"
REASONS = ("stale", "derivable", "enforced", "duplicate", "conflict", "old-model", "evicted")
JUDGED = ("derivable", "duplicate", "enforced", "old-model")
SOURCES = ("session", "sync")
POINTER = "- More lines for this file, read on demand: `{}`"

TAG = re.compile(r"\[s:([a-z0-9-]{1,20})( pin)?\]\s*$")
_POINTER = re.compile(r"^- .*" + re.escape(OVERFLOW_DIR) + "/")
_HEAD = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_ENTRY = re.compile(r"^## s:([a-z0-9-]{1,20})\s+[—-]\s*(.*)$", re.MULTILINE)
_FIELD = re.compile(r"^- (line|from|source):\s?(.*)$")
_ROW = re.compile(r"^- \d{4}-\d\d-\d\d(?:T\d\d:\d\d:\d\dZ)? \S")  # move rows carry the time
_TOMB = re.compile(r"^tombstone:\s*(.+?)\s*→\s*(.+?)\s*$", re.MULTILINE)
_PROTECTED = re.compile(r"(?i)\b(hard rules|safety)\b")
_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|passw(or)?d)\b\s*[:=]\s*\S{8,}|sk-[A-Za-z0-9_-]{20,}"
    r"|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}")


class GuardError(Exception):
    """A move its guard refused. The CLI prints the message and exits 3."""


@dataclass
class LiveLine:
    path: str          # repo-relative, posix
    section: str       # the nearest heading above it
    index: int         # line number in the LF-split file, from 0
    text: str          # without `- ` and without the tag
    id: str | None
    pinned: bool
    protected: bool


@dataclass
class Entry:
    id: str
    title: str
    line: str          # the first wording
    source: str        # `path#section`; for a gap, session|sync
    rows: list         # event rows, without the leading `- `
    file: str          # the tier file holding it, repo-relative ("" before it is placed)
    tier: str          # overflow | archive | gap
    more: list = field(default_factory=list)  # the bullet's indented lines, verbatim


# --- small helpers ---------------------------------------------------------------------------

def _fold(s: str) -> str:
    return s.casefold() if os.name == "nt" or sys.platform == "darwin" else s


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _day(when) -> date:
    return when.date() if isinstance(when, datetime) else when


def _stamp(when) -> str:
    """A move row's UTC time, so a same-day restore can prove its turn came after the move. A
    plain date stamps midnight; the CLI always passes the current time."""
    if isinstance(when, datetime):
        return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{when}T00:00:00Z"


def _rel(root: Path, path) -> str:
    p = Path(path)
    p = (p if p.is_absolute() else root / p).resolve()
    s, r = str(p), str(root)
    if _fold(s) == _fold(r) or not _fold(s).startswith(_fold(r) + os.sep):
        raise GuardError(f"{path} is not inside the repo")
    return s[len(r) + 1:].replace(os.sep, "/")


def _read(path) -> str:
    """A text file as LF text, "" when it does not exist. CRLF reads the same as LF, and a BOM
    is dropped (so it is not written back)."""
    try:
        text = Path(path).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except FileNotFoundError:
        return ""
    return text[1:] if text.startswith("\ufeff") else text


def _write_all(files: dict) -> None:
    """Scan what every write ADDS (lines the file does not already hold) before writing any, so a
    refusal leaves every file as it was, and text already in a file never blocks a later move
    out of it. Then write LF."""
    for path, text in files.items():
        have = set(_read(path).split("\n"))
        scan_write("\n".join(line for line in text.split("\n") if line not in have))
    for path, text in files.items():
        _kit.atomic_write(path, text)


def _glob_re(glob: str):
    """A `paths:` glob as a regex: `**` crosses directories, `*` does not."""
    out = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    out = out.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.compile(out + r"\Z", re.IGNORECASE if _fold("A") == "a" else 0)


def _quote(q: str) -> str:
    return " ".join(str(q or "").split())[:QUOTE_MAX]


def _clean(text: str) -> str:
    text = " ".join(str(text).split())
    return text[2:] if text.startswith("- ") else text


def _title(text: str) -> str:
    return text if len(text) <= 60 else text[:57].rstrip() + "..."


def _check_owner(owner: bool) -> None:
    if owner and os.environ.get("CONTEXT_KIT_HEADLESS") == "1":
        raise GuardError("--owner is refused in a headless run: no owner is there to approve")


def scan_write(text: str) -> None:
    """Refuse text that must never reach a tier file: a BOM, a mojibake marker, a secret."""
    if "\ufeff" in text:
        raise GuardError("refused: a byte-order mark (BOM) in the text")
    data = text.encode("utf-8", errors="replace")
    if any(m in data for m in _kit.MOJIBAKE):
        raise GuardError("refused: a mojibake marker (a cp1252 round-trip) in the text")
    m = _SECRET.search(text)
    if m:
        raise GuardError(f"refused: text at character {m.start()} looks like a secret")


def path_slug(rel) -> str:
    """`.claude/rules/git-workflow.md` -> `claude-rules-git-workflow`: a source's tier file name."""
    s = re.sub(r"\.md$", "", str(rel).replace("\\", "/"), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def tier_file(tier, source) -> str:
    """The kit's own file for `source` in `tier`: `<dir>/kit-<path-slug>.md`, or `_gaps.md`. The
    `kit-` prefix keeps it off any pre-kit file of the same slug, which the kit never writes into."""
    if tier == "gap":
        return GAPS
    return f"{OVERFLOW_DIR if tier == 'overflow' else ARCHIVE_DIR}/kit-{path_slug(source)}.md"


# --- the live tier ---------------------------------------------------------------------------

def _special(root: Path) -> set:
    """The files whose lines never move: the root CLAUDE.md, and `contract_path` when a repo
    names one more (a file of standing instructions it keeps outside the tiers)."""
    cfg = _kit.config(root)
    return {_fold(p) for p in (cfg["contract_path"], cfg["root_claude_md"]) if p}


def module_files(root, cfg=None) -> list[str]:
    """Module CLAUDE.md files, repo-relative: what `claude_md_globs` matches among the tracked
    (and not-yet-ignored new) files, minus the root CLAUDE.md and anything under `.claude/`.
    Listing from git, not walking the tree, keeps a `**/CLAUDE.md` glob out of node_modules."""
    root = Path(root).resolve()
    cfg = cfg or _kit.config(root)
    names = _kit.git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    pats, skip = [_glob_re(g) for g in cfg["claude_md_globs"]], _fold(cfg["root_claude_md"])
    return sorted({n for n in names.stdout.split("\0") if n and any(r.match(n) for r in pats)
                   and not n.startswith(".claude/") and _fold(n) != skip and (root / n).is_file()})


def live_files(root) -> list[str]:
    """Rules files and module CLAUDE.md files, repo-relative."""
    root = Path(root).resolve()
    cfg, special = _kit.config(root), _special(root)
    found = set(module_files(root, cfg))
    found.update(p.relative_to(root).as_posix() for p in root.glob(cfg["rules_glob"])
                 if p.is_file())
    return sorted(r for r in found if _fold(r) not in special)


def _body_start(lines: list) -> int:
    """The first line after YAML front matter (whose `- ` items are not lines)."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def _parse_text(rel: str, text: str, whole_protected: bool) -> list[LiveLine]:
    lines, out, fence, heads, section = text.split("\n"), [], False, {}, ""
    for i in range(_body_start(lines), len(lines)):
        line = lines[i]
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        m = None if fence else _HEAD.match(line)
        if m:
            level = len(m.group(1))
            heads = {k: v for k, v in heads.items() if k < level}
            heads[level] = section = m.group(2)
            continue
        if fence or not line.startswith("- ") or _POINTER.match(line):
            continue
        body = line[2:].rstrip()
        t = TAG.search(body)
        pinned = bool(t and t.group(2))
        out.append(LiveLine(rel, section, i, body[:t.start()].rstrip() if t else body,
                            t.group(1) if t else None, pinned,
                            whole_protected or pinned
                            or any(_PROTECTED.search(h) for h in heads.values())))
    return out


def parse_live(root, path) -> list[LiveLine]:
    """Top-level `- ` bullets outside fences, front matter and the pointer line. CRLF-tolerant."""
    root = Path(root).resolve()
    rel = _rel(root, path)
    return _parse_text(rel, _read(root / rel), _fold(rel) in _special(root))


def _all_live(root: Path, with_protected: bool = False) -> list[LiveLine]:
    rels = live_files(root)
    if with_protected:  # so a root-file line is refused as protected, not as unknown
        cfg = _kit.config(root)
        rels += [p for p in (cfg["contract_path"], cfg["root_claude_md"]) if p and (root / p).is_file()]
    return [line for rel in rels for line in parse_live(root, rel)]


def _live_ids(root: Path) -> set:
    return {line.id for line in _all_live(root) if line.id}


def _find_live(root: Path, ref: str) -> LiveLine | None:
    """A live line by ID or by its exact text (tag-free)."""
    ref = _clean(ref)
    hits = [line for line in _all_live(root, True) if ref in (line.id, line.text)]
    if len(hits) > 1:
        raise GuardError(f"{len(hits)} live lines read {ref!r}: name one by its ID")
    return hits[0] if hits else None


def _children(lines: list, i: int) -> list:
    """The indented lines under the bullet at `i` (continuations, nested bullets): they move with
    it as one block. Blank lines followed by more indented lines are the bullet's too (a loose
    list): a live bullet starts at column 0, so any indented block is deeper than it."""
    j = k = i + 1
    while k < len(lines) and (lines[k][:1] in (" ", "\t") or not lines[k].strip()):
        k += 1
        if lines[k - 1].strip():
            j = k
    return lines[i + 1:j]


def _insert(lines: list, section: str, block: list) -> list:
    """`block` at the top of `section`, below its pinned lines; a missing section is appended.
    Two sections of that heading are refused: picking one would be a guess."""
    start = _body_start(lines)
    fence, same = False, 0
    for line in lines[start:]:
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        m = None if fence else _HEAD.match(line)
        same += bool(section and m and m.group(2) == section)
    if same > 1:
        raise GuardError(f"{same} sections are headed {section!r}: rename one, or put the line "
                         "back by hand")
    head, pos, fence = (start - 1 if not section else None), None, False
    for i in range(start, len(lines)):
        line = lines[i]
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        m = None if fence else _HEAD.match(line)
        if head is None:
            if m and m.group(2) == section:
                head = i
            continue
        if m:
            break
        if not fence and line.startswith("- ") and not _POINTER.match(line):
            t = TAG.search(line)
            if t and t.group(2):
                pos = i + 1 + len(_children(lines, i))
                continue
            pos = pos or i
            break
    if head is None:
        while lines and lines[-1] == "":
            lines = lines[:-1]
        return lines + ["", f"## {section}", *block, ""]
    pos = head + 1 if pos is None else pos
    return lines[:pos] + block + lines[pos:]


def _paths_globs(text: str) -> list[str]:
    """The `paths:` front-matter globs of a rules file, in list or inline form."""
    if not text.startswith("---"):
        return []
    m = re.search(r"(?m)^paths:[ \t]*(.*)$((?:\n[ \t]*-.*)*)", text.split("---", 2)[1])
    if not m:
        return []
    items = [s.strip().lstrip("-") for s in m.group(2).split("\n")] + re.split(r"[,\[\]]", m.group(1))
    return [s.strip().strip("'\"") for s in items if s.strip().strip("'\"")]


# --- entries ---------------------------------------------------------------------------------

def _tier_files(root: Path):
    for d, tier in ((OVERFLOW_DIR, "overflow"), (ARCHIVE_DIR, "archive")):
        for p in sorted((root / d).glob("*.md")):
            yield p, ("gap" if p.name == "_gaps.md" else tier)


def _is_legacy(text: str) -> bool:
    return any(line.startswith("legacy:") for line in text.split("\n")[:5])


def _split(text: str) -> tuple[str, list]:
    """(the header, one chunk per `## s:` entry)."""
    parts = re.split(r"(?m)^(?=## s:)", text)
    return parts[0], parts[1:]


def _cid(chunk: str) -> str:
    m = _ENTRY.match(chunk)
    return m.group(1) if m else ""


def _parse_entry(chunk: str, file: str, tier: str) -> Entry | None:
    lines = chunk.rstrip("\n").split("\n")
    m = _ENTRY.match(lines[0])
    if not m:
        return None
    fields, rows, more, last = {}, [], [], ""
    for line in lines[1:]:
        f = _FIELD.match(line)
        if f:
            fields.setdefault(f.group(1), f.group(2))
            last = f.group(1)
        elif line[:1] in (" ", "\t") and last == "line":  # the bullet's own indented lines
            more.append(line)
        elif _ROW.match(line):
            rows.append(line[2:])
            last = ""
    return Entry(m.group(1), m.group(2), fields.get("line", ""),
                 fields.get("source" if tier == "gap" else "from", ""), rows, file, tier, more)


def _render(e: Entry, tier: str) -> str:
    head = [f"## s:{e.id} — {e.title}"]
    head += [f"- source: {e.source}"] if tier == "gap" else [f"- line: {e.line}", *e.more,
                                                              f"- from: {e.source}"]
    return "\n".join(head + [f"- {r}" for r in e.rows]) + "\n\n"


def load_entries(root) -> list[Entry]:
    """Every entry in the overflow and archive files (legacy prose excluded), in file order."""
    root = Path(root).resolve()
    out = []
    for p, tier in _tier_files(root):
        text = _read(p)
        if not _is_legacy(text):
            rel = p.relative_to(root).as_posix()
            out += [e for e in (_parse_entry(c, rel, tier) for c in _split(text)[1]) if e]
    return out


def _entry(root: Path, eid: str) -> Entry | None:
    """The entry for `eid`; a line's history wins over a gap of the same ID."""
    return next((e for e in sorted(load_entries(root), key=lambda e: e.tier == "gap")
                 if e.id == eid), None)


def tombstones(root) -> dict:
    """{old path: new path, or None when deleted}, from `tombstone: <old> → <new|deleted>`."""
    root = Path(root).resolve()
    return {old: (None if new == "deleted" else new)
            for p, _ in _tier_files(root) for old, new in _TOMB.findall(_read(p))}


def legacy_files(root) -> list[str]:
    root = Path(root).resolve()
    return [p.relative_to(root).as_posix() for p, _ in _tier_files(root) if _is_legacy(_read(p))]


def _resolve(root: Path, rel: str) -> str | None:
    """Follow tombstones from `rel` to where it lives now; None when it was deleted."""
    tombs, seen = {_fold(k): v for k, v in tombstones(root).items()}, set()
    while rel is not None and _fold(rel) in tombs and _fold(rel) not in seen:
        seen.add(_fold(rel))
        rel = tombs[_fold(rel)]
    return rel


def _rows(e: Entry, event: str) -> list[str]:
    return [r for r in e.rows if r.split(" ", 2)[1:2] == [event]]


def restores(entry: Entry, today: date) -> int:
    """Restores of `entry` in the last PROTECT_DAYS."""
    cut = (_day(today) - timedelta(days=PROTECT_DAYS)).isoformat()
    return sum(1 for r in _rows(entry, "restored") if r[:10] >= cut)


_NL = "↵"  # joins a revised block's lines inside one `revised` row


def _revised(e: Entry):
    """The latest revised block's lines (indentation kept), or None when never revised. The block
    starts at the first ' → ' of the row: the quote is stripped of that string on write."""
    rows = _rows(e, "revised")
    return rows[-1].split(" → ", 1)[1].split(_NL) if rows and " → " in rows[-1] else None


def _current(e: Entry) -> str:
    """The latest wording of the bullet itself."""
    return (_revised(e) or [e.line])[0]


def _current_more(e: Entry) -> list:
    """The latest wording's continuation lines."""
    rev = _revised(e)
    return list(e.more) if rev is None else rev[1:]


def _header(tier: str, source: str) -> str:
    src = source.split("#")[0]
    return {"overflow": f"# Overflow: `{src}`\n\nLines moved out of `{src}` for its budget. "
                        "Read them when the work touches them.\n\n",
            "archive": f"# Lessons archive: `{src}`\n\nNever loaded as context. One entry per "
                       "line ID: where it came from and what happened to it.\n\n",
            "gap": "# Gaps\n\nLessons seen in sessions that no live line covers yet.\n\n"}[tier]


def _place(root: Path, entry: Entry, tier: str, out: dict) -> dict:
    """Add to `out` ({path: text}) the writes that leave `entry` once, in its `tier` file."""
    target = tier_file(tier, entry.source.split("#")[0])

    def text_of(rel):
        return out.get(root / rel, _read(root / rel))
    if entry.file and entry.file != target:
        head, chunks = _split(text_of(entry.file))
        out[root / entry.file] = head + "".join(c for c in chunks if _cid(c) != entry.id)
    head, chunks = _split(text_of(target) or _header(tier, entry.source))
    chunks = [c.rstrip("\n") + "\n\n" for c in chunks]
    block, ids = _render(entry, tier), [_cid(c) for c in chunks]
    if entry.id in ids:
        chunks[ids.index(entry.id)] = block
    else:
        chunks.insert(0, block)  # newest first: the bottom entry is the oldest
    out[root / target] = (head.rstrip("\n") + "\n\n" + "".join(chunks)).rstrip("\n") + "\n"
    entry.file, entry.tier = target, tier
    return out


# --- IDs, lookup, demotion -------------------------------------------------------------------

def _ids(root: Path) -> set:
    return {e.id for e in load_entries(root)} | _live_ids(root)


def _mint(text: str, taken: set) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower()) or ["line"]
    slug = words[0][:ID_MAX]
    for w in words[1:]:
        if len(slug) + 1 + len(w) > ID_MAX:
            break
        slug += "-" + w
    cand, n = slug, 2
    while cand in taken:
        suffix = f"-{n}"
        cand, n = slug[:ID_MAX - len(suffix)].rstrip("-") + suffix, n + 1
    taken.add(cand)
    return cand


def mint_id(root, text: str) -> str:
    """A new, never-used ID: the first words of `text` as kebab case, at most ID_MAX chars."""
    return _mint(text, _ids(Path(root).resolve()))


def lookup(root, words, files=()) -> list[dict]:
    """Every live line, entry, legacy line and tombstone holding all of `words` (any case). With
    `files`, only those whose source is one of them, through tombstones."""
    root = Path(root).resolve()
    words = [w.casefold() for w in words]
    want = {_fold(_resolve(root, _rel(root, f)) or _rel(root, f)) for f in files}

    def keep(text, *paths):
        return all(w in text.casefold() for w in words) and (
            not want or any(p and _fold(_resolve(root, p) or p) in want for p in paths))
    hits, live = [], set()
    for line in _all_live(root):
        live.add(line.id)
        if keep(line.text, line.path):
            hits.append({"tier": "live", "id": line.id, "text": line.text,
                         "source": f"{line.path}#{line.section}", "file": line.path})
    for e in load_entries(root):
        if keep(" ".join([e.title, e.line, *e.rows]), e.source.split("#")[0]):
            hits.append({"tier": e.tier, "id": e.id, "text": _current(e) or e.title,
                         "source": e.source, "file": e.file, "rows": e.rows, "live": e.id in live})
    for rel in legacy_files(root):
        hits += [{"tier": "legacy", "text": line.strip(), "file": rel}
                 for line in _read(root / rel).split("\n") if line.strip() and keep(line)]
    for old, new in tombstones(root).items():
        text = f"{old} → {new or 'deleted'}"
        if keep(text, old, new):
            hits.append({"tier": "tombstone", "text": text})
    return hits


def pick_demotion(lines_or_entries, entries, today: date):
    """The bottom-most unpinned candidate with no restore in PROTECT_DAYS, else the one with the
    fewest such restores (ties to the bottom-most). None when nothing may move."""
    hist = {e.id: e for e in entries}
    cands = [x for x in lines_or_entries if not getattr(x, "protected", False)]
    if not cands:
        return None
    return min(reversed(cands), key=lambda x: restores(hist[x.id], today) if x.id in hist else 0)


# --- the guards ------------------------------------------------------------------------------

def _must_exist(root: Path, cite: str) -> None:
    """A cited file (not a directory) inside the repo, holding `symbol` when one is named."""
    path, _, symbol = cite.partition("::")
    p = (root / path.strip()).resolve()
    if not path.strip() or not p.is_file() or not _fold(str(p)).startswith(_fold(str(root)) + os.sep):
        raise GuardError(f"the cited path {path.strip()!r} is not a file in the repo")
    if symbol and symbol.strip() not in _read(p):
        raise GuardError(f"{symbol.strip()!r} is not in {path.strip()}")


def _evidence(root: Path, reason: str, ev: str, text: str, own, owner: bool):
    """(the evidence row text, the partner LiveLine when it was named by text), or GuardError
    when the evidence does not hold. `own` is the line (or entry) being moved."""
    ev = (ev or "").strip()
    if reason == "evicted":
        return "budget", None
    if reason in ("duplicate", "conflict"):
        # an untagged partner is named by its exact text: it is tagged by the move
        partners = [p for p in _all_live(root) if _clean(ev) in (p.id, p.text) and p != own]
        known = set() if reason == "duplicate" else {e.id for e in load_entries(root)}
        if len(partners) > 1:
            raise GuardError(f"{len(partners)} live lines read {ev!r}: name the partner by its ID")
        if not ev or ev == getattr(own, "id", None) or not (partners or ev in known):
            raise GuardError(f"{reason} needs the other line: a "
                             f"{'live ' if reason == 'duplicate' else ''}ID, or its exact text")
        if reason == "conflict" and not owner:
            raise GuardError("a conflict is never moved without the owner (--owner)")
        return (partners[0].id or "", partners[0]) if partners else (ev, None)
    return _plain_evidence(root, reason, ev, text), None


def _is_path(tok: str) -> bool:
    """A path-shaped token: no spaces, and a '/' or a file extension (not a plain word)."""
    return not re.search(r"\s", tok) and ("/" in tok or bool(re.search(r"\.[A-Za-z]\w{0,7}$", tok)))


def _resolves(root: Path, tok: str) -> bool:
    """Exists from the root, or as the tail of any repo file (a module-relative path)."""
    if (root / tok).exists():
        return True
    files = _kit.git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return tail_match(files.stdout.split("\0"), tok)


def tail_match(files, tok: str) -> bool:
    """Is `tok` the tail of any of `files` (a module-relative path)?"""
    tail = _fold("/" + (tok[2:] if tok.startswith("./") else tok).replace("\\", "/"))
    return any(_fold("/" + f).endswith(tail) for f in files if f)


def _plain_evidence(root: Path, reason: str, ev: str, text: str) -> str:
    if reason in ("derivable", "enforced"):
        cite = ev[5:].strip() if ev.startswith("cite ") else ev
        if not cite:
            raise GuardError(f"{reason} needs --evidence 'cite <path[::symbol]>'")
        _must_exist(root, cite)
        return f"cite {cite}"
    if reason == "stale":
        if ev and (_fold(ev) in {_fold(k) for k in tombstones(root)}
                   or (ev in text and _is_path(ev) and not _resolves(root, ev))):
            return ev
        raise GuardError("stale needs a dangling path the line names (a token with a '/' or a "
                         "file extension that exists nowhere in the repo), or a tombstoned path")
    if not ev or ev.casefold() not in text.casefold():  # old-model
        raise GuardError("old-model needs the compensation phrase, as it appears in the line")
    return ev


def live_at(root: Path, sha: str) -> list[LiveLine]:
    """The live lines of the commit `sha` (what `_all_live` reads from the working tree)."""
    cfg, special = _kit.config(root), _special(root)
    rules, mods = _glob_re(cfg["rules_glob"]), [_glob_re(g) for g in cfg["claude_md_globs"]]
    names = _kit.git(root, "ls-tree", "-r", "-z", "--name-only", sha).stdout.split("\0")
    out: list[LiveLine] = []
    for rel in names:
        if rel and (rules.match(rel) or (not rel.startswith(".claude/")
                                         and any(g.match(rel) for g in mods))) \
                and _fold(rel) not in special:
            out += _parse_text(rel, _kit.git(root, "show", f"{sha}:{rel}").stdout
                               .replace("\r\n", "\n"), False)
    return out


def _live_at(root: Path, sha: str) -> int:
    return len(live_at(root, sha))


def _cap(root: Path) -> None:
    """Headless only: one run's net removals (live lines at its base minus live lines after this
    move, so a restore gives one back) stay within MOVE_CAP_PCT of the base."""
    if os.environ.get("CONTEXT_KIT_HEADLESS") != "1":
        return
    try:
        base = json.loads(_read(_kit.upkeep_dir(root) / "state.json") or "{}").get("base")
    except (ValueError, AttributeError):
        base = None
    if not base:
        return  # Stage A never runs headless; Stage B sets the base before any move
    at_base, now = _live_at(root, base), len(_all_live(root))
    limit = math.ceil(MOVE_CAP_PCT * at_base / 100)
    if at_base - (now - 1) > limit:
        raise GuardError(f"net-removal cap: {at_base - now + 1} removals > {limit} "
                         f"({MOVE_CAP_PCT}% of {at_base} live lines at the base)")


def _touched(root: Path, record: dict, src: str) -> bool:
    """Did the session touch what loads `src`: its `paths:`, or a module CLAUDE.md's directory?
    A rules file with no `paths:` loads in every session, so any session qualifies."""
    globs = [] if src.endswith("CLAUDE.md") else _paths_globs(_read(root / src))
    if not src.endswith("CLAUDE.md") and not globs:
        return True
    rels = []
    for f in record.get("files") or []:
        for base in (root, Path(record.get("cwd") or root)):  # a worktree session's paths too
            p = Path(f) if Path(f).is_absolute() else base / f
            s, b = _fold(str(p.resolve())), _fold(str(base.resolve()))
            if s.startswith(b + os.sep):
                rels.append(s[len(b) + 1:].replace(os.sep, "/"))
                break
    if src.endswith("CLAUDE.md"):
        d = _fold(src.rsplit("/", 1)[0]) + "/" if "/" in src else ""
        return any(r.startswith(d) for r in rels)
    return any(_glob_re(g).match(r) for g in globs for r in rels)


def _result(eid: str, needs_owner=None, second_sighting=False) -> dict:
    return {"id": eid, "needs_owner": needs_owner, "second_sighting": second_sighting}


def _tagged(line: LiveLine, lid: str) -> str:
    return f"- {line.text} [s:{lid}{' pin' if line.pinned else ''}]"


def _history(root: Path, ref: str, owner: bool, lid: str | None = None,
             line: LiveLine | None = None):
    """(line, its entry or a new one, the file's lines with the line tagged) for a live line."""
    line = line or _find_live(root, ref)
    if line is None:
        raise GuardError(f"no live line is {ref!r}")
    if _fold(line.path) in _special(root):  # the protected files are never tagged
        raise GuardError(f"{line.path} is never tagged or changed by the upkeep")
    if line.protected and not owner:
        raise GuardError(f"{line.path}: {line.text!r} is protected")
    lid = line.id or lid or mint_id(root, line.text)
    entry = _entry(root, lid) or Entry(lid, _title(line.text), line.text,
                                       f"{line.path}#{line.section}", [], "", "archive")
    lines = _read(root / line.path).split("\n")
    lines[line.index] = _tagged(line, lid)
    return line, entry, lines


# --- the moves -------------------------------------------------------------------------------

def move(root, ref, tier, reason, evidence="", today=None, owner=False) -> dict:
    """Move a live line (by ID or exact text) to overflow or archive, or an overflow entry to the
    archive. Mints the line's ID on its first move."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    if tier not in ("overflow", "archive"):
        raise GuardError(f"tier must be overflow or archive, not {tier!r}")
    if reason not in REASONS:
        raise GuardError(f"reason must be one of {', '.join(REASONS)}")
    # exact twins: the bottom-most moves as a duplicate of the top-most
    twins = [t for t in _all_live(root, True) if t.text == _clean(ref)] if reason == "duplicate" \
        else []
    line = twins[-1] if len(twins) > 1 else _find_live(root, ref)
    entry = _entry(root, line.id) if line and line.id else None
    if line is None:
        entry = _entry(root, _clean(ref))
        if not entry or entry.tier != "overflow" or tier != "archive":
            raise GuardError(f"no live line or overflow entry is {ref!r}")
    elif line.protected:
        raise GuardError(f"{line.path}: {line.text!r} is protected (the root CLAUDE.md, "
                         "`contract_path`, Hard Rules, Safety and pinned lines never move)")
    text = line.text if line else _current(entry)
    if len(twins) > 1 and _clean(evidence or ref) in (line.text, twins[0].id):
        ev, partner = twins[0].id or "", twins[0]
    else:
        ev, partner = _evidence(root, reason, evidence, text, line or entry, owner)
    if entry and reason in JUDGED and _rows(entry, "restored") and not owner:
        raise GuardError(f"{entry.id} was restored: {reason} needs the owner (--owner)")
    if reason == "evicted" and not owner:
        cands = (parse_live(root, line.path) if line
                 else [e for e in load_entries(root) if e.file == entry.file])
        if pick_demotion(cands, load_entries(root), today) != (line or entry):
            raise GuardError("evicted takes only the pick_demotion choice: the bottom-most line "
                             f"with no restore in {PROTECT_DAYS} days, else the fewest")
    out, taken = {}, _ids(root)
    lid = (line.id or _mint(line.text, taken)) if line else entry.id
    if partner and not partner.id:  # tag the partner, so the row names an ID that resolves
        ev = _mint(partner.text, taken)
        plines = _read(root / partner.path).split("\n")
        plines[partner.index] = _tagged(partner, ev)
        out[root / partner.path] = "\n".join(plines)
    if line:
        _cap(root)
        _, entry, lines = _history(root, ref, owner, lid, line)
        if partner and partner.path == line.path:  # both edits on one copy of the file
            lines[partner.index] = _tagged(partner, ev)
        kids = _children(lines, line.index)
        if not entry.more and not _rows(entry, "revised"):  # the first wording's block, kept whole
            entry.more = kids
        del lines[line.index:line.index + 1 + len(kids)]
        pointer = POINTER.format(tier_file("overflow", line.path))
        if tier == "overflow" and not any(_POINTER.match(x) for x in lines):
            while lines and lines[-1] == "":
                lines.pop()
            lines += ["", pointer, ""]
        out[root / line.path] = "\n".join(lines)
    entry.rows.append(f"{_stamp(today)} moved {tier} · {reason} · {ev}")
    _write_all(_place(root, entry, tier, out))
    return _result(entry.id)


def restore(root, id, record=None, turn_ts="", quote="", today=None, text=None,
            owner=False) -> dict:
    """Put an archived or overflowed line back, tagged, at the top of its section."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    entry = _entry(root, id)
    if not entry or entry.tier == "gap":
        raise GuardError(f"no archive or overflow entry is {id!r}")
    if id in _live_ids(root):
        raise GuardError(f"{id} is already live")
    src, _, section = entry.source.partition("#")
    path = _resolve(root, src)
    if path is None or not (root / path).is_file():
        raise GuardError(f"{src} is gone (tombstoned or deleted): restore it by hand")
    text = _clean(text or _current(entry))
    if not owner:
        if not record:
            raise GuardError("a restore needs the session record (or --owner)")
        if turn_ts[:19] not in {str(t.get("ts", ""))[:19] for t in record.get("turns") or []}:
            raise GuardError(f"{turn_ts} is not an owner turn of session {record.get('id')}")
        moved = _rows(entry, "moved")
        stamp = moved[-1].split(" ", 1)[0] if moved else ""
        turn = turn_ts[:19] if len(stamp) > 10 else turn_ts[:10]  # old rows hold a date only
        if stamp and turn <= stamp[:19]:
            raise GuardError(f"the turn ({turn_ts}) is not after the last move ({stamp})")
        if not _touched(root, record, path):
            raise GuardError(f"session {record.get('id')} touched nothing that loads {path}")
        if len(text) > len(entry.line) * (1 + GROWTH_MAX):
            raise GuardError(f"{len(text)} chars is over {GROWTH_MAX:.0%} longer than the first "
                             f"wording ({len(entry.line)})")
        conflict = [r for r in moved if r.split(" · ")[1:2] == ["conflict"]]
        partner = conflict[-1].split(" · ")[2] if conflict else ""
        if partner and partner in _live_ids(root):
            raise GuardError(f"its conflict partner {partner} is still live")
        entry.rows.append(f'{_day(today)} restored · {record.get("id")} · {turn_ts} · '
                          f'"{_quote(quote)}"')
    else:
        entry.rows.append(f"{_day(today)} restored · owner")
    entry.source = f"{path}#{section}"
    lines = _insert(_read(root / path).split("\n"), section,
                    [f"- {text} [s:{id}]", *_current_more(entry)])
    _write_all(_place(root, entry, "archive", {root / path: "\n".join(lines)}))
    return _result(id, "second-restore" if len(_rows(entry, "restored")) >= 2 else None)


def revise(root, ref, new_text, record=None, quote="", today=None, owner=False) -> dict:
    """Reword a live line, capped at GROWTH_MAX over its FIRST wording unless the owner says. The
    reword replaces the whole block: new_text's first line is the bullet, any further lines are
    its continuation, and the old continuation goes with the old bullet."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    new = [x for x in str(new_text).replace("\r", "").split("\n") if x.strip()] or [""]
    new = [x.replace(_NL, " ") for x in new]  # the row's own line joiner may not appear in text
    new[0] = _clean(new[0])
    new[1:] = [x.rstrip() if x[:1] in (" ", "\t") else "  " + x.strip() for x in new[1:]]
    line, entry, lines = _history(root, ref, owner)
    old = _children(lines, line.index)
    if old and len(new) == 1:  # a one-line text for a wrapped bullet would drop its other lines
        raise GuardError(f"the line wraps over {1 + len(old)} lines; pass the whole block as "
                         "new_text, one line per line, or its continuation is lost")
    if not entry.more and not _rows(entry, "revised"):  # the first wording's block, kept whole
        entry.more = old
    size = len("\n".join(x.strip() for x in new))
    base = len("\n".join(x.strip() for x in [entry.line, *entry.more]))
    if not owner and size > base * (1 + GROWTH_MAX):
        name = line.id or line.text  # an untagged line's minted ID is not written yet
        raise GuardError(
            f"{size} chars is over {GROWTH_MAX:.0%} longer than the first wording "
            f"({base}). Split it instead: archive it as `stale` "
            f"(move {name!r} archive stale --evidence <path>), then file the rest with `seen`; "
            "or ask the owner (--owner)")
    who = "owner" if owner else (record or {}).get("id") or "audit"
    block = _NL.join(new)  # one row, whatever the line count; the quote may not hold the arrow
    quote = _quote(quote).replace(" → ", " -> ")
    lines[line.index:line.index + 1 + len(old)] = [
        f"- {new[0]} [s:{entry.id}{' pin' if line.pinned else ''}]", *new[1:]]
    entry.rows.append(f'{_day(today)} revised · {who} · "{quote}" → {block}')
    _write_all(_place(root, entry, "archive", {root / line.path: "\n".join(lines)}))
    return _result(entry.id)


def ignored(root, ref, record, turn_ts, quote, today=None, owner=False) -> dict:
    """Note that a session went against a live line. The second time, the owner decides: pin it,
    or give it a mechanism."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    line, entry, lines = _history(root, ref, True)
    entry.rows.append(f'{_day(today)} ignored · {(record or {}).get("id") or "owner"} · {turn_ts} · '
                      f'"{_quote(quote)}"')
    _write_all(_place(root, entry, "archive", {root / line.path: "\n".join(lines)}))
    return _result(entry.id, "second-ignored" if len(_rows(entry, "ignored")) >= 2 else None)


def seen(root, title, source, record=None, quote="", today=None, owner=False) -> dict:
    """Record a lesson no live line covers, in _gaps.md. One sighting per session."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    if source not in SOURCES:
        raise GuardError(f"source must be one of {', '.join(SOURCES)}")
    title = " ".join(str(title).split())
    gap = next((e for e in load_entries(root)
                if e.tier == "gap" and e.title.casefold() == title.casefold()), None)
    gap = gap or Entry(mint_id(root, title), title, "", source, [], "", "gap")
    sid = (record or {}).get("id")
    who = sid or source  # a session sights once; a sync pass once a day
    if not any(r.split(" · ")[1:2] == [who] and (sid or r.startswith(str(_day(today))))
               for r in _rows(gap, "seen")):
        gap.rows.append(f'{_day(today)} seen · {who} · "{_quote(quote)}"')
    _write_all(_place(root, gap, "gap", {}))
    return _result(gap.id, second_sighting=len(_rows(gap, "seen")) >= 2)


def promote(root, gap_id, path, section, text, mechanism, today=None, owner=False) -> dict:
    """Make a gap a live line. It needs a second sighting (the owner can waive that) and an
    existing mechanism behind it: a test, hook or guard (nobody can waive that)."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    gap = _entry(root, gap_id)
    if not gap or gap.tier != "gap":
        raise GuardError(f"no gap is {gap_id!r}")
    if gap_id in _live_ids(root):
        raise GuardError(f"{gap_id} is already live")
    sightings = len(_rows(gap, "seen"))
    if sightings < 2 and not owner:
        raise GuardError(f"{gap_id} was seen {sightings} time(s): a promotion needs a second sighting")
    _must_exist(root, mechanism or "")
    rel = _rel(root, path)
    if _fold(rel) not in {_fold(r) for r in live_files(root)}:
        raise GuardError(f"{rel} is not a live file")
    text = _clean(text)
    lines = _insert(_read(root / rel).split("\n"), section, [f"- {text} [s:{gap_id}]"])
    # the gap becomes the line's history: an archive entry (its sightings kept), so a later move,
    # revise or restore finds a real first wording and source, and _gaps.md no longer holds it
    gap.line, gap.source = text, f"{rel}#{section}"
    gap.rows.append(f"{_day(today)} promoted · {rel}#{section} · mechanism {mechanism}")
    _write_all(_place(root, gap, "archive", {root / rel: "\n".join(lines)}))
    return _result(gap_id, second_sighting=sightings >= 2)


def _pin(root, ref, today, owner: bool, pinned: bool) -> dict:
    """Set or clear a line's pin, the owner's call only, and move the line (with its children)
    to where that leaves it in its section: the end of the pinned block, which is also the top
    of the unpinned lines. A pin past PIN_CAP of the file's budget is refused."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    if not owner:
        raise GuardError("a pin is the owner's call (--owner)")
    line, entry, lines = _history(root, ref, owner)
    if line.pinned == pinned:
        raise GuardError(f"{line.text!r} is {'already' if pinned else 'not'} pinned")
    kids = _children(lines, line.index)
    rest = lines[:line.index] + lines[line.index + 1 + len(kids):]
    block = [f"- {line.text} [s:{entry.id}{' pin' if pinned else ''}]", *kids]
    text = "\n".join(_insert(rest, line.section, block))
    if pinned:
        cfg, new = _kit.config(root), text.split("\n")
        budget = cfg["claude_md_max_chars" if line.path.endswith("CLAUDE.md") else "rules_max_chars"]
        used = sum(len(new[x.index]) + sum(len(c) for c in _children(new, x.index))
                   for x in _parse_text(line.path, text, False) if x.pinned)
        if used > PIN_CAP * budget:  # measured as context_check measures it
            raise GuardError(f"{line.path}: pinned lines would be {used} chars, over "
                             f"{PIN_CAP:.0%} of {budget}")
    entry.rows.append(f"{_day(today)} {'pinned' if pinned else 'unpinned'} · owner")
    _write_all(_place(root, entry, "archive", {root / line.path: text}))
    return _result(entry.id)


def pin(root, ref, today=None, owner=False) -> dict:
    return _pin(root, ref, today, owner, True)


def unpin(root, ref, today=None, owner=False) -> dict:
    return _pin(root, ref, today, owner, False)


def _last_text(root: Path, rel: str) -> str:
    """`rel` as last committed: at HEAD, else just before the commit that deleted it."""
    done = _kit.git(root, "show", f"HEAD:{rel}")
    if done.returncode != 0:
        sha = _kit.git(root, "rev-list", "-n", "1", "HEAD", "--", rel).stdout.strip()
        done = _kit.git(root, "show", f"{sha}^:{rel}") if sha else done
    return done.stdout.replace("\r\n", "\n") if done.returncode == 0 else ""


def tombstone(root, old, new=None, today=None, owner=False) -> dict:
    """Retire a moved or deleted surface: `tombstone: <old> → <new|deleted>` in its archive file.
    A deleted surface's lines (as last committed) are archived as `stale`."""
    root, today = Path(root).resolve(), today or _today()
    _check_owner(owner)
    old = _rel(root, old)
    new = None if new in (None, "", "deleted") else _rel(root, new)
    if new is None and (root / old).exists():
        raise GuardError(f"{old} still exists")
    if new is not None and not (root / new).exists():
        raise GuardError(f"{new} does not exist")
    mark, row = f"tombstone: {old} → {new or 'deleted'}", "moved archive · stale · tombstone"
    entries = {e.id: e for e in load_entries(root)}
    if any(_fold(o) == _fold(old) and n == new for o, n in tombstones(root).items()):
        return {"ids": [i for i, e in entries.items()  # done already: a rerun mints nothing
                        if _fold(e.source.split("#")[0]) == _fold(old) and
                        any(r.endswith(row) for r in e.rows)], "tombstone": mark}
    out, ids, taken = {}, [], _ids(root)
    text = _last_text(root, old) if new is None else ""
    for line in _parse_text(old, text, False):
        lid = line.id or _mint(line.text, taken)
        entry = entries.get(lid) or Entry(lid, _title(line.text), line.text,
                                          f"{old}#{line.section}", [], "", "archive")
        entry.more = _children(text.split("\n"), line.index)
        entry.rows.append(f"{_stamp(today)} {row}")
        _place(root, entry, "archive", out)
        ids.append(lid)
    for tier in ("archive", "overflow"):
        rel = tier_file(tier, old)
        text = out.get(root / rel, _read(root / rel))
        if (text or tier == "archive") and mark not in text:
            head, chunks = _split(text or _header(tier, old))
            out[root / rel] = head.rstrip("\n") + f"\n\n{mark}\n\n" + "".join(chunks)
    _write_all(out)
    return {"ids": ids, "tombstone": mark}


# --- public names for context_check (it reads the tiers, never reaches for the underscores) ---
rows, tier_files, read, children, is_legacy = _rows, _tier_files, _read, _children, _is_legacy
glob_re = _glob_re
