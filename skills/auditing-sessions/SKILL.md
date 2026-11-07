---
name: auditing-sessions
description: Use when the start-of-session line says an audit is due, when the owner asks how the sessions went or whether the context is holding up, when an agent keeps making a mistake the context should prevent, and after a model change.
---
# Auditing Sessions

The sessions are the only record of what the owner asked and what the agent did. PR bodies and
progress rows are the agent's own account of itself, and they flatter it. The kit keeps its own
condensed copy of every owner session; this skill audits them for context failures (a line that
was missing, wrong, stale or ignored), moves context lines on that evidence, asks the owner
where judgment is needed, and writes one ledger that the next audit is compared against.

`U` below means `<python_cmd> .claude/hooks/context_upkeep.py`. `<python_cmd>` is `python_cmd`
in `.claude/context-kit.json`; `U batch` prints `u`, the whole prefix, spelled out. Headless,
that interpreter is the only one allowed: copy `u` exactly, since any other spelling of the
command is refused. **Every change to a line goes through `U`; never edit a line of a tier file
by hand** (rules files, module `CLAUDE.md` files, overflow, archive, `_gaps.md`). Each command
prints JSON, or `refused: ...` with exit 3. A refusal is the guard working: drop the move, or
put it on the owner's list (step 9). Quotes are the owner's words, at most 200 characters.

## 1. Read the batch

`U batch` writes `.context-kit-batch/<session>.txt` for at most 40 sessions past the last
ledger, and prints `files`, `covered_until` (the ledger carries it), `inputs` (step 5),
`headless` and `u`. **`headless: true`** means the kit started this run itself
(`CONTEXT_KIT_HEADLESS` is set): no owner is there, so nothing is asked and nothing waits. It
runs in its own worktree on the `context/audit` branch, and every step below that says
"headless" applies.

`U batch` refuses in an owner's session while a headless audit of the same sessions is running
or waits for review. Tell the owner what it printed (wait, or review and `accept`/`dismiss`)
and stop: two audits of one batch pay twice and can disagree.

No files: there are no sessions to audit, so skip steps 3, 4, 6 and 7 and work the inputs. If
they need nothing either, say so and stop. A quiet run is a healthy one.

## 2. Re-key first

