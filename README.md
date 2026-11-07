# context-kit

context-kit keeps a repo's context for Claude Code accurate over the life of a long project. It
installs three things:

- **A routed, size-capped context surface.** One `CLAUDE.md` per module says why the code is
  built the way it is, the rules for writing it, and what an agent gets wrong there. The root
  `CLAUDE.md` routes to them, and `docs/context/` holds the mission, the domain model and the
  design decisions.
- **A check** that fails when the context outgrows its budget or points at files that are
  gone, and notes a context file nothing routes to.
- **A maintenance loop**: `/context-sync` compares each module's code with its context file, and
  `/auditing-sessions` reads past sessions for context that was missing, wrong or ignored,
  then moves lines on that evidence.

It is plain Python 3.9+ with no packages, and CI tests it on Windows, macOS and Linux.

## Why it exists

Context files fail in a predictable way. They start useful, grow with every incident, and
end up too long for anyone (human or agent) to read. Meanwhile the code moves and the lines
that describe it go stale. The longer a project runs, the more of its context is wrong.
context-kit addresses both:

1. **Hard budgets.** Every file has a size cap, and the total that loads at session start is
   capped too. The check fails past either.
2. **A sync against the code.** `/context-sync` scans each module and proposes the lines that
   are now false, the facts the context lacks, and the bugs where code contradicts a line that
   is still right.
3. **A pruning loop.** Lines that are stale, derivable from the code, duplicated or over budget
   move out of the live files with evidence, and you approve each move. Nothing is deleted, and
   a line a later session needed comes back.

## Quick start

```
git clone https://github.com/msdels/context-kit.git
python context-kit/install.py <path-to-your-repo>
```

The installer only handles the mechanical part. It copies the files into the repo, merges one
SessionStart hook into `.claude/settings.json` alongside any hooks you already have, and writes
`.claude/context-kit.json` with the main branch and Python command it detected. It writes
nothing about your project.

Then open Claude Code in that repo and say:

```
read .claude/context-kit/INSTALL.md and set this repo up
```

Claude scans the repo, then asks you only what the code cannot tell it: what the project is
for, the rules that exist for reasons outside the repo, style the linter does not enforce, and
why each module is built the way it is. It writes the CLAUDE.md files and context docs, runs the
check, and commits.

### Run the check in CI

The check is what keeps the budget honest for every contributor. Add it to the workflow the repo
already has; on GitHub Actions:

```yaml
- uses: actions/setup-python@v5
  with:
    python-version: "3.12"
- run: python .claude/hooks/context_check.py
```

### Upgrading

Re-run `install.py` against the repo. Kit scripts and skills are replaced. `context-kit.json` is
never replaced. Templates are copied only if they are missing. A file an older kit installed and
this one no longer ships (the guards, the pre-push hook, two skills) stays on disk and is
unhooked from `settings.json`; the installer lists each one, and says when `core.hooksPath`
still points at `.claude/hooks`. Delete them, or move what you want to keep into the repo's own
hooks.

## How it works

### What lands in the repo

```
.claude/hooks/            context_check, the context_upkeep CLI and the audit machinery
.claude/skills/           context-sync, auditing-sessions
.claude/context-kit.json  every setting the scripts read
.claude/context-kit/      INSTALL.md and the templates, kept for the next /context-sync
CLAUDE.md, docs/context/  written by Claude during setup
<module>/CLAUDE.md        one per module, written by Claude during setup
```

### One file per module

Each module has one context file, `{module}/CLAUDE.md`, in three sections: why it is built this
way, the rules for writing code in it, and what an agent gets wrong there. Claude Code loads it
when it reads files in that directory, so the module's context arrives with its code. The root
routing table names every module file too, because it is what reaches an agent that is
investigating a symptom and has not opened the module yet.

Every line has to pass the grep test: *could an agent find this out from the code?* If yes, the
line does not belong in a context file. The full method is in
`.claude/skills/context-sync/context-guide.md`.

A repo that already keeps `.claude/rules/*.md` files can go on using them: the kit budgets,
routes and prunes them the same way.

### The context budget

Everything the root CLAUDE.md imports, plus every rules file without `paths:` frontmatter,
loads at launch in every session. Imports organize that context but do not shrink it.
`context_check` adds up that set, reports it as files, characters and estimated tokens, and
fails past `always_loaded_budget_chars` (default 24,000, about 6,000 tokens). Per-file caps
alone cannot bound the total, because the easy way around a per-file cap is another file.

Sizes are counted the way Claude Code loads them: a CLAUDE.md's block-level HTML comments are
stripped before loading, so they are not counted.

### The check

`context_check.py` fails on:
- a file over its size budget, or a launch set over its budget
- a routing row that points at a missing file, or an `@import` that resolves to nothing
- a BOM or mojibake in a context file
- a malformed overflow or archive entry, a duplicate line ID, or pins over their cap

and notes a context file nothing routes to.

### The pruning loop

Context files only grow unless something removes lines, and deleting on a guess loses the
one that mattered. So lines are moved, never deleted:

