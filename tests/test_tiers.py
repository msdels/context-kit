"""Task 3: tiers and guarded moves — parsing, entries, lookup, moves, restores and their guards."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_tiers` and discover
import _util  # noqa: E402  (isolates the environment on import)
import _kit  # noqa: E402
import upkeep_tiers as ut  # noqa: E402

UPKEEP = Path(ut.__file__).resolve().parent / "context_upkeep.py"
TODAY = date(2026, 10, 31)
T0 = f"{TODAY}T00:00:00Z"  # a move row stamp for a date-only `today`
EARLY = "2026-10-30T10:00:00.000Z"  # before the moves (dated TODAY)
LATER = "2026-11-02T10:00:00.000Z"  # after them
MAP = "map HTTP status to a FooError subclass"

API_RULES = """---
paths:
  - "src/api/**"
---
# API Conventions

## Errors
- old pinned line [s:keep-me pin]
- map HTTP status to a FooError subclass
  - nested detail
- never retry a POST [s:no-post-retry]

```text
- not a line
```

## Safety
- never log the auth header
"""
GENERAL = "# General\n\n## Habits\n- run the tests before pushing\n- prefer small diffs\n"
MODULE = "# api — why\n\n## What an agent gets wrong here by default\n- the cache is per-process\n"
API, GEN, MOD = ".claude/rules/api.md", ".claude/rules/general.md", "src/api/CLAUDE.md"


def rec(sid, *stamps, files=()):
    return {"id": sid, "turns": [{"ts": s, "owner": "x"} for s in stamps],
            "files": [str(f) for f in files], "ts_end": stamps[-1] if stamps else "", "cwd": ""}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(dir=_util.TMP)
        self.addCleanup(self._tmp.cleanup)
        self.repo = _util.make_repo(Path(self._tmp.name).resolve())
        _util.write(self.repo, API, API_RULES)
        _util.write(self.repo, GEN, GENERAL)
        _util.write(self.repo, MOD, MODULE)
        _util.commit(self.repo, "context")

    def read(self, rel) -> str:
        return (self.repo / rel).read_text(encoding="utf-8")

    def move(self, ref, tier="archive", reason="derivable", evidence="cite README.md", **kw):
        return ut.move(self.repo, ref, tier, reason, evidence, TODAY, **kw)


class Parsing(Base):
    def test_parse_live_optional_ids_crlf(self):
        lines = ut.parse_live(self.repo, API)
        self.assertEqual([(l.text, l.id, l.pinned, l.protected, l.section) for l in lines], [
            ("old pinned line", "keep-me", True, True, "Errors"),
            (MAP, None, False, False, "Errors"),
            ("never retry a POST", "no-post-retry", False, False, "Errors"),
            ("never log the auth header", None, False, True, "Safety")])
        _util.write(self.repo, API, API_RULES, crlf=True)
        self.assertEqual(ut.parse_live(self.repo, API), lines)
        self.assertEqual(ut.live_files(self.repo), [API, GEN, MOD])


class Moves(Base):
    def test_move_mints_id_and_pointer(self):
        out = self.move(MAP, tier="overflow")
        self.assertEqual(out, {"id": "map-http-status-to-a", "needs_owner": None,
                               "second_sighting": False})
        over = self.read("docs/context/overflow/kit-claude-rules-api.md")
        self.assertIn("## s:map-http-status-to-a — ", over)
        self.assertIn(f"- line: {MAP}\n  - nested detail\n- from: {API}#Errors\n", over)
        self.assertIn(f"- {T0} moved overflow · derivable · cite README.md", over)
        self.move("no-post-retry", tier="overflow")
        live = self.read(API)
        self.assertNotIn(MAP, live)
        self.assertNotIn("nested detail", live)
        self.assertNotIn("never retry", live)
        self.assertEqual(live.count("docs/context/overflow/kit-claude-rules-api.md"), 1)
        self.assertEqual([l.text for l in ut.parse_live(self.repo, API)],
                         ["old pinned line", "never log the auth header"])  # pointer skipped
        self.assertEqual(ut.mint_id(self.repo, "map HTTP status to a new thing"),
                         "map-http-status-to-2")

    def test_move_never_writes_into_a_prekit_file_of_the_same_slug(self):
        prose = "# Pre-kit notes on the API\n\nStories, no entries.\n"
        for rel in ("docs/references/lessons-archive/claude-rules-api.md",
                    "docs/context/overflow/claude-rules-api.md"):
            _util.write(self.repo, rel, prose)
        self.move(MAP)
        self.move("no-post-retry", tier="overflow")
        for rel in ("docs/references/lessons-archive/claude-rules-api.md",
                    "docs/context/overflow/claude-rules-api.md"):
            self.assertEqual(self.read(rel), prose)
        self.assertIn("## s:map-http-status-to-a ",
                      self.read("docs/references/lessons-archive/kit-claude-rules-api.md"))
        self.assertEqual([e.id for e in ut.load_entries(self.repo)],
                         ["no-post-retry", "map-http-status-to-a"])

    def test_place_moves_an_entry_out_of_an_old_named_file_and_nothing_else(self):
        """An older kit named its files <slug>.md. An entry in one is re-homed to its kit- file;
        the old file's prose and its other entries stay byte for byte (the owner may have edited)."""
        self.move(MAP, tier="overflow")
        self.move("no-post-retry", tier="overflow")
        new = self.repo / "docs/context/overflow/kit-claude-rules-api.md"
        old = self.repo / "docs/context/overflow/claude-rules-api.md"
        new.rename(old)
        _util.write(self.repo, "docs/context/overflow/claude-rules-api.md",
                    "Notes the owner added above the entries.\n\n" + old.read_text(encoding="utf-8"))
        before = old.read_text(encoding="utf-8")
        entry = next(e for e in ut.load_entries(self.repo) if e.id == "no-post-retry")
        out = ut._place(self.repo, entry, "overflow", {})
        kept = out[old]
        self.assertTrue(kept.startswith("Notes the owner added above the entries.\n\n"))
        self.assertIn("## s:map-http-status-to-a ", kept)
        self.assertNotIn("no-post-retry", kept)
        chunks = re.split(r"(?m)^(?=## s:)", before)
        self.assertEqual(kept, "".join(c for c in chunks if not c.startswith("## s:no-post-retry")))
        self.assertIn("## s:no-post-retry ", out[new])

    def test_move_evidence_required(self):
        for ev in ("", "README.md::NoSuchSymbol", "cite nope/missing.py"):
            with self.assertRaises(ut.GuardError):
                self.move("prefer small diffs", reason="enforced", evidence=ev)
        with self.assertRaises(ut.GuardError):
            self.move("run the tests before pushing", reason="duplicate", evidence="no-such-id")
        # an untagged partner in the same file, named by its text, is tagged in the same write,
        # so the row's ID resolves
        self.move("run the tests before pushing", reason="duplicate", evidence="prefer small diffs")
        self.assertIn("- prefer small diffs [s:prefer-small-diffs]", self.read(GEN))
        self.assertIn("\u00b7 duplicate \u00b7 prefer-small-diffs",
                      self.read("docs/references/lessons-archive/kit-claude-rules-general.md"))
        # exact twins: the bottom one moves, the top one stays and is tagged as its partner
        _util.write(self.repo, ".claude/rules/twins.md",
                    "# T\n\n## A\n- keep diffs small\n- other\n- keep diffs small\n")
        self.move("keep diffs small", reason="duplicate", evidence="")
        self.assertRegex(self.read(".claude/rules/twins.md"),
                         r"^# T\n\n## A\n- keep diffs small \[s:([a-z0-9-]+)\]\n- other\n$")
        self.move("prefer-small-diffs", reason="enforced", evidence="cite README.md::trial")
        with self.assertRaises(ut.GuardError):  # protected: under Safety
            self.move("never log the auth header")
        with self.assertRaises(ut.GuardError):  # protected: pinned
            self.move("keep-me")
        _util.write(self.repo, ".claude/rules/misc.md",
                    "# M\n\n## A\n- see `docs/gone.md` for the setup\n"
                    "- think step by step before every edit\n- keep it short\n\n"
                    "## Hard Rules\n- never force-push\n")
        for ev in ("README.md", "docs/other.md"):  # exists; missing but not named by the line
            with self.assertRaises(ut.GuardError):
                self.move("see `docs/gone.md` for the setup", reason="stale", evidence=ev)
        self.move("see `docs/gone.md` for the setup", reason="stale", evidence="docs/gone.md")
        with self.assertRaises(ut.GuardError):
            self.move("think step by step before every edit", reason="old-model",
                      evidence="be careful")
        self.move("think step by step before every edit", reason="old-model",
                  evidence="step by step")
        with self.assertRaises(ut.GuardError):  # protected: Hard Rules
            self.move("never force-push")
        _util.write(self.repo, ".claude/context-kit.json",  # `contract_path`: also never moves
                    json.dumps({"contract_path": "docs/context/operating-contract.md"}))
        _util.write(self.repo, "docs/context/operating-contract.md",
                    "# Contract\n\n## Every turn\n- answer the live ask first\n")
        _util.write(self.repo, "CLAUDE.md", "# Root\n\n## Notes\n- read the routing table\n")
        for text in ("answer the live ask first", "read the routing table"):  # never moved
            with self.assertRaisesRegex(ut.GuardError, "protected"):
                self.move(text, owner=True)
        (self.repo.parent / "outside.txt").write_text("x", encoding="utf-8")
        for ev in ("cite src/api", "cite ../outside.txt"):  # a directory; outside the repo
            with self.assertRaises(ut.GuardError):
                self.move("keep it short", reason="enforced", evidence=ev)

    def test_conflict_requires_owner(self):
        with self.assertRaises(ut.GuardError):
            self.move("prefer small diffs", reason="conflict", evidence="no-post-retry")
        with mock.patch.dict(os.environ, {"CONTEXT_KIT_HEADLESS": "1"}):
            with self.assertRaises(ut.GuardError):
                self.move("prefer small diffs", reason="conflict", evidence="no-post-retry",
                          owner=True)
        self.move("prefer small diffs", reason="conflict", evidence="the cache is per-process",
                  owner=True)
        self.assertNotIn("prefer small diffs", self.read(GEN))
        self.assertIn("- the cache is per-process [s:the-cache-is-per]", self.read(MOD))

    def test_judged_reason_blocked_after_restore(self):
        mid = self.move("prefer small diffs")["id"]
        ut.restore(self.repo, mid, None, "", "", TODAY, owner=True)
        self.assertIn(f"- prefer small diffs [s:{mid}]", self.read(GEN))
        for reason in ("derivable", "enforced"):
            with self.assertRaises(ut.GuardError):
                self.move(mid, reason=reason)
        self.move(mid, owner=True)
        self.assertNotIn("prefer small diffs", self.read(GEN))

    def test_eviction_is_only_the_pick(self):
        with self.assertRaises(ut.GuardError):  # live: the bottom-most line is the pick
            ut.move(self.repo, "run the tests before pushing", "archive", "evicted", "", TODAY)
        old = self.move("run the tests before pushing", tier="overflow")["id"]
        new = self.move("prefer small diffs", tier="overflow")["id"]
        with self.assertRaises(ut.GuardError):  # the newest overflow entry is not the choice
            ut.move(self.repo, new, "archive", "evicted", "", TODAY)
        ut.move(self.repo, old, "archive", "evicted", "", TODAY)
        over = self.read("docs/context/overflow/kit-claude-rules-general.md")
        arch = self.read("docs/references/lessons-archive/kit-claude-rules-general.md")
        self.assertNotIn(f"s:{old}", over)
        self.assertIn(f"s:{new}", over)
        self.assertIn(f"## s:{old}", arch)
        self.assertIn(f"{T0} moved overflow · derivable · cite README.md\n"
                      f"- {T0} moved archive · evicted · budget", arch)

    def test_net_removal_cap_counts_restores(self):
        mid = self.move("prefer small diffs")["id"]
        base = _util.commit(self.repo, "moved")  # 6 live lines: the cap is ceil(0.6) = 1
        state = _kit.upkeep_dir(self.repo) / "state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"base": base}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CONTEXT_KIT_HEADLESS": "1"}):
            ut.restore(self.repo, mid, rec("s1", LATER), LATER, "it is gone", TODAY)
            ut.move(self.repo, "run the tests before pushing", "archive", "evicted", "", TODAY)
            ut.move(self.repo, mid, "archive", "evicted", "", TODAY)
            with self.assertRaisesRegex(ut.GuardError, "cap"):
                ut.move(self.repo, "no-post-retry", "archive", "evicted", "", TODAY)
        ut.move(self.repo, "no-post-retry", "archive", "evicted", "", TODAY)  # not headless

    def test_crlf_written_back_lf(self):
        _util.write(self.repo, GEN, GENERAL, crlf=True)
        self.move("prefer small diffs")
        data = (self.repo / GEN).read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertEqual(data.decode("utf-8"), "# General\n\n## Habits\n- run the tests before pushing\n")


