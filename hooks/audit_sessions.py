"""Condense Claude Code session transcripts into one text file per session, so the
`auditing-sessions` skill can hand each one to an auditor.
When the kit's capture store (context_upkeep.py) holds records, those are read instead of the
transcripts, which Claude Code deletes after 30 days.

The transcript is the only record of what the owner asked and what the agent did in reply;
PR bodies are the agent's own account.

usage: <python_cmd> .claude/hooks/audit_sessions.py --since YYYY-MM-DD --out <dir> [--projects <dir>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from _kit import repo_root

REPO_ROOT = repo_root()
_MAX_OWNER_CHARS = 6000
_MAX_AGENT_CHARS = 5000


def default_projects_dir() -> Path:
    # Claude Code keys a project by its absolute path with every non-alphanumeric run as '-'.
    slug = "".join(c if c.isalnum() else "-" for c in str(REPO_ROOT))
    return Path.home() / ".claude" / "projects" / slug


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"
    )


def _tools(content) -> list[str]:
    if isinstance(content, str):
        return []
    return [
        b.get("name", "") for b in content or []
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


_FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
               "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def files_of(content) -> list[str]:
    """Paths the agent read or edited in one message, in order, once each."""
    seen: dict[str, None] = {}
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _FILE_TOOLS:
            path = (b.get("input") or {}).get(_FILE_TOOLS[b["name"]])
            if isinstance(path, str) and path:
                seen[path] = None
    return list(seen)


def records_of(transcript: Path):
    """Parsed transcript records, streamed one line at a time (a transcript can be tens of MB).
    Lines that are not a JSON object — a half-written last line — are skipped."""
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


# Text that opens a `user` record the owner did not write: hand-backs, skill bodies, hook
# feedback, usage-limit notes, compaction, interrupt markers. `<...>` records (task notifications,
# system reminders, command echoes, bash output) are dropped separately.
_NOT_OWNER = ("Another Claude session sent a message", "[Subagent hand-back]",
              "Base directory for this skill", "Stop hook feedback", "PreToolUse",
              "[Usage limit reached", "[Earlier usage-limit", "[Request interrupted",
              "This session is being continued", "# Update Config Skill",
              "# Claude in Chrome", "## Page contract")
_BASH_INPUT = re.compile(r"\s*<bash-input>(.*?)</bash-input>\s*$", re.S)


def owner_text(rec: dict, text: str) -> str:
    """What the owner typed in this `user` record, or "" when it is not theirs. Claude Code marks
    most injected records (isMeta, a non-human origin, a tool result); the text prefixes catch the
    rest and older transcripts (a record with a human origin skips them). A `!` command the owner ran is a turn: `! <command>`."""
    if (rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("toolUseResult")
            or rec.get("sourceToolUseID") or rec.get("usageLimitNote")
            or (isinstance(rec.get("origin"), dict) and rec["origin"].get("kind") != "human")):
        return ""
    text = text.strip()
    bash = _BASH_INPUT.match(text)
    if bash:
        return "! " + bash.group(1).strip()
    if isinstance(rec.get("origin"), dict):  # kind == "human" here: the harness vouches, and an
        return text                          # owner may paste a hand-back or start with "<"
    if not text or text.startswith("<") or text.startswith(_NOT_OWNER):
        return ""
    return text


def condense(transcript: Path) -> tuple[str, int, list[dict]]:
    return condense_records(records_of(transcript))


def condense_records(records) -> tuple[str, int, list[dict]]:
    """One block per owner turn: the owner's words, then the agent's tool counts, first text and
    turn-ending text. Returns (text, owner_turn_count, [{"ts", "owner"} per turn]) — the capture
    store keeps the per-turn timestamps so a restore can prove a turn came after a move.
    Takes any iterable of records, so a caller that is already reading the file feeds it."""
    blocks: list[dict] = []
    for rec in records:
        if rec.get("type") not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text, ts = _text(content), rec.get("timestamp", "")
        if rec["type"] == "user":
            owner = owner_text(rec, text)
            if not owner:
                continue
            blocks.append({"ts": ts, "owner": owner[:_MAX_OWNER_CHARS], "n": 0,
                           "first": "", "last": (ts, ""), "tools": Counter()})
        elif blocks:
            blocks[-1]["tools"].update(_tools(content))
            if text.strip():  # keep the ends only: a long turn has hundreds of messages
                b = blocks[-1]
                b["n"] += 1
                b["first"] = b["first"] or text
                b["last"] = (ts, text)
    out: list[str] = []
    for n, b in enumerate(blocks, 1):
        owner = b["owner"]
        out.append(f"\n{'=' * 100}\n### TURN {n}  [{b['ts']}]  OWNER\n{owner}\n")
        if b["n"]:
            first, (last_ts, last) = b["first"], b["last"]
            out.append(f"\n--- agent: {b['n']} messages; tools: {dict(b['tools'])}\n")
            out.append(f"--- agent FIRST text:\n{first[:1500].strip()}\n")
            out.append(f"--- agent LAST text [{last_ts}]:\n{last[-_MAX_AGENT_CHARS:].strip()}\n")
        else:
            out.append(f"\n--- agent: {sum(b['tools'].values())} tool calls, no text\n")
    turns = [{"ts": b["ts"], "owner": b["owner"]} for b in blocks]
    return "".join(out), len(blocks), turns


def _stored(records):
    """(session id, last activity, condensed text, owner turns) per captured record."""
    for r in records:
        yield r["id"], r["ts_end"][:16].replace("T", " "), r.get("text", ""), r.get("owner_turns", 0)


def _transcripts(args):
    """The same, condensed from the transcripts modified since `--since`."""
    for path in sorted(args.projects.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if modified.date() < args.since:
            continue
        text, turns, _ = condense(path)
        if turns:
            yield path.stem, modified.strftime("%Y-%m-%d %H:%M"), text, turns


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", type=date.fromisoformat, required=True, help="first day, YYYY-MM-DD")
    ap.add_argument("--out", type=Path, required=True, help="directory for the condensed files")
    ap.add_argument("--projects", type=Path, default=default_projects_dir())
    args = ap.parse_args(argv)
    from context_upkeep import records_after  # lazy: context_upkeep imports this module lazily too

    # The kit's own store outlives Claude Code's 30-day transcripts: read it when it has records.
    stored = [r for r in records_after(REPO_ROOT, "") if r["ts_end"][:10] >= args.since.isoformat()]
    if not stored and not args.projects.is_dir():
        print(f"no transcripts dir: {args.projects}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for sid, stamp, text, turns in (_stored(stored) if stored else _transcripts(args)):
        target = args.out / f"turns-{sid[:8]}.txt"
        target.write_text(text, encoding="utf-8")
        rows.append((sid[:8], stamp, turns, target))
    print(f"{'session':<10}{'last modified':<18}{'owner turns':>12}  file")
    for stem, mod, turns, target in rows:
        print(f"{stem:<10}{mod:<18}{turns:>12}  {target}")
    print(f"total: {len(rows)} sessions, {sum(r[2] for r in rows)} owner turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
