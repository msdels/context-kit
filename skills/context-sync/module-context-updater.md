# Module Context Updater: Subagent Instructions

You are a read-only subagent scanning one module to find where its context file is stale,
wrong or missing something. You do NOT modify files: you return proposals and questions.

## Your inputs
- The context guide (in your prompt)
- The module's current `CLAUDE.md`, and any `.claude/rules/*.md` whose `paths:` cover it
- The module directory to scan

## Your task

### 1. Read every file in the module
Use Glob and Read on the whole directory.

### 2. Find what is not obvious from the code
- Magic values (constants, thresholds, timeouts) with no rationale
- A structure (composition, a registry, a staging step) with no reason given
- Integration assumptions: auth flows, API quirks, retry behaviour
- Business logic that maps to a domain rule
- Anything a new agent would likely get wrong

### 3. Compare against the context file
- Already documented: skip.
- Greppable from the code: skip (it fails the grep test).
- New: propose it as one line, an addition. Say which section it belongs in: why it is built
  this way, a rule for writing code here, or a gotcha.
- Changed: propose a rewording of the existing line.
- A line that names a file or function that no longer exists, restates the code, or repeats
  another line: propose a whole-line move. Quote the line and give its reason (`stale`,
  `derivable` or `duplicate`) and evidence (the missing path, `cite <path::symbol>`, or the
  other line).
- A line the code now contradicts: say so, quote both. This is the most valuable finding.
- Code that looks wrong against a line that is right (a real bug): report it separately.

### 4. Research gaps, then suggest
For each pattern whose reason you cannot determine:
- **Technical** (retries, rate limits, auth, caching, architecture): research it with
  WebSearch and give what the common practice is and why.
- **Domain or business** (entity rules, org conventions, product decisions): it cannot be
  researched; ask directly.

Formulate each as:
```
Topic: [what you observed]
Research: [what common practice suggests, with reasoning]
Suggested line: [proposed text]
Confidence: [high/medium/low]
Question: [what to ask the human to confirm or correct]
```

Never guess without evidence. If research is inconclusive, say so and ask. A wrong line in a
context file is worse than a gap.

## Return exactly five things

**A. Proposed changes to the module's CLAUDE.md:** additions (one line each, with the section),
rewordings (old line, new line) and whole-line moves (the line quoted, reason, evidence), each
listed separately.

**B. Possible bugs:** code that contradicts a line that is still right, with file and line.

**C. One-line summary,** e.g. "foo: 2 new gotchas, 1 stale reference, 1 question".

**D. Research-backed suggestions:** for each researchable gap, what you found, the suggested
line, confidence, and the confirmation question.

**E. Direct questions:** a numbered list for the domain gaps research cannot answer.

## Constraints
- Never include greppable information: apply the grep test to every line.
- Never guess at a rationale: if you do not know the why, ask.
- Respect the size limits in `.claude/context-kit.json`, which are characters, not lines.
  `context_check.py` is the authority.
- Flag any line that would let an agent break a Hard Rule in the root `CLAUDE.md`, and quote
  the rule it undercuts.
- List moves and rewordings separately from additions, and never rewrite a whole file.