class Restores(Base):
    def test_restore_guards_turn_ts(self):
        mod = self.move("the cache is per-process")["id"]
        api = self.move(MAP)["id"]
        touched_api = [self.repo / "src/api/h.py"]
        with self.assertRaises(ut.GuardError):  # a turn the record does not hold
            ut.restore(self.repo, api, rec("s0", LATER, files=touched_api),
                       "2026-11-03T10:00:00.000Z", "q", TODAY)
        # a turn before the move, though the session ended after it
        with self.assertRaises(ut.GuardError):
            ut.restore(self.repo, mod, rec("s1", EARLY, LATER, files=touched_api), EARLY, "q", TODAY)
        with self.assertRaises(ut.GuardError):  # outside `paths:`
            ut.restore(self.repo, api, rec("s2", LATER, files=[self.repo / "docs/x.md"]), LATER,
                       "q", TODAY)
        with self.assertRaises(ut.GuardError):  # +25%
            ut.restore(self.repo, api, rec("s3", LATER, files=touched_api), LATER, "q", TODAY,
                       text=MAP + "x" * 10)
        out = ut.restore(self.repo, api, rec("s3", LATER, files=touched_api), LATER, "q", TODAY)
        self.assertIsNone(out["needs_owner"])
        live = self.read(API).split("\n")
        i = live.index("- old pinned line [s:keep-me pin]")  # top of the section, below the pin
        self.assertEqual(live[i + 1:i + 4], [f"- {MAP} [s:{api}]", "  - nested detail",
                                             "- never retry a POST [s:no-post-retry]"])
        arch = self.read("docs/references/lessons-archive/kit-claude-rules-api.md")
        self.assertEqual(arch.count(f"## s:{api} "), 1)  # updated in place, not appended
        self.assertIn(f'restored · s3 · {LATER} · "q"', arch)
        # the module-directory variant
        with self.assertRaises(ut.GuardError):
            ut.restore(self.repo, mod, rec("s4", LATER, files=[self.repo / "src/other/a.py"]),
                       LATER, "q", TODAY)
        ut.restore(self.repo, mod, rec("s4", LATER, files=[self.repo / "src/api/a.py"]),
                   LATER, "q" * 500, TODAY)
        arch = self.read("docs/references/lessons-archive/kit-src-api-claude.md")
        self.assertIn(f'- 2026-10-31 restored · s4 · {LATER} · "{"q" * 200}"', arch)
        self.assertNotIn("q" * 201, arch)
        # a live conflict partner
        con = self.move("prefer small diffs", reason="conflict", evidence="no-post-retry",
                        owner=True)["id"]
        with self.assertRaises(ut.GuardError):
            ut.restore(self.repo, con, rec("s5", LATER), LATER, "q", TODAY)
        # an unscoped file accepts any session
        run = self.move("run the tests before pushing")["id"]
        ut.restore(self.repo, run, rec("s6", LATER), LATER, "q", TODAY)
        self.assertIn(f"[s:{run}]", self.read(GEN))
        with self.assertRaises(ut.GuardError):  # already live
            ut.restore(self.repo, run, rec("s7", LATER), LATER, "q", TODAY)

    def test_revise_cap_and_owner_override(self):
        with self.assertRaises(ut.GuardError):  # 26 > 18 * 1.2
            ut.revise(self.repo, "prefer small diffs", "prefer small diffs, always", None, "", TODAY)
        out = ut.revise(self.repo, "prefer small diffs", "prefer smaller diffs", rec("s1"), "q",
                        TODAY)
        rid = out["id"]
        self.assertIn(f"- prefer smaller diffs [s:{rid}]", self.read(GEN))
        # 23: within 20% of the current, not the first; the refusal names the split
        with self.assertRaisesRegex(ut.GuardError, r"stale.*`seen`"):
            ut.revise(self.repo, rid, "prefer smaller diffs ok", rec("s1"), "q", TODAY)
        ut.revise(self.repo, rid, "prefer smaller diffs ok", None, "", TODAY, owner=True)
        self.assertIn(f"- prefer smaller diffs ok [s:{rid}]", self.read(GEN))
        entry = next(e for e in ut.load_entries(self.repo) if e.id == rid)
        self.assertEqual(entry.line, "prefer small diffs")

    def test_revise_replaces_the_whole_block(self):
        # a wrapped bullet: the reword takes its continuation lines too, the archive row keeps both
        # old lines, and the growth cap measures the whole block
        _util.write(self.repo, GEN, "# General\n\n## Habits\n- run the tests\n  before pushing\n"
                    "- prefer small diffs\n")
        rid = ut.revise(self.repo, "run the tests", "run the suite\nbefore every push", None, "",
                        TODAY, owner=True)["id"]
        self.assertIn(f"- run the suite [s:{rid}]\n  before every push\n- prefer small diffs\n",
                      self.read(GEN))
        self.assertNotIn("before pushing", self.read(GEN))  # no stray continuation
        entry = next(e for e in ut.load_entries(self.repo) if e.id == rid)
        self.assertEqual((entry.line, entry.more), ("run the tests", ["  before pushing"]))
        with self.assertRaises(ut.GuardError):  # fits as a first line alone, not as a block
            ut.revise(self.repo, rid, "run the suite\nbefore every push of work", None, "", TODAY)

    def test_revise_refuses_a_one_line_text_for_a_wrapped_bullet(self):
        # 2026-11-01: a one-line reword of a wrapped bullet silently dropped its continuation
        _util.write(self.repo, GEN, "# General\n\n## Habits\n- run the tests\n  before pushing\n")
        before = self.read(GEN)
        with self.assertRaisesRegex(ut.GuardError, "wraps over 2 lines"):
            ut.revise(self.repo, "run the tests", "run the suite", None, "", TODAY, owner=True)
        self.assertEqual(self.read(GEN), before)

    def test_wrapped_bullet_round_trip_keeps_the_first_wording(self):
        # first block -> multi-line revise -> move -> restore -> revise again: the archive's first
        # wording stays whole, a restore brings back the LATEST block, and " -> " in text survives
        _util.write(self.repo, GEN, "# General\n\n## Habits\n- run the tests\n  before pushing\n")
        first = ("run the tests", ["  before pushing"])
        rid = ut.revise(self.repo, "run the tests", "run the suite → ok\nbefore every push",
                        None, "a → b", TODAY, owner=True)["id"]
        self.move(rid)
        entry = next(e for e in ut.load_entries(self.repo) if e.id == rid)
        self.assertEqual((entry.line, entry.more), first)
        ut.restore(self.repo, rid, today=TODAY, owner=True)
        self.assertIn(f"## Habits\n- run the suite → ok [s:{rid}]\n  before every push\n",
                      self.read(GEN))
        ut.revise(self.repo, rid, "run the suite → ok\n  before every push\n    and the lint",
                  None, "", TODAY, owner=True)
        entry = next(e for e in ut.load_entries(self.repo) if e.id == rid)
        self.assertEqual((entry.line, entry.more), first)
        self.move(rid, owner=True)
        ut.restore(self.repo, rid, today=TODAY, owner=True)
        self.assertIn(f"- run the suite → ok [s:{rid}]\n  before every push\n    and the lint\n",
                      self.read(GEN))

    def test_loose_list_block_moves_with_its_bullet(self):
        """A blank line, then an indented block: CommonMark makes it the bullet's, so it goes
        with it, rather than staying behind under whatever bullet is above."""
        _util.write(self.repo, GEN, "# General\n\n## Habits\n- prefer small diffs\n- run the tests"
                                    "\n\n  before pushing, always\n\n- keep commits small\n")
        rid = self.move("run the tests")["id"]
        self.assertNotIn("before pushing", self.read(GEN))
        ut.restore(self.repo, rid, today=TODAY, owner=True)
        self.assertIn(f"- run the tests [s:{rid}]\n", self.read(GEN))
        self.assertIn("  before pushing, always\n", self.read(GEN))

    def test_restore_refuses_an_ambiguous_section(self):
        _util.write(self.repo, GEN, "# General\n\n## Habits\n- prefer small diffs\n\n# Later\n\n"
                                    "## Habits\n- run the tests\n- keep commits small\n")
        rid = self.move("run the tests")["id"]
        before = self.read(GEN)
        with self.assertRaisesRegex(ut.GuardError, "Habits"):
            ut.restore(self.repo, rid, today=TODAY, owner=True)
        self.assertEqual(self.read(GEN), before)

    def test_stale_evidence_must_be_a_path(self):
        line = "Bump the version in `docs/gone.md` and `api.py`"
        _util.write(self.repo, ".claude/rules/misc.md", f"# M\n\n## A\n- {line}\n")
        _util.write(self.repo, "src/api/api.py", "")
        for ev in ("Bump", "version", "api.py"):  # a word; a path that resolves module-relative
            with self.assertRaises(ut.GuardError):
                self.move(line, reason="stale", evidence=ev)
        self.move(line, reason="stale", evidence="docs/gone.md")

    def test_promote_needs_second_sighting_and_mechanism(self):
        first = ut.seen(self.repo, "agent forgets to run lint", "session", rec("s1"), "lint?", TODAY)
        self.assertFalse(first["second_sighting"])
        gid = first["id"]
        with self.assertRaises(ut.GuardError):  # one sighting
            ut.promote(self.repo, gid, GEN, "Habits", "run lint before pushing", "README.md", TODAY)
        with self.assertRaises(ut.GuardError):  # owner skips the sighting, not the mechanism
            ut.promote(self.repo, gid, GEN, "Habits", "run lint before pushing", "nope.py", TODAY,
                       owner=True)
        self.assertFalse(ut.seen(self.repo, "agent forgets to run lint", "session", rec("s1"),
                                 "again", TODAY)["second_sighting"])  # same session: no new sighting
        self.assertTrue(ut.seen(self.repo, "Agent forgets to run lint", "session", rec("s2"),
                                "lint!", TODAY)["second_sighting"])
        with self.assertRaises(ut.GuardError):
            ut.promote(self.repo, gid, GEN, "Habits", "run lint before pushing", "missing.py", TODAY)
        ut.promote(self.repo, gid, GEN, "Habits", "run lint before pushing", "README.md", TODAY)
        self.assertEqual(self.read(GEN).split("\n")[3], f"- run lint before pushing [s:{gid}]")

    def test_pick_demotion_protection_window(self):
        _util.write(self.repo, ".claude/rules/three.md",
                    "# T\n\n## S\n- alpha [s:a]\n- beta [s:b]\n- gamma [s:c]\n- pinned [s:p pin]\n")
        lines = ut.parse_live(self.repo, ".claude/rules/three.md")

        def entries(**restored):
            return [ut.Entry(i, "", "", "", [f"{d} restored · owner" for d in ds], "", "archive")
                    for i, ds in restored.items()]
        pick = ut.pick_demotion(lines, entries(c=["2026-10-21"], b=["2026-07-23"]), TODAY)
        self.assertEqual(pick.id, "b")  # c restored 10 days ago; b's restore is past the window
        pick = ut.pick_demotion(lines, entries(a=["2026-10-01", "2026-10-02"], b=["2026-10-01"],
                                               c=["2026-10-01"]), TODAY)
        self.assertEqual(pick.id, "c")  # fewest, ties to the bottom-most


