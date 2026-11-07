"""Context-surface check: size budgets, dead routes, orphaned context files, text hygiene.

Run in CI and by `/context-sync` and `/auditing-sessions`. Exit 1 fails the build; notes are
printed and exit 0. Everything it measures is configured in `.claude/context-kit.json` (see
`_kit.py`). Sizes are measured as Claude Code loads the text: a CLAUDE.md's block-level HTML
comments are stripped before injection, so they cost nothing and are not counted.

WHY IT EXISTS. Context files rot in exactly three ways, and none of them is visible in a diff:
they grow past the point where anyone reads them, they point at files that have been moved or
deleted, and they accumulate surfaces nothing routes to. All three are mechanical, so a hook
catches them and a rules file does not.
"""
from __future__ import annotations

import re
import sys
from fnmatch import fnmatch
from pathlib import Path

import upkeep_tiers as ut
from _kit import MOJIBAKE, config, repo_root

ROOT = repo_root()
CFG = config()

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


# A block-level HTML comment: Claude Code strips these from CLAUDE.md files before loading them,
# except inside a code fence. Inline comments are counted, so the measure never undercounts.
_BLOCK_COMMENT_RE = re.compile(r"(?m)^[ \t]*<!--(?:(?!-->).)*-->[ \t]*(?:\n|\Z)", re.DOTALL)
_FENCED_RE = re.compile(r"(```.*?```)", re.DOTALL)


def loaded_text(path: Path) -> str:
    """`path` as Claude Code injects it: a CLAUDE.md loses its block-level HTML comments."""
    text = path.read_text(encoding="utf-8")
    if path.name != "CLAUDE.md":
        return text
    return "".join(part if part.startswith("```") else _BLOCK_COMMENT_RE.sub("", part)
                   for part in _FENCED_RE.split(text))


# --------------------------------------------------------------------------- size budgets

def check_size_limits() -> list[str]:
    """Line budgets for the always-loaded files, character budgets for the on-demand ones.

    Characters, not lines, for anything with a per-file budget: a line cap is satisfied by
    writing longer lines."""
    errors: list[str] = []
    for rel, max_lines in CFG["size_limits"].items():
        for path in sorted(ROOT.glob(rel)):
            n = len(loaded_text(path).splitlines())
            if n > max_lines:
                errors.append(f"  {path.relative_to(ROOT).as_posix()}: {n} lines > {max_lines}")
    errors += _over_char_budget(
        [ROOT / rel for rel in ut.module_files(ROOT, CFG)], CFG["claude_md_max_chars"]
    )
    errors += _over_char_budget(sorted(ROOT.glob(CFG["rules_glob"])), CFG["rules_max_chars"])
    return errors


def _over_char_budget(paths: list[Path], max_chars: int) -> list[str]:
    errors: list[str] = []
    for path in sorted(set(paths)):
        text = loaded_text(path)
        rel = path.relative_to(ROOT).as_posix()
        if len(text) > max_chars:
            errors.append(f"  {rel}: {len(text)} chars > {max_chars}")
        longest = max((len(line) for line in text.splitlines()), default=0)
        if longest > CFG["rules_max_line"]:
            errors.append(f"  {rel}: a line of {longest} chars > {CFG['rules_max_line']}")
    return errors


# --------------------------------------------------------------------------- routing

def _split_paths(cell: str) -> list[str]:
    """Pull file paths out of a routing-table cell: `a` + [b](b) + a/glob/*.md."""
    paths: list[str] = []
    for chunk in re.split(r"\s+\+\s+", cell):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("/"):  # empty, or a skill invocation
            continue
        link = re.match(r"^\[.*?\]\((.+?)\)$", chunk)
        if link:
            chunk = link.group(1)
        chunk = chunk.strip("`").strip()
        if chunk:
            paths.append(chunk)
    return paths


def _routes(text: str) -> list[tuple[str, str]]:
    """[(topic, path)] from the first markdown table whose header says 'Read first'."""
    rows: list[tuple[str, str]] = []
    in_table = False
    for line in text.splitlines():
        s = line.strip()
        if not in_table:
            if s.startswith("|") and "Read first" in s:
                in_table = True
            continue
        if not s.startswith("|"):
            break
        if re.match(r"^\|[\s\-:|]+\|$", s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) >= 2:
            rows += [(cells[0], p) for p in _split_paths(cells[1])]
    return rows


