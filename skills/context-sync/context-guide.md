# Context Guide

Loaded by `/context-sync` and `/auditing-sessions`, and read once at setup. It holds what goes in
a context file, what each surface costs, and how lines leave.

## Core principle: the grep test

Every line in a context file must fail this test: "Could an agent discover this by reading the
code or running grep?" If yes, it does not belong.

**Include:** domain knowledge, design rationale, gotchas, non-obvious conventions, routing.
**Exclude:** file listings, function signatures, import structures, style a linter enforces,
and the story of the incident behind a rule.

### Good vs bad context

BAD (greppable): "Client uses mixin composition"
GOOD (not greppable): "We chose mixin composition because each entity's endpoints change
independently and the client must stay extractable as a standalone package"

BAD (greppable): "All IDs are strings"
GOOD (not greppable): "All IDs are strings because some upstream systems use alphanumeric
IDs (e.g., `6r3lx6l`), and mixing int/str in pandas causes silent join failures"

BAD (vague): "Rate limiting is important"
GOOD (specific): "Server returns 429 after ~100 req/s, retry with exponential backoff, 60s ceiling"

## One file per module

Each module has one context file, `{module}/CLAUDE.md`, with three kinds of line:
- **Why it is built this way:** the decision behind the shape, and the alternative it beat.
- **Rules:** one line per constraint for writing code here, naming what enforces it if anything
  does.
- **What an agent gets wrong here:** the gotcha that looks safe and is not.

Claude Code loads a module's `CLAUDE.md` when it reads files in that directory, so the module's
context arrives with its code. A repo that already keeps `.claude/rules/*.md` files can go on
using them; the kit budgets and prunes them the same way, and a rules file without `paths:`
frontmatter loads in every session.

## Where to store context

| Content type | Store in | Why |
|---|---|---|
| What the project is for, who it serves | `docs/context/mission.md` | imported by root CLAUDE.md: every session |
| Domain knowledge, the central unit, invariants | `docs/context/domain-model.md` | imported by root CLAUDE.md: every session |
| Design decisions (chose X over Y) | `docs/context/design-decisions.md` | on demand, grows over time |
| A module's why, rules and gotchas | `{module}/CLAUDE.md` | loaded when working in that module |
| Feature specs, implementation plans | `docs/` | loaded only when needed |

**Every context file needs a routing row** in the root CLAUDE.md. The row is what reaches an
agent that is investigating a symptom and has not opened any of the module's files yet; the
module file loads by itself only once it does.

## What each surface costs: the deciding question

Where content goes is a cost decision before it is a taxonomy decision:

| Surface | Loads | Therefore |
|---|---|---|
| Root `CLAUDE.md` + everything it `@`-imports | every session, at launch | facts every turn needs. An import organises; it does not save a single token. |
| `{module}/CLAUDE.md` | when Claude reads files in that directory | the default for anything module-scoped |
| `.claude/rules/x.md` without `paths:` frontmatter | every session, at launch | only for a rule that applies to every turn |
| A skill | when invoked or its description matches, then stays for the session | multi-step procedures, not standing facts |
| Auto memory | on demand, by Claude | Claude's own notes; not yours to write |

The launch set is the number that matters: `context_check.py` sums it and fails past
`always_loaded_budget_chars`. Per-file caps alone never bound it: you add files instead.

A CLAUDE.md's block-level HTML comments (`<!-- ... -->`) are stripped before Claude Code loads
it, so a note for human maintainers costs nothing and the check does not count it.

**Auto memory is a separate system.** Claude writes its own notes under
`~/.claude/projects/<project>/memory/`. Do not copy them into context files and do not write into
that directory during a sync: CLAUDE.md is what you instruct, auto memory is what Claude learned.
A lesson the whole team needs moves into a module's CLAUDE.md.

## Capturing domain knowledge: research first, then ask

When the why behind some code is unclear, research before asking, and present a suggestion the
human can confirm or correct. People confirm or correct a suggestion much faster than they write
context from scratch, and a wrong suggestion with evidence still helps, because the correction
captures the real reason.

**Bare question:** "Why does the retry cap at 60s?"
**Informed suggestion:** "The retry caps at 60s. Most API rate limiters use 60-120s sliding
windows, and providers recommend matching the window. Is 60s the upstream's actual window, or
was it an arbitrary choice?"

Domain and business questions (entity rules, org conventions, product decisions) usually cannot
be researched: ask them directly. Never present a suggestion as fact. A wrong line in a context
file is worse than a gap.