For each `inputs.orphans` item, `U tombstone <path> <renamed_to>`, or `U tombstone <path>
deleted` when `renamed_to` is null (a deleted module's lines are archived as `stale`). Do this
before anything else, so `lookup` and `restore` resolve through the new paths.

## 3. Audit every owner turn, read-only auditors of 3-5 sessions each

Dispatch one auditor per 3-5 batch files, in parallel, each told to read the root `CLAUDE.md`
and `.claude/skills/context-sync/context-guide.md` first and to write
`<scratchpad>/audit-<session>.md` for each of its sessions. Headless, there are no auditors (the
Agent tool is not allowed): read the batch files yourself, one at a time, and keep the same
per-session findings. The rubric is fixed so audits compare:

| field | values |
|---|---|
| ask_type | task · question · ruling · continue/go · correction-or-re-ask · chatter |
| outcome | done-in-turn · partial · asked-instead · nothing · n/a |
| flags | context-missing · context-wrong · context-ignored · stale-reference · rediscovery · conflicting-instructions · owner-frustration |

- `context-missing`: the agent lacked a fact about this repo that no line states, and it cost
  a turn or a wrong result.
- `context-wrong`: a loaded line misled as worded.
- `context-ignored`: a live line covered it and was not followed.
- `stale-reference`: a line pointed at a file, function or command that is gone or changed.
- `rediscovery`: the agent worked out, or the owner re-told it, something a context line or an
  archived lesson already said.
- `conflicting-instructions`: two loaded lines, or a line and the ask, pulled opposite ways;
  name both.

Every failure carries the session, the turn's timestamp from its `### TURN n [ts]` header, the
owner's words verbatim, and the context line involved, quoted (or "none"). An auditor that
cannot tell from the extract says "insufficient in extract", never guesses. Its summary names
the single worst turn. Read each auditor's file; never relay a total from its chat message.

## 4. Map each failure

Find the line with `U lookup <words> [--files <path>]` (every tier, legacy archives and
tombstones), then exactly one of:

| finding | command |
|---|---|
| a moved line would have prevented it | `U restore <id> <session> <turn-ts> "<quote>"`, with that turn's timestamp |
| a live line misled as worded | a rewording, for the owner's list |
| a live line was there and not followed | `U ignored <id-or-text> <session> <turn-ts> "<quote>"` |
| a live line caused it | `U move <id-or-text> archive <reason> --evidence <evidence>` |
| nothing covers it | `U seen "<title>" --session <session> --quote "<quote>"` |

A result with `needs_owner` (`second-restore`, `second-ignored`) goes on the owner's list.

## 5. Inputs

From `inputs` in the batch output:
- **`dangling`**: a backticked path or a `paths:` glob that resolves nowhere. Judge each row
  first and drop the false positives (`and/or`, a placeholder, a name that is not a path). A
  path that moved is a rewording to its new path; a path that is gone is `U move <id-or-text>
  archive stale --evidence <the path>`; a dead `paths:` glob is front matter, so it goes on the
  owner's list.
- **`drift`**: modules whose code changed since their `CLAUDE.md` did, oldest drift first. Run
  the `/context-sync` procedure on the first 3 of them in every run, headless too: compare the
  module's code with its `CLAUDE.md`, with no scanner subagents. An addition is `U seen
  "<title>" --source sync`; a rewording goes on the owner's list. The rest carry over: name
  them in the ledger. When the owner runs `/context-sync` on every drifted module in the same
  session, nothing carries over.
- **`restated`**, the restated-figures sweep: lines added since the last ledger that state a
  figure or a status, or that `lookup` finds elsewhere (`hits`, each `{tier, id, text}`). A
  figure or status stays only as dated history or as a pointer at what computes it; otherwise
  `U move <id-or-text> archive derivable --evidence "cite <the producer's path>"`, or propose
  rewording it to that pointer. A `live` hit is a duplicate: `U move <id-or-text> archive
  duplicate --evidence <the kept ID or its exact text>`. An `archive` or `overflow` hit means the
  lesson came back by hand: owner's list.

## 6. Model change

`U ledger` prints `models` (the batch's model families, most used first), `figures`, and
`last`, the previous ledger. When `models[0]` is not first in that ledger's `models:` (no
ledger yet: skip), run the compensation pass: every live line that compensates for an older
model (shouting, "always" or "never forget" reminders, step-by-step hand-holding for what
models now do unprompted) moves with `U move <id-or-text> archive old-model --evidence "<the
phrase>"`.

## 7. Promote

A gap whose `seen` result says `second_sighting: true` and that has a mechanism (an existing
test, check or hook that enforces it) becomes live: `U promote <gap-id> <path> <section>
"<text>" --mechanism <path>`. A second sighting with no mechanism goes on the owner's list:
name the test or check that would carry it, or leave it a gap.

## 8. Demote until `context_check` passes

Run `<python_cmd> .claude/hooks/context_check.py`. While a budget fails, first move what has
evidence against it, then `U move <id-or-text> overflow evicted` for the line the guard
allows: the bottom-most unpinned line with no restore in 90 days, else the fewest (a refusal
names the rule). An overflow file over budget sends its entries to `archive` the same way.
Net removals (removals minus restores) stop at 10% of `inputs.live_lines`; the rest goes on
the owner's list, as does a budget no move can reach (root `CLAUDE.md` and what it imports).

## 9. Ask the owner, live, in one list

One numbered message, then wait for the answers:
- **every eviction and `old-model` move**, the line quoted: keep the move? (default: yes)
- **every conflict**, both lines quoted: which one stays? A conflict never moves without the
  owner.
- a second `restored` or `ignored` of the same ID: pin it, reword it, or give it a mechanism?
- every rewording (the line and the proposed text), pin, new line without a mechanism, dead
  `paths:` glob, and budget overrun in root `CLAUDE.md`

Apply every answer with `--owner`:
- an undone move: `U restore <id> --owner` (it records `restored · owner`)
- a conflict: `U move <loser> archive conflict --evidence <winner-id> --owner`
- a rewording: `U revise <id-or-text> "<new text>" --owner`
- a pin: `U pin <id-or-text> --owner` (`unpin` reverses it; it refuses past the pin cap)

A `paths:` glob and root `CLAUDE.md` have no command: edit them directly, and only after the
owner says yes.

**Headless,** there is no message and no wait, and `--owner` is refused. The list goes into the
ledger (step 10) under `## Owner decisions`, as numbered `- [ ]` items. Each item ends with the
one command its answer runs, spelled in full (`u` from step 1, then the command, not `U`),
after `undo:` or `apply:`. `accept` runs only `restore` for an undo and only `move`, `revise`
or `pin` for an apply, and refuses an `n` on an item with neither:
- **every eviction and `old-model` move is already applied** (default yes); its item carries
  the undo, `restore <id> --owner`
- **every conflict is never moved**; its item quotes both lines and carries the apply that
  archives the line that would go, `move <loser> archive conflict --evidence <winner-id>
  --owner`
- every other item is not applied, and carries its apply (`revise ... --owner`, `pin ...
  --owner`); one with no command says `apply: by hand` and what to change

The owner answers with `accept 1y 2n ...`: `y` keeps an applied item or runs an apply, and `n`
runs an undo or skips an apply.

```
## Owner decisions
- [ ] 1. evicted "<the line>" from `src/api/CLAUDE.md` (applied, default yes) · undo: `python .claude/hooks/context_upkeep.py restore s:api-retry --owner`
- [ ] 2. conflict: "<line A>" vs "<line B>" · apply: `python .claude/hooks/context_upkeep.py move s:b archive conflict --evidence s:a --owner`
```

## 10. Write the ledger

`docs/references/session-audits/<YYYY-MM-DD-HHMM>.md`, named for now in UTC:

```
---
covered_until: <covered_until from step 1>
models: <models from step 6, comma-separated>
---
```

Then:
- the session range (first and last session, and how many)
- **the context failures**: the flag totals, and each failure with its line and what step 4
  did about it
- **the rot signal**: the rubric's correction-or-re-ask count over its owner asks, from the
  auditors' files, beside the last ledger's, with the `figures` from `U ledger` (`asks`,
  `corrections`, `rate`) as a mechanical cross-check. Read it as a trend over several ledgers,
  never as a verdict on one: two audits grading the same sessions can differ by a few asks.
- the worst turn, quoted
- every move and every answer, each with the command that applied it

## 11. Commit

On the owner's current branch:

```bash
U commit -m "context: audit <YYYY-MM-DD-HHMM> [context-kit]" <the ledger, every file the commands changed, and every approved direct edit, by name>   # stage by name, never the whole tree
U _helper    # refreshes the due line, so the next session start stops saying an audit is due
```

Headless, the same commit command lands on `context/audit` (the worktree's branch), as the run's
one commit, and `U _helper` is skipped. Never push or merge: the owner's `accept` does that.

An audit that ends in chat has produced nothing durable.

## Red flags

- Reading PR bodies or progress rows as the record of what the owner asked.
- A total relayed from an auditor's message instead of its file.
- A tier line edited by hand, or a refusal worked around instead of asked.
- An eviction, `old-model` move or conflict the owner was not shown.
- A lesson written into a live file by hand instead of filed with `seen` and promoted.