class Writes(Base):
    def test_scan_write_refuses_secret(self):
        for bad in ("token: abcdefgh12345", "use sk-" + "a" * 24, "\ufeffhi", "x\u00e2\u20acy",
                    "AKIA" + "A" * 16):
            with self.assertRaises(ut.GuardError):
                ut.scan_write(bad)
        ut.scan_write("a plain line — with a dash, a token mention and no value")
        before = self.read(GEN)
        with self.assertRaises(ut.GuardError):
            ut.revise(self.repo, "prefer small diffs", "prefer api_key=abcdefgh1234", None, "",
                      TODAY, owner=True)
        self.assertEqual(self.read(GEN), before)
        # a secret-shaped line already in the file does not block a move; a secret the write
        # adds does
        _util.write(self.repo, GEN, GENERAL + '- read it from env: api_key = os.environ["X"]\n')
        mid = self.move("prefer small diffs")["id"]
        before = self.read(GEN)
        with self.assertRaises(ut.GuardError):
            ut.restore(self.repo, mid, rec("s1", LATER), LATER, "mine is ghp_" + "a" * 36, TODAY)
        self.assertEqual(self.read(GEN), before)

    def test_tombstone_deleted_module(self):
        with self.assertRaises(ut.GuardError):  # still there
            ut.tombstone(self.repo, GEN, "deleted", TODAY)
        (self.repo / MOD).unlink()
        _util.commit(self.repo, "drop the module")
        out = ut.tombstone(self.repo, MOD, "deleted", TODAY)
        arch = self.read("docs/references/lessons-archive/kit-src-api-claude.md")
        self.assertIn(f"tombstone: {MOD} → deleted", arch)
        self.assertIn(f"- line: the cache is per-process\n- from: {MOD}#What an agent gets wrong "
                      f"here by default\n- {T0} moved archive · stale · tombstone", arch)
        self.assertEqual(out["ids"], ["the-cache-is-per"])
        self.assertEqual(ut.tombstones(self.repo), {MOD: None})
        self.assertEqual(ut.tombstone(self.repo, MOD, "deleted", TODAY), out)  # twice: a no-op
        self.assertEqual(self.read("docs/references/lessons-archive/kit-src-api-claude.md"), arch)

    def test_lookup_across_tiers_legacy_tombstone(self):
        self.move(MAP, tier="overflow")
        self.move("prefer small diffs")
        _util.write(self.repo, "docs/references/lessons-archive/old-notes.md",
                    "# Old notes\nlegacy: pre-kit prose\n\nRetry storms took the API down twice.\n")
        ut.tombstone(self.repo, ".claude/rules/old.md", GEN, TODAY)

        def tiers(*words, files=()):
            return {(h["tier"], h.get("id")) for h in ut.lookup(self.repo, list(words), files)}
        self.assertEqual(tiers("retry"), {("live", "no-post-retry"), ("legacy", None)})
        self.assertEqual(tiers("fooerror"), {("overflow", "map-http-status-to-a")})
        self.assertEqual(tiers("small", "diffs"), {("archive", "prefer-small-diffs")})
        self.assertEqual(tiers("old.md"), {("tombstone", None)})
        self.assertIn(("archive", "prefer-small-diffs"),
                      tiers(files=[".claude/rules/old.md"]))  # resolved through the tombstone
        self.assertEqual(ut.legacy_files(self.repo),
                         ["docs/references/lessons-archive/old-notes.md"])