def _resolves(path: str) -> bool:
    if "*" in path:
        p = Path(path)
        parent = ROOT / p.parent
        return parent.is_dir() and any(parent.glob(p.name))
    return (ROOT / path).exists()


def check_routing() -> tuple[list[str], list[str]]:
    """(errors, notes). A dead route blocks; a context file nothing routes to informs."""
    root_md = ROOT / CFG["root_claude_md"]
    if not root_md.exists():
        return [f"  {CFG['root_claude_md']} not found"], []
    text = root_md.read_text(encoding="utf-8")
    routes = _routes(text)
    if not routes:
        return [f"  {CFG['root_claude_md']}: no routing table (no 'Read first' header column)"], []

    errors = [f"  {topic!r}: route points at missing '{p}'" for topic, p in routes if not _resolves(p)]

    # A surface is routed when the table names it outright or a glob route covers it.
    globs = [r for _, r in routes if "*" in r]
    surfaces = [*(ROOT / rel for rel in ut.module_files(ROOT, CFG)), *ROOT.glob(CFG["rules_glob"])]
    notes = []
    for path in sorted(set(surfaces)):
        rel = path.relative_to(ROOT).as_posix()
        if rel not in text and not any(fnmatch(rel, g) for g in globs):
            notes.append(f"  {rel}: no routing row in {CFG['root_claude_md']} — invisible to an agent")
    return errors, notes


# ------------------------------------------------------------- imports and the launch budget

# `@path` outside a code span. Claude Code's own parser skips code spans and fenced blocks, so
# `` `@README` `` is literal text and `@README` on its own is an import — this matches that.
_IMPORT_RE = re.compile(r"(?<![\w`])@([A-Za-z0-9_~][A-Za-z0-9_./\\-]*)")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_SPAN_RE = re.compile(r"`[^`\n]*`")
_MAX_HOPS = 4  # Claude Code's documented import depth


def _imports_of(path: Path) -> list[tuple[str, Path | None]]:
    """[(raw, resolved-or-None)] for one file. Relative paths resolve against the FILE, not cwd."""
    text = _SPAN_RE.sub(" ", _FENCE_RE.sub(" ", path.read_text(encoding="utf-8")))
    out: list[tuple[str, Path | None]] = []
    for raw in _IMPORT_RE.findall(text):
        target = Path(raw).expanduser() if raw.startswith("~") else (path.parent / raw)
        out.append((raw, target if target.exists() else None))
    return out


def check_imports_and_budget() -> tuple[list[str], list[str]]:
    """(errors, notes). A broken @import loads NOTHING and says nothing — it has to block.

    Then measure what every session pays before the first prompt: the root CLAUDE.md, everything
    it imports (imports do not save context — they move it), and every `.claude/rules/*.md`
    with no `paths:` frontmatter, which loads unconditionally. That total is the number the
    context-engineering literature is actually about; per-file caps alone let it grow by adding
    files."""
    root_md = ROOT / CFG["root_claude_md"]
    if not root_md.exists():
        return [], []
    errors: list[str] = []
    loaded: dict[Path, int] = {root_md: len(loaded_text(root_md))}

    frontier, hops = [root_md], 0
    while frontier and hops < _MAX_HOPS:
        nxt: list[Path] = []
        for path in frontier:
            for raw, resolved in _imports_of(path):
                if resolved is None:
                    errors.append(f"  {path.relative_to(ROOT).as_posix()}: @{raw} resolves to "
                                  "no file — it imports NOTHING, silently")
                elif resolved not in loaded and resolved.is_file():
                    loaded[resolved] = len(loaded_text(resolved))
                    nxt.append(resolved)
        frontier, hops = nxt, hops + 1
    if frontier:
        errors.append(f"  @import chain is deeper than {_MAX_HOPS} hops — Claude Code stops "
                      "there, so the deepest files never load")

    unscoped: list[Path] = []
    for path in sorted(ROOT.glob(CFG["rules_glob"])):
        head = path.read_text(encoding="utf-8")[:400]
        if not (head.startswith("---") and "paths:" in head.split("---")[1]):
            unscoped.append(path)
            loaded[path] = len(path.read_text(encoding="utf-8"))

    total = sum(loaded.values())
    budget = CFG["always_loaded_budget_chars"]
    summary = (f"  {len(loaded)} files, {total} chars (~{total // 4} tokens) load before every "
               f"prompt; budget {budget}")
    if total > budget:
        errors.append(summary)
        errors += [f"    {p.relative_to(ROOT).as_posix()}: {n}"
                   for p, n in sorted(loaded.items(), key=lambda kv: -kv[1])]
        errors.append("    Trim, or give a rules file `paths:` frontmatter so it loads only "
                      "when a matching file is touched.")
        return errors, []
    notes = [summary]
    if unscoped:
        notes.append("  unconditional (no `paths:` frontmatter): "
                     + ", ".join(p.name for p in unscoped))
    return errors, notes


