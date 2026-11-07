# Set this repo up: the procedure Claude follows

You are configuring a context system that was mechanically copied into this repo. The files are
here; nothing about THIS project has been written yet. That is your job, and it ends with a
commit, not with a plan or a question.

Work through the steps in order. Steps 3 and 4 are the only ones that need the human.

---

## 0. What level everything lives at

**Write everything at repo level, committed:** `CLAUDE.md` files, `docs/context/`,
`.claude/skills/`, `.claude/hooks/`, `.claude/settings.json`, `.claude/context-kit.json`. A
teammate who clones must get the whole system. Never write to `~/.claude/`,
`.claude/settings.local.json` or `CLAUDE.local.md`: those belong to one person. If the owner
states a purely personal preference during the interview (tone, verbosity, their editor), say
that it belongs in THEIR `~/.claude/CLAUDE.md` and leave it out of the repo.

## 1. Read the method first

Read `.claude/skills/context-sync/context-guide.md` end to end. It is the authority on what
goes in a context file, the grep test, what each surface costs, and the size budgets. Everything
you write below is judged against it.

## 2. Scan the repo: answer everything you can without asking

Dispatch parallel read-only agents (or do it yourself if the repo is small) and establish:

| Question | Where to look |
|---|---|
| What does this project do, for whom? | README, docs/, package metadata, the landing page if there is one |
| Languages, package manager, runtime | manifest files (`pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, ...) |
| How tests, lint and build run | CI config, scripts/, Makefile, manifest scripts, contributor docs |
| The module boundaries | top-level source dirs, the import graph, what each dir owns |
| Existing agent context | `CLAUDE.md`, `AGENTS.md`, `.claude/rules/`, `.cursorrules`, `.github/copilot-instructions.md` |
| Code style the linter does NOT enforce | 3-4 representative source files: error handling, naming, layering, comment density |
| Files that must never be hand-edited | migrations, generated code, vendored dirs, lockfiles |

**Do not ask the human anything you can read.** The interview is for what the code cannot tell
you: intent, priorities, and the rules that exist for a reason outside the repo.

- **No CLAUDE.md yet?** Run `/init` first and treat its output as a draft to restructure into the
  templates, not as the finished file.
- **Existing agent instructions are reconciled, never stacked.** Two files giving different
  guidance on the same behaviour means Claude picks one arbitrarily.
  - If the repo has `AGENTS.md`, put `@AGENTS.md` at the top of the root CLAUDE.md and add
    only Claude-specific lines below it. The import is required: Claude Code reads AGENTS.md on
    its own only when no CLAUDE.md exists.
  - Anything you keep from `.cursorrules` or a Copilot file moves into the new surface, and the
    old copy is deleted or reduced to a pointer.
  - Existing `.claude/rules/*.md` files can stay; the kit budgets and prunes them the same way.
    Propose folding a module's rules file into that module's CLAUDE.md, and do it only if the
    owner agrees.
  - List every duplicate or contradiction you found in your report.

## 3. Interview: informed suggestions, never bare questions

Use `AskUserQuestion`, batched (up to 4 questions a call), each option carrying what you found
in the scan. The pattern from the guide: *I noticed X. Most repos like this do Y. Is that right,
or is the real reason something else?* A human corrects a wrong suggestion far faster than they
write context from scratch.

Cover, at minimum:

1. **What the project is for, and who it serves in priority order.** This is the one paragraph a
   new agent should never get wrong. Offer a draft from the README; ask what it gets wrong.
2. **The hard rules.** These are the things that must never happen, whatever a later instruction
   says (safety, legal, data handling, "this number always comes from the source"). They become
   the Hard Rules block in CLAUDE.md. Ask for the *reason* behind each: a rule with no reason
   gets argued away within weeks.
3. **Code style the linter does not catch:** flat vs layered, abstraction appetite, error
   handling, comment density, typing discipline, where new files go. Propose what the code
   already does and ask what is aspiration and what is accident.
4. **Module ownership.** For each module you found, one line on *why it exists* and the
   constraint an agent gets wrong by default. This is the single highest-value thing in the
   whole system and the thing only they know.
5. **Python.** Confirm the python command the installer detected exists under that name on
   EVERY collaborator's machine. macOS has `python3` and no `python`; Windows is the reverse. A
   repo-mandated runner such as `uv run --no-project python` is the only name a mixed team is
   sure to share.
6. **The audit.**
   - Offer the settings, with the defaults: `audit_budget_usd` (25, per run),
     `audit_month_usd` (100), `audit_model` (blank: the session's own).
   - Say how the run is billed: a headless run reports `total_cost_usd` and is capped by
     `--max-budget-usd`; whether it draws on a subscription's usage pool is not verified.
   - Ask for `main_branch` and `work_branch` (where `accept` merges an audit), and whether
     main takes changes only through PRs (`require_pr_to_main`).
   - Last, ask whether this clone is the owner's. If yes, run
     `git config contextkit.autoAudit true`. It lives in this clone's `.git/config`, so it is
     never committed and a teammate's clone stays silent.

## 4. Write the context surface

From `.claude/context-kit/templates/`, filling every `{{PLACEHOLDER}}` and deleting every
section that does not apply. Keep inside the size budgets; step 6 checks them.

| Write | From | Notes |
|---|---|---|
| `CLAUDE.md` (root) | `CLAUDE.md.tmpl` | The hub: @-imports, the routing table, context rules, hard rules, code style. At most 60 lines. |
| `docs/context/mission.md` | `docs-context/mission.md.tmpl` | What the project is and who it is for. Only if the repo has a product; skip it for a library and drop the import. |
| `docs/context/domain-model.md` | `docs-context/domain-model.md.tmpl` | The nouns, their relationships, and the modelling decision agents get wrong. |
| `docs/context/design-decisions.md` | `docs-context/design-decisions.md` | Starts nearly empty and grows one entry per ruling. |
| `<module>/CLAUDE.md` | `module-CLAUDE.md.tmpl` | One per module: why it is built this way, its rules, its gotchas. |

Every file you create gets a row in the routing table, or the check in step 6 reports it as
unrouted. Skip a module whose constraints you cannot yet name: an empty context file is worse
than none, and `/context-sync` will propose one later from real code.

**What each file costs, which decides where content goes:**

- Root `CLAUDE.md` and everything it `@`-imports load **at launch, in every session**. Imports
  organise; they do not save context. Keep the whole launch set inside
  `always_loaded_budget_chars`; step 6 measures it.
- A module's `CLAUDE.md` loads only when Claude works in that directory. Anything
  module-specific goes there, not in the root.
- A **skill** loads only when invoked or when its description matches the prompt. Multi-step
  procedures belong there, not in CLAUDE.md.
- Anything Claude can derive from the code belongs in none of them.

**Nothing here is enforcement.** CLAUDE.md files are context: Claude reads them and usually
complies. For a rule that must hold regardless (a migrations directory nobody edits, a command
nobody runs), propose a `permissions.deny` entry in `.claude/settings.json`, e.g.
`"Edit(db/migrations/**)"`, and add it if the owner agrees.

## 5. Write the config, and seed the ledger

`.claude/context-kit.json`: fill `main_branch`, `work_branch`, `require_pr_to_main`,
`python_cmd`, the audit settings from step 3.6, and the `size_limits` entries for the context
files you actually created. The scripts read only this file.

`always_loaded_budget_chars` (default 24000, ~6k tokens) is the one to set deliberately: it caps
CLAUDE.md plus its imports plus any unscoped rules file. Raise it only against a measured need,
and say in your report what the repo actually came in at.

**Seed the ledger.** The audit reads only sessions after the newest ledger's `covered_until`,
and the first audit compares its models against the seed's. Run

```
<python_cmd> .claude/hooks/context_upkeep.py ledger --seed --models <your model ID without a -YYYYMMDD suffix>
```

It captures the sessions already on disk, then writes
`docs/references/session-audits/<UTC YYYY-MM-DD-HHMM>.md`. In a fresh repo its `covered_until` is
now. When sessions were captured (a repo with history), it is 1 s before the earliest one
starts, so the first audit reads every one of them. Do not write the seed by hand: a
`covered_until` of now hides every captured session. `--models` is needed only when no session
is captured yet, and otherwise overrides the captured families. It refuses when a ledger
already exists. Commit the seed in the setup commit.

## 6. Verify

Do not report success from the fact that files exist.

```
<python_cmd> .claude/hooks/context_check.py      # must print OK, or name what to fix
```

The check prints the launch budget: how many files and roughly how many tokens every session pays
before the first prompt. Put that number in your report; it is the figure this system exists to
hold down.

Then confirm the files actually load, which only the harness can tell you: run **`/context`** and
check the **Memory files** list for the root CLAUDE.md and each `@`-import. A file that is on
disk and absent there is invisible; the most common cause is an import written inside
backticks.

If the repo has CI, add a step that runs `<python_cmd> .claude/hooks/context_check.py`, so the
budgets hold for every contributor (the README has a GitHub Actions snippet). `.claude/hooks/ruff.toml`
keeps a host ruff off the kit's scripts; any other Python linter the CI runs (flake8, pylint,
mypy) needs `.claude/hooks/` excluded.

## 7. Commit, and tell them what they now have

Stage by name, commit, and report in a handful of lines: the files written, the check output with
the launch budget, the CI step if you added one, and the two things they can now do:

- `/context-sync`: re-scan modules, catch stale context and the bugs it points at, and propose
  updates. Run it monthly, or after any big refactor.
- `/auditing-sessions`: audit past sessions for context that was missing, wrong or ignored, and
  prune the context files. The session-start line says when one is due.

Name anything you could not fill in and what you would need to fill it.

## The headless audit

In the owner's clone, the kit runs `/auditing-sessions` by itself once an audit is due. Tell
the owner how it works:

- **Per clone.** `git config contextkit.autoAudit true` turns it on. It lives in `.git/config`,
  so it is never committed, and a teammate's clone never runs it.
- **The run.** At a session start, `context_upkeep.py` starts a detached helper, and
  `upkeep_audit.py` runs `claude -p "/auditing-sessions"`. `claude` must be on `PATH` or in
  `~/.local/bin`.
- **Budgets.** `audit_budget_usd` caps one run (default $25) through `--max-budget-usd`.
  `audit_month_usd` caps the month (default $100): no run starts that could pass it.
- **Where.** A worktree beside the repo, `<parent>/.context-kit-wt-<repo name>`, on the branch
  `context/audit`. The run commits there. It never pushes or merges.
- **State.** `.git/context-kit/` holds `state.json` and `audit.log`. None of it is committed.
- **The prompt.** When a run is ready, the next session start prints a line like
  `context: audit ready: 1 move, 0 restores, 1 item. Review: git diff <base>...context/audit ·
  then: python .claude/hooks/context_upkeep.py accept 1y   (or: python
  .claude/hooks/context_upkeep.py dismiss)`. The `accept` answers it prints are the defaults.
- **Answer it.**
  - `<python_cmd> .claude/hooks/context_upkeep.py accept 1y 2n` takes the audit. `y` keeps an
    applied move or runs an apply. `n` undoes a move or skips an apply.
  - `<python_cmd> .claude/hooks/context_upkeep.py dismiss` drops it. Its sessions count as
    audited.
  - Until one of the two runs, a hand-run `/auditing-sessions` refuses to start.
- **Merge mode** (`require_pr_to_main: false`, the default). `accept` needs a clean tree, the
  target branch (`work_branch`, else `main_branch`) checked out, and that branch equal to
  `origin`. It merges `context/audit` with `--no-ff` and pushes.
- **PR mode** (`require_pr_to_main: true`). `accept` pushes `context/audit` and prints
  `gh pr create --fill --head context/audit`. No new audit starts until that PR merges.
- **Cost.** A run reports `total_cost_usd`, and the month is charged that. A real audit on a
  clone took 5 to 6 minutes and cost $1.28 (Sonnet) to $2.86 (Opus).
- **Lint.** `.claude/hooks/ruff.toml` makes a host `ruff check` pass on the kit's scripts. A host
  that runs ruff with `--isolated` or `--config`, or runs `ruff format --check`, may still flag
  them: exclude `.claude/hooks/` there.