class Fixes(Base):
    """The review of 2e044e5: each test failed before its fix."""

    def test_promote_then_move_and_restore_round_trip(self):
        for sid in ("s1", "s2"):
            gid = ut.seen(self.repo, "agent forgets lint", "session", rec(sid), "q", TODAY)["id"]
        ut.promote(self.repo, gid, GEN, "Habits", "run lint before pushing", "README.md", TODAY)
        self.assertNotIn(f"## s:{gid}", self.read(ut.GAPS))
        arch = "docs/references/lessons-archive/kit-claude-rules-general.md"
        self.assertIn(f"- line: run lint before pushing\n- from: {GEN}#Habits\n", self.read(arch))
        ut.revise(self.repo, gid, "run lint before you push", rec("s3"), "q", TODAY)
        self.move(gid)
        ut.restore(self.repo, gid, rec("s4", LATER), LATER, "lint again", TODAY)
        self.assertIn(f"- run lint before you push [s:{gid}]", self.read(GEN))
        self.assertFalse((self.repo / "docs/references/lessons-archive/session.md").exists())
        self.assertEqual(sum(e.id == gid for e in ut.load_entries(self.repo)), 1)

    def test_same_day_restore_uses_full_stamp(self):
        nine = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
        mid = ut.move(self.repo, "prefer small diffs", "archive", "derivable", "cite README.md",
                      nine)["id"]
        arch = "docs/references/lessons-archive/kit-claude-rules-general.md"
        self.assertIn("- 2026-10-31T09:00:00Z moved archive · derivable", self.read(arch))
        early, late = "2026-10-31T08:00:00.000Z", "2026-10-31T15:00:00.000Z"
        with self.assertRaises(ut.GuardError):
            ut.restore(self.repo, mid, rec("s1", early), early, "q", nine)
        ut.restore(self.repo, mid, rec("s2", late), late, "q", nine)
        self.assertIn(f"[s:{mid}]", self.read(GEN))

    def test_ignored_rows_and_second_ignored(self):
        first = ut.ignored(self.repo, "run the tests before pushing", rec("s1", LATER), LATER,
                           "skipped them", TODAY)
        self.assertIsNone(first["needs_owner"])
        self.assertIn(f'- {TODAY} ignored · s1 · {LATER} · "skipped them"',
                      self.read("docs/references/lessons-archive/kit-claude-rules-general.md"))
        again = ut.ignored(self.repo, first["id"], rec("s2", LATER), LATER, "again", TODAY)
        self.assertEqual(again["needs_owner"], "second-ignored")
        self.assertIn(f"- run the tests before pushing [s:{first['id']}]", self.read(GEN))