# --------------------------------------------------------------------------- text hygiene

_SUFFIXES = {".py", ".md", ".ps1", ".ts", ".tsx", ".js", ".jsx", ".svelte", ".yaml", ".yml",
             ".sql", ".toml", ".html", ".css", ".json"}


def _context_files() -> list[Path]:
    """Every context surface: the root CLAUDE.md and what it imports, module CLAUDE.md files,
    rules files, and the overflow and archive tiers."""
    root_md = ROOT / CFG["root_claude_md"]
    files = {root_md} if root_md.is_file() else set()
    if files:
        files |= {r for _, r in _imports_of(root_md) if r and r.is_file()}
    files |= {ROOT / rel for rel in ut.module_files(ROOT, CFG)}
    files |= set(ROOT.glob(CFG["rules_glob"]))
    files |= {p for p, _ in ut.tier_files(ROOT)}
    return sorted(p for p in files if p.is_file())


def check_text_hygiene() -> list[str]:
    """A BOM, a mojibake marker or a literal control byte in a context file. A BOM before
    `---` hides a rules file's `paths:` frontmatter; mojibake is read as text."""
    if not CFG["text_hygiene"]:
        return []
    errors: list[str] = []
    for path in _context_files():
        rel = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            errors.append(f"  {rel}: UTF-8 BOM")
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            errors.append(f"  {rel}: UTF-16 BOM")
        for marker in MOJIBAKE:
            if marker in data:
                errors.append(f"  {rel}: mojibake marker {marker!r} (a cp1252 round-trip)")
        for byte, name in ((8, "backspace"), (12, "form feed"), (27, "escape")):
            if bytes([byte]) in data:
                errors.append(f"  {rel}: literal 0x{byte:02x} ({name}) byte — a backslash-"
                              f"{name[0]} that lost its backslash in a heredoc. Inside a regex, "
                              "that check is INERT.")
                break
    return errors


# --------------------------------------------------------------------------- pruning

def check_root_chars() -> list[str]:
    """`claude_md_max_chars` on the root CLAUDE.md (the module globs never match it)."""
    path = ROOT / CFG["root_claude_md"]
    if not path.is_file():
        return []
    n = len(loaded_text(path))
    if n <= CFG["claude_md_max_chars"]:
        return []
    return [f"  {CFG['root_claude_md']}: {n} chars > {CFG['claude_md_max_chars']}"]


_ID = re.compile(r"[a-z0-9-]{1,20}\Z")


def _evidence_error(reason: str, ev: str) -> str:
    """Why a move row's evidence is not well formed for its reason, or "". Shape only: that a
    cited path or partner exists is enforced when the line moves, and a cite that dangles later
    is an audit input, never a block on unrelated work."""
    if reason in ("derivable", "enforced"):
        if not (ev.startswith("cite ") and ev[5:].partition("::")[0].strip()):
            return "needs 'cite <path[::symbol]>'"
    elif reason in ("duplicate", "conflict") and not _ID.match(ev):
        return "needs the other line's ID"
    elif reason in ("stale", "old-model") and not ev:
        return "needs evidence (a path, tombstone or phrase)"
    return ""


