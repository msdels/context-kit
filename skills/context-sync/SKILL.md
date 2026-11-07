---
name: context-sync
description: Scans each module's code against its CLAUDE.md, reports stale, oversized, unrouted or contradicted context, asks the human to fill the gaps it found, and drafts the updates. Use when context files feel out of date, after a refactor or a new module, when an agent makes a mistake the context should have prevented, before a big onboarding, or when the context check fails. Also on request as "context sync", "refresh the context files", or "/context-sync".
argument-hint: "[module ...] [--check]"
---
# Context Sync

Scan each module's code, compare it against its context file, find the gaps, ask the human to
fill them, and apply the updates they approve.

`U` below means `<python_cmd> .claude/hooks/context_upkeep.py`.

**Arguments:**
- No args: sync all modules
- module names: sync only those (the audit passes its `drift` list)
- `--check`: report staleness only, no proposals

## Step 1: Load the guide

Read `.claude/skills/context-sync/context-guide.md`. It holds the grep test, what each surface
costs, and the pruning rules. Every proposal below is judged against it.

## Step 2: Validate the routing table

Check that every row of the root CLAUDE.md routing table points at a file that exists, and that
every module `CLAUDE.md` (and any `.claude/rules/*.md`) has a row. Report broken and unrouted
files.

## Step 2b: Check size limits

Run `<python_cmd> .claude/hooks/context_check.py` and report any file over its limit.
Over-limit files are the leading indicator of context going stale: they stop being read.

Read the launch-budget line it prints: files, characters and estimated tokens paid before every
prompt. When that is what is over, the first remedy is not trimming prose. It is moving a
module-specific line from the root CLAUDE.md (or a file it imports) into that module's
CLAUDE.md, which loads only when Claude works there, or demoting a multi-step procedure to a
skill. Report the number even when it passes: a budget nobody watches is the one that doubles.
Trim a live file by whole-line moves (Step 7). A demotion to a skill and archiving older
`design-decisions.md` entries to `docs/context/design-decisions-archive.md` are proposals that
wait for approval (Step 7).

## Step 2c: Restated-figures sweep

A count, threshold or status restated in prose goes stale without anyone noticing, because a
wrong adjective reads as prose while a wrong number reads as an error. For every context file in
scope, grep for numerals and status adjectives (`green`, `red`, `pending`, `N%`, `N of M`,
`~N`). Each hit must be one of:
- **dated history** ("2026-03-10: took X from 11.6% to 19.3%"), a record, allowed;
- **a pointer at what computes it** ("`scripts/coverage.py` is the authority"), allowed;
- **a live claim with no producer**, a violation: propose rewording it to a pointer at the
  symbol or script that computes it, or archive it (`move <line> archive derivable --evidence
  "cite <the producer>"`, Step 7). When the number must stay, pair it with its denominator and
  date so it reads as history, not as now.

## Step 3: Scan modules

Enumerate modules with `<python_cmd> -c "import sys; sys.path.insert(0, '.claude/hooks'); import upkeep_tiers as ut; print(*ut.module_files('.'), sep=chr(10))"` (the `claude_md_globs` matches, minus the root `CLAUDE.md` and anything under `.claude/`). If modules are named, scan only those.

For each module, use the Agent tool to dispatch a read-only subagent. Pass it:
- the content of `.claude/skills/context-sync/module-context-updater.md` as instructions
- the current content of `{module}/CLAUDE.md`, and of any `.claude/rules/*.md` whose `paths:`
  globs cover the module
- the module directory to scan

Collect from each subagent its proposed changes (additions, rewordings, whole-line moves), its
possible bugs, its summary, and its questions.

If `--check` was passed, report the summaries and stop here.

## Step 4: Cross-reference the work ledger (skip if the repo has none)

If the repo tracks work in files (a `progress/` dir, a backlog, issue exports), compare them with
git:
- **Stale active items:** marked active, with every branch merged: possibly complete.
- **Stale complete items:** marked complete, with open branches and recent commits.
- **Orphaned in-progress tasks:** no commits in the last 7 days.

Report findings. Do NOT modify progress files: present recommendations for human review.

## Step 5: Check design decisions

Read `docs/context/design-decisions.md`. Look at recent conversation history for design
decisions or domain knowledge the human shared that are not captured yet.

## Step 6: Research and suggest, then ask

### 6a. Research first
For each gap the subagents found, decide whether research could help:
- **Technical patterns** (retries, rate limiting, auth, error handling, caching): use WebSearch
  for current common practice.
- **Architecture decisions** (module boundaries, composition vs inheritance): search for
  trade-off analyses.
- **Domain-specific questions** (business logic, entity relationships, org conventions): these
  usually cannot be researched; ask.

### 6b. Present one list
Group the findings into three kinds, numbered, so the human can answer `1y 2n ...`:
1. **A line that is false**: the line, what the code does now, and the proposed rewording.
2. **A fact the context lacks**: the proposed line, where it goes, and the evidence (below).
3. **A possible bug**: code that contradicts a line that is still right. This goes to the
   backlog, not into a context file.

Each fact the context lacks, as:

```
**[Module] [Topic]**
I noticed: [what you observed in the code]
Research says: [what common practice suggests, with sources]
Suggested line: [the proposed text]
Confidence: [high/medium/low]
```

Then the direct questions research could not answer.

### 6c. Collect answers
- Confirmed: apply it (Step 7).
- Corrected: the human's correction is the line instead.
- "I don't know": file it as a gap, `U seen "OPEN: why a 60s retry ceiling?" --source sync`.

Wait for the answers. Do NOT apply anything that needs approval before the human responds.

Where answers go:
- Domain knowledge → `docs/context/domain-model.md`
- Design choices → `docs/context/design-decisions.md`
- Module facts, rules and gotchas → the module's `CLAUDE.md` (Step 7)

## Step 7: Apply

- **Whole-line moves need no approval:** a line that names what no longer exists, restates the
  code, or repeats another line moves with `U move <line> archive <stale|derivable|duplicate>
  --evidence <path, cite or the other line>`. `U restore <id> --owner` undoes it.
- **Rewordings** wait for approval, then apply with `U revise <line> "<new text>" --owner`, which
  keeps the old text in the archive.
- **Additions** wait for approval, then are written into the module's `CLAUDE.md` directly, one
  line in the section it belongs to. A new line has no ID; it gets one the first time it moves.
- **Never delete a line by hand.** A removal is always a move, so it can be found and
  restored.
- **Root `CLAUDE.md` and `docs/context/` edits** wait for approval, then are edited directly.

Then run `<python_cmd> .claude/hooks/context_check.py` again. If an addition pushed a file over
its budget, propose the moves that bring it back (the audit's eviction rule, Step 8 of
`/auditing-sessions`, says which line goes first).

Show the moves you made beside the approved changes.

## Step 8: Update routing and commit

If new modules were found, add their routing rows to the root CLAUDE.md. Commit every file you
changed, by name:

```bash
<python_cmd> .claude/hooks/context_upkeep.py commit -m "docs: sync context files via /context-sync [context-kit]" <each file you changed>
```
