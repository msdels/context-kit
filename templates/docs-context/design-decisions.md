# Design Decisions

One entry per decision, newest first. When this file passes its size budget, sweep the oldest
entries into `design-decisions-archive.md` beside it and keep roughly the last twenty.

A decision belongs here the turn it is made, not the turn it is implemented. The reason is what
goes stale first, and it is the only part that cannot be recovered from the code later.

## Format

```
## YYYY-MM-DD: <Decision title>
Chose <X> over <Y> because <Z>.
Context: <what problem this solved, or what constraint forced it>
```

## YYYY-MM-DD: Adopted context-kit
Chose a routed, size-capped context surface with a mechanical check and a pruning loop over
ad-hoc CLAUDE.md files.
Context: context files go stale in mechanical ways (they outgrow being read, they point at
moved files, they collect surfaces nothing routes to) and in ways only a session shows (a line
that misled, a lesson that came back). The check catches the first kind; the audit the second.