class Cli(Base):
    def run_cli(self, *args, **env):
        return subprocess.run(
            [sys.executable, str(UPKEEP), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=60,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.repo), **env})

    def test_cli_json_guard_exit_and_headless_owner(self):
        done = self.run_cli("move", "prefer small diffs", "archive", "enforced",
                            "--evidence", "cite nope.py")
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("nope.py", done.stdout + done.stderr)
        done = self.run_cli("move", "prefer small diffs", "archive", "enforced",
                            "--evidence", "cite README.md")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["id"], "prefer-small-diffs")
        done = self.run_cli("lookup", "small", "--owner", CONTEXT_KIT_HEADLESS="1")
        self.assertEqual(done.returncode, 3)
        done = self.run_cli("lookup", "small")
        self.assertEqual(json.loads(done.stdout)[0]["id"], "prefer-small-diffs")
        done = self.run_cli("pin", "run the tests before pushing", "--owner")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("- run the tests before pushing [s:run-the-tests-before pin]", self.read(GEN))


class Pins(Base):
    def errors(self) -> list[str]:
        text = self.read(API)
        return text[text.index("## Errors"):text.index("```")].rstrip("\n").split("\n")

    def test_pin_mints_and_moves_into_the_pinned_block_unpin_moves_back(self):
        ut.pin(self.repo, "never retry a POST", TODAY, owner=True)
        out = ut.pin(self.repo, MAP, TODAY, owner=True)  # untagged, with a child line
        mid = out["id"]
        self.assertEqual(self.errors(), [
            "## Errors", "- old pinned line [s:keep-me pin]",
            "- never retry a POST [s:no-post-retry pin]", f"- {MAP} [s:{mid} pin]",
            "  - nested detail"])
        entry = next(e for e in ut.load_entries(self.repo) if e.id == mid)
        self.assertTrue(any(" pinned \u00b7 owner" in r for r in entry.rows), entry.rows)
        ut.unpin(self.repo, "no-post-retry", TODAY, owner=True)
        self.assertEqual(self.errors(), [
            "## Errors", "- old pinned line [s:keep-me pin]", f"- {MAP} [s:{mid} pin]",
            "  - nested detail", "- never retry a POST [s:no-post-retry]"])

    def test_pin_guards(self):
        with self.assertRaisesRegex(ut.GuardError, "owner"):
            ut.pin(self.repo, "prefer small diffs", TODAY)
        with self.assertRaisesRegex(ut.GuardError, "already pinned"):
            ut.pin(self.repo, "keep-me", TODAY, owner=True)
        with self.assertRaisesRegex(ut.GuardError, "not pinned"):
            ut.unpin(self.repo, "no-post-retry", TODAY, owner=True)
        with mock.patch.dict(os.environ, {"CONTEXT_KIT_HEADLESS": "1"}):
            with self.assertRaisesRegex(ut.GuardError, "headless"):
                ut.pin(self.repo, "prefer small diffs", TODAY, owner=True)
        _util.write(self.repo, ".claude/context-kit.json", json.dumps({"rules_max_chars": 200}))
        before = self.read(API)  # keep-me is 33 chars; 33 + 40 > 25% of 200
        with self.assertRaisesRegex(ut.GuardError, "PIN_CAP|25%"):
            ut.pin(self.repo, "no-post-retry", TODAY, owner=True)
        self.assertEqual(self.read(API), before)


if __name__ == "__main__":
    unittest.main()