def check_pruning_hygiene() -> list[str]:
    """The tier files and tagged live lines are well formed. Untagged live lines
    are normal (IDs exist only once a line has a history) and are never looked at."""
    live = [line for rel in ut.live_files(ROOT) for line in ut.parse_live(ROOT, rel)]
    entries = ut.load_entries(ROOT)
    errors: list[str] = []
    for ids in ([line.id for line in live if line.id], [e.id for e in entries]):
        errors += [f"  duplicate ID s:{i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for e in entries:
        where = f"  {e.file} s:{e.id}:"
        if e.tier != "gap":
            errors += [f"{where} missing `{f}:`" for f, v in (("line", e.line), ("from", e.source))
                       if not v.strip()]
        for row in ut.rows(e, "moved"):
            parts = row.split(" \u00b7 ")
            reason, ev = (parts + ["", ""])[1].strip(), (parts + ["", ""])[2].strip()
            if reason not in ut.REASONS:
                errors.append(f"{where} move row has no closed reason ({reason!r})")
            elif bad := _evidence_error(reason, ev):
                errors.append(f"{where} {reason} {bad}")

    sources = set(ut.live_files(ROOT))
    for old, new in ut.tombstones(ROOT).items():
        sources |= {old, new or old}
    for path, tier in ut.tier_files(ROOT):
        rel = path.relative_to(ROOT).as_posix()
        text = ut.read(path)
        own = {ut.tier_file(tier, s) for s in sources | {e.source.split("#")[0] for e in entries
                                                          if e.file == rel}}
        exempt = rel == ut.GAPS or ut.is_legacy(text) or re.search(r"(?m)^tombstone:", text)
        if not exempt and rel not in own:
            errors.append(f"  {rel}: not named `kit-<slug>.md` for a source, and no `tombstone:` "
                          "or `legacy:` line")

    root_md = ROOT / CFG["root_claude_md"]
    if root_md.is_file():
        routed = [p for _, p in _routes(root_md.read_text(encoding="utf-8"))]
        routed += [raw for raw, _ in _imports_of(root_md)]
        errors += [f"  {CFG['root_claude_md']}: routed archive {p!r} (it is never loaded)"
                   for p in routed if p.replace("\\", "/").lstrip("./").startswith(
                       ut.ARCHIVE_DIR) or ut.glob_re(p).match(ut.ARCHIVE_DIR + "/x.md")]

    for rel in ut.live_files(ROOT):
        budget = CFG["claude_md_max_chars" if rel.endswith("CLAUDE.md") else "rules_max_chars"]
        lines = ut.read(ROOT / rel).split("\n")
        pinned = sum(len(lines[x.index]) + sum(len(c) for c in ut.children(lines, x.index))
                     for x in ut.parse_live(ROOT, rel) if x.pinned)
        if pinned > ut.PIN_CAP * budget:
            errors.append(f"  {rel}: pinned lines are {pinned} chars > {ut.PIN_CAP:.0%} of {budget}")
    return errors


def main() -> int:
    blocking: list[tuple[str, list[str]]] = []
    notes: list[tuple[str, list[str]]] = []

    blocking.append(("Size budgets", check_size_limits()))
    route_errors, route_notes = check_routing()
    blocking.append(("Routing manifest", route_errors))
    notes.append(("Unrouted context files", route_notes))
    import_errors, budget_notes = check_imports_and_budget()
    blocking.append(("Imports and launch budget", import_errors))
    notes.append(("Launch context budget", budget_notes))
    blocking.append(("Text hygiene", check_text_hygiene()))
    try:
        prune_errors = check_pruning_hygiene()
    except Exception as e:  # never takes the other checks down; the headless audit's validate
        # relies on it as the last gate
        prune_errors = [f"  could not run: {type(e).__name__}: {e}"]
    blocking.append(("Pruning hygiene", prune_errors))
    blocking.append(("Root CLAUDE.md size", check_root_chars()))

    for title, lines in notes:
        if lines:
            print(f"NOTE — {title}:")
            print("\n".join(lines))
    failed = False
    for title, lines in blocking:
        if lines:
            failed = True
            print(f"FAIL — {title}:")
            print("\n".join(lines))
    if not failed:
        print("context_check: OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