## Size limits

The defaults below are the kit's; `.claude/context-kit.json` is the authority for this repo, and
`.claude/hooks/context_check.py` fails when one is exceeded.

| File type | Budget | Rationale |
|-----------|--------|-----------|
| Root CLAUDE.md | 60 lines | ~600 tokens, a hub. Everything it imports is paid at the same moment. |
| mission.md (imported) | 80 lines | ~800 tokens, rarely changes |
| domain-model.md (imported) | 80 lines | ~800 tokens, sometimes changes |
| design-decisions.md (not imported) | 100 lines | on demand; archive when full |
| Each module CLAUDE.md | 5000 chars (longest line 300) | ~1,250 tokens, on demand |
| Each `.claude/rules/*.md`, if any | 6000 chars (longest line 300) | ~1,500 tokens |
| Everything that loads at launch, together | 24000 chars (~6k tokens) | the only budget adding another file cannot evade |

**Characters, not lines, wherever a file has its own budget.** A line cap is satisfied by writing
longer lines. Lines survive only for the always-loaded files, as a proxy for "can a human still
scan this".

**Overflow for design-decisions.md:** when it passes 100 lines, move older entries to
`docs/context/design-decisions-archive.md` and keep about the 20 most recent.

## Pruning: lines move, they are never deleted

A context file only grows unless something takes lines out, and deleting on a guess loses the line
that mattered. So a line that leaves a live file moves, with a reason and evidence, to one of
three tiers:

| Tier | Where | Loaded |
|---|---|---|
| Live | module `CLAUDE.md` files and rules files | as above |
| Overflow | `docs/context/overflow/kit-<path-slug>.md`, reached by one pointer line at the end of the live file | on demand |
| Archive | `docs/references/lessons-archive/kit-<path-slug>.md`, plus `_gaps.md` | never |

The `kit-` prefix keeps the kit's files apart from any older prose files in the same folders. A
hand-written file there that starts with a `legacy:` line is searched by `lookup` and otherwise
left alone.

- **A line gets an ID only once it has a history.** `move` mints `[s:<id>]` the first time it
  moves a line; `restore` and `promote` insert it already tagged. Do not tag lines by hand, and
  never tag the root `CLAUDE.md`. An untagged line is addressed by its exact text.
- **`[s:<id> pin]`** keeps a line first in its section and out of demotion. Pins are capped at
  25% of the file's budget, so pin only what must never leave. Lines under `Hard Rules` or
  `Safety` headings, and the root `CLAUDE.md`, never move.
- **Every move has a closed reason and evidence** (`stale`, `derivable`, `duplicate`,
  `enforced`, `conflict`, `old-model`, `evicted`). Moves go through `context_upkeep.py
  move|restore|revise`, which check the evidence and write the archive entry; a hand edit that
  drops a line loses its history.
- **Restore on evidence.** One failure is noise; a session that failed for want of the line, with
  a quote, brings it back.
- **Gaps.** A lesson no live line covers is filed in `_gaps.md` with `seen`. A second sighting
  on a later day makes it a candidate for a live line (`promote`).
- Moving lines on session evidence is the audit's job (`/auditing-sessions`). `/context-sync`
  adds and rewords lines, and moves only what is stale, derivable or a duplicate.

## When to update context files

- A human explains a design decision: add it to design-decisions.md in the same turn.
- A new module is created: create its CLAUDE.md and its routing row.
- An agent makes a mistake the context would have prevented: add the gotcha.
- A convention changes: reword the line that states it.
- Code is deleted or restructured: move the stale lines out.

## When not to

- Code was reformatted (the linter handles it).
- A function was added or renamed (greppable).
- Tests were added (greppable).
- Dependencies were updated (the lockfile is authoritative).

## Staleness signals

A context file is stale when:
- it references files or functions that no longer exist;
- it describes patterns the code no longer follows;
- it omits modules or conventions that have been added;
- an agent working in that area makes a mistake the context should have prevented;
- two context files contradict each other.

## Flag removals separately

When proposing updates, list moves and rewordings separately from additions. Removing a line
that turns out to be needed costs more than keeping one that turns out to be noise.

## Design decisions format

Each entry in `docs/context/design-decisions.md`:

```
## YYYY-MM-DD: [Decision Title]
Chose [X] over [Y] because [Z].
Context: [what problem this solved or what constraint drove it]
```