1. **Capture.** `context_upkeep.py` condenses each finished owner session into a small record
   under `.git/context-kit/`, which is never committed. Claude Code deletes transcripts after
   30 days; the records outlive them.
2. **Due notice.** After 10 records, or 21 days with at least one, the next session start
   prints `context: audit due`.
3. **`/auditing-sessions`.** Reads the sessions for context that was missing, wrong, stale or
   ignored. It restores a line a session failed without, and moves lines that are stale,
   derivable, duplicated or over budget to `docs/context/overflow/` (read on demand) or
   `docs/references/lessons-archive/` (never read), each with a reason and evidence. It also
   syncs the modules whose code changed most since their context did.
4. **Owner list.** Evictions, conflicts, pins and rewordings come to you as one numbered list.
   No line leaves a live file on the agent's judgment alone.

Each audit is committed as `context: audit ... [context-kit]` and can be reverted with
`git revert`.

### The headless audit

In the owner's clone, the kit can run the audit on its own.

- Turn it on per clone with `git config contextkit.autoAudit true`. This setting is never
  committed, so a teammate's clone never runs it.
- Once an audit is due, a session start runs `claude -p "/auditing-sessions"` in the
  background. `claude` must be on `PATH` or in `~/.local/bin`.
- One run is capped at `audit_budget_usd` ($25) and each month at `audit_month_usd` ($100).
- It works in a worktree next to the repo, `<parent>/.context-kit-wt-<repo name>`, on the
  branch `context/audit`. It never pushes or merges on its own.
- When it finishes, the next session start prints `context: audit ready` with the diff to
  review and the exact command to run. Answer with
  `python .claude/hooks/context_upkeep.py accept 1y 2n` or
  `python .claude/hooks/context_upkeep.py dismiss`.
- While a headless audit is running or waiting for review, a hand-run `/auditing-sessions`
  refuses to start, so one batch is never audited twice.
- In merge mode (the default), `accept` merges `context/audit` into `work_branch`, or the main
  branch if none is set, and pushes. With `require_pr_to_main: true` it pushes the branch and
  prints `gh pr create --fill --head context/audit`.
- A real audit on a clone took 5 to 6 minutes and cost $1.28 (Sonnet) to $2.86 (Opus). Whether
  runs draw on a subscription's usage pool has not been verified.

Audit commits run with git hooks turned off and are tagged `[context-kit]`, and the audit branch
is `context/audit`, so a commit-time review bot or CI filter can exclude both.

## Claude Code features it leans on

| You want | Use |
|---|---|
| Outdated or conflicting instructions found across CLAUDE.md, rules and skills | `/doctor prompt-audit` (Claude Code 2.1.283+) |
| What actually loaded in this session, and its size | `/context` |
| A rule that holds whatever Claude decides | `permissions.deny` in `.claude/settings.json`, or a hook |
| A first CLAUDE.md drafted from the code | `/init`, then restructure it with the kit's templates |
| Instructions shared with Codex, Cursor and others | `AGENTS.md`, imported from the root CLAUDE.md as `@AGENTS.md` (Claude Code reads AGENTS.md on its own only when there is no CLAUDE.md) |

CLAUDE.md files and rules are context: Claude reads them and usually complies, but nothing
forces it to. Only hooks and permissions are certain.

## What lives at which level

Everything the kit installs is **repo-level and committed**, so a clone gets the whole system.
Three things cannot be:

| Level | What | Why it cannot be repo-level |
|---|---|---|
| Per machine | Python 3.9+ (or the repo's runner) on `PATH` under the name in `settings.json` | `settings.json` is shared and no one command name works everywhere: macOS has `python3`, Windows has `python`. A mixed-OS team should use a runner the repo already requires, for example `--python "uv run --no-project python"`. |
| Per clone | `git config contextkit.autoAudit true` | Only the owner's clone should spend money on audits. |
| Per user | Claude's auto memory and personal preferences (`~/.claude/`, `CLAUDE.local.md`, `.claude/settings.local.json`) | Machine-local by design. A lesson the team needs moves into a module's CLAUDE.md. |

## Trade-offs to know before installing

- **The audit reads one person's sessions.** The capture and the due notice run only in the
  clone that turns `autoAudit` on, so a team's context is pruned from its owner's sessions.
  `/context-sync` reads code, not sessions, and works the same for everyone.
- **A failed hook prints an error and blocks nothing.** If `python_cmd` does not exist under
  that name on a teammate's machine, their session start shows a hook error.
- **Host linters.** `.claude/hooks/ruff.toml` makes a host `ruff check` pass on the kit's
  scripts. A host that runs `ruff --isolated`, `--config` or `ruff format --check` may still
  flag them; exclude `.claude/hooks/` there.

## Development

```
python -m unittest discover -s tests -v
python smoke_test.py
```

The tests use fake `claude` and `gh` binaries under `tests/fakes/`, so they run offline and
cost nothing. [INSTALL.md](INSTALL.md) is the full setup procedure Claude follows.

## License

MIT. See [LICENSE](LICENSE).
