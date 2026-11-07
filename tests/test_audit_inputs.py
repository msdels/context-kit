"""Task 5: the prompted audit's inputs - `batch`, `inputs`, `figures`, and audit_sessions on the store."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_audit_inputs`
import _util  # noqa: E402  (isolates the environment on import)
import _kit  # noqa: E402
import audit_sessions  # noqa: E402
import context_upkeep as cu  # noqa: E402

DASH = "\u2014"
PAST = "2026-01-01T00:00:00Z"


class Base(unittest.TestCase):
    install = False

    def setUp(self):
        self._tmp = TemporaryDirectory(dir=_util.TMP)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {"CONTEXT_KIT_PROJECTS": str(self.tmp / "projects")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = _util.make_repo(self.tmp, install=self.install)

    def commit_at(self, when: str, msg: str):
        """Commit everything with both dates set to `when` (so it lands before a watermark)."""
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        _kit.git(self.repo, "add", "-A", "--", ".")
        self.assertEqual(_kit.git(self.repo, "commit", "-qm", msg, env=env).returncode, 0)

    def ledger(self, covered_until: str):
        _util.write(self.repo, "docs/references/session-audits/2026-02-01-0900.md",
                    f"---\ncovered_until: {covered_until}\nmodels: claude-opus-4-5\n---\n# audit\n")
        _util.commit(self.repo, "ledger")

    def cli(self, *args) -> dict:
        done = subprocess.run(
            [sys.executable, str(self.repo / ".claude" / "hooks" / "context_upkeep.py"), *args],
            cwd=self.repo, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)


class Inputs(Base):
    install = True

    def test_inputs_dangling_orphans_restated(self):
        _util.write(self.repo, ".claude/rules/api.md",
                    "---\npaths:\n  - api/**\n  - nowhere/**/*.py\n---\n# API\n\n## Errors\n"
                    "- see `docs/gone.md` before retrying; `README.md` is fine\n")
        _util.write(self.repo, "api/x.py", "x = 1\n")
        _util.write(self.repo, "src/old/CLAUDE.md", "# Old\n\n## Gotchas\n- takes 5 ms to warm up\n")
        _util.write(self.repo, "src/old/a.py", "a = 1\n")
        _util.write(self.repo, "docs/references/lessons-archive/kit-claude-rules-legacy-md.md",
                    f"# archive\n\n## s:gone-line {DASH} gone\n- line: gone line\n"
                    "- from: .claude/rules/legacy.md#X\n"
                    "- 2026-01-01T00:00:00Z moved archive \u00b7 evicted \u00b7 budget\n\n"
                    f"## s:flaky-upload {DASH} flaky upload\n- line: retry the flaky upload twice\n"
                    "- from: .claude/rules/legacy.md#X\n"
                    "- 2026-01-01T00:00:00Z moved archive \u00b7 evicted \u00b7 budget\n\n")
        self.commit_at(PAST, "setup")
        self.ledger("2026-01-15T00:00:00Z")
        self.assertEqual(_kit.git(self.repo, "mv", "src/old", "src/new").returncode, 0)
        with open(self.repo / ".claude/rules/api.md", "a", encoding="utf-8") as f:
            f.write("- p95 is 320 ms on the search path\n- retry the flaky upload twice\n"
                    "- run 3 sessions before merging\n")
        _util.commit(self.repo, "rename module, add a figure")
        _util.write(self.repo, "src/new/b.py", "b = 2\n")
        _util.commit(self.repo, "code only")

        got = self.cli("batch")["inputs"]
        dangling = {(d["file"], d["ref"]) for d in got["dangling"]}
        self.assertIn((".claude/rules/api.md", "docs/gone.md"), dangling)
        self.assertIn((".claude/rules/api.md", "nowhere/**/*.py"), dangling)
        self.assertNotIn((".claude/rules/api.md", "README.md"), dangling)
        self.assertNotIn((".claude/rules/api.md", "api/**"), dangling)
        orphans = {o["path"]: o["renamed_to"] for o in got["orphans"]}
        self.assertEqual(orphans.get("src/old/CLAUDE.md"), "src/new/CLAUDE.md")
        self.assertIn(".claude/rules/legacy.md", orphans)
        self.assertIsNone(orphans[".claude/rules/legacy.md"])
        restated = [r["text"] for r in got["restated"]]
        self.assertTrue(any("p95 is 320 ms" in t for t in restated), restated)
        self.assertFalse(any("5 ms to warm up" in t for t in restated), restated)  # before the mark
        self.assertFalse(any("3 sessions" in t for t in restated), restated)  # "s" is no unit here
        refiled = [(r["why"], r["hits"]) for r in got["restated"] if "flaky upload" in r["text"]]
        self.assertEqual(refiled, [(["lookup"], [{"tier": "archive", "id": "flaky-upload",
                                                   "text": "retry the flaky upload twice"}])])
        self.assertEqual(got["live_lines"], 5)
        self.assertEqual([d["module"] for d in got["drift"]], ["src/new"])

    def test_restated_skips_the_last_audits_own_work(self):
        import upkeep_tiers as ut
        rules = ".claude/rules/api.md"
        queue = "keep the queue depth bounded tightly"
        _util.write(self.repo, rules, "# API\n\n## Errors\n- the warm cache path takes 5 ms\n"
                    f"- keep the retry budget small please\n- {queue}\n")
        _util.commit(self.repo, "rules")
        ids = [ut.move(self.repo, line, "archive", "derivable", "cite README.md", owner=True)["id"]
               for line in ("the warm cache path takes 5 ms", "keep the retry budget small please")]
        qid = ut.ignored(self.repo, queue, {"id": "s1"}, "", "q")["id"]  # live, with a history
        _util.commit(self.repo, "moves")
        # the last audit restored the figure line and committed it with its ledger: only the
        # sweep's base (that commit) keeps its "5 ms" out
        ut.restore(self.repo, ids[0], owner=True)
        self.ledger(PAST)
        ut.restore(self.repo, ids[1], owner=True)  # restored by hand since: its own history
        with open(self.repo / rules, "a", encoding="utf-8") as f:
            f.write("- keep the queue depth bounded, tightly\n")  # a duplicate of a live line
        _util.commit(self.repo, "a later restore, a duplicate")
        got = self.cli("batch")["inputs"]
        self.assertEqual([(r["text"], r["hits"]) for r in got["restated"]],
                         [("- keep the queue depth bounded, tightly",
                           [{"tier": "live", "id": qid, "text": queue}])])


    def test_drift_counts_code_after_covered_until_not_after_the_ledger_commit(self):
        """F-A4: the seed ledger is committed after the work it covers from; drift by commit
        date still sees code that landed between `covered_until` and that commit."""
        for mod in ("src/early", "src/late"):
            _util.write(self.repo, f"{mod}/CLAUDE.md", "# Mod\n\n## Gotchas\n- a gotcha\n")
            _util.write(self.repo, f"{mod}/a.py", "a = 1\n")
        self.commit_at("2026-01-01T00:00:00Z", "modules")
        _util.write(self.repo, "src/early/b.py", "b = 1\n")
        self.commit_at("2026-01-02T00:00:00Z", "covered code")
        _util.write(self.repo, "src/late/b.py", "b = 1\n")
        self.commit_at("2026-01-10T00:00:00Z", "uncovered code")
        self.ledger("2026-01-05T00:00:00Z")  # committed now, after both
        got = self.cli("batch")["inputs"]
        self.assertEqual(got["drift"], [{"module": "src/late", "commits": 1}])

    def test_drift_ignores_the_kits_own_commits_to_claude_md(self):
        """The adopt/setup commit adds a pointer to a module's CLAUDE.md; that is not an update
        that describes the code, and counting it as the last change hid the drift."""
        _util.write(self.repo, "src/mod/CLAUDE.md", "# Mod\n\n## Gotchas\n- a gotcha\n")
        _util.write(self.repo, "src/mod/a.py", "a = 1\n")
        self.commit_at("2026-01-01T00:00:00Z", "module")
        _util.write(self.repo, "src/mod/b.py", "b = 1\n")
        self.commit_at("2026-01-10T00:00:00Z", "code")
        _util.write(self.repo, "src/mod/CLAUDE.md", "# Mod\n\n## Gotchas\n- a gotcha\n- see overflow\n")
        self.commit_at("2026-01-11T00:00:00Z", "setup context-kit [context-kit]")
        self.ledger("2026-01-05T00:00:00Z")
        got = self.cli("batch")["inputs"]
        self.assertEqual(got["drift"], [{"module": "src/mod", "commits": 1}])

    def test_dangling_resolves_module_relative_refs_branches_and_ignored_dirs(self):
        """F18/F-A9/F-S7: the detector must not flood the audit with paths that resolve."""
        _util.write(self.repo, ".gitignore", "data/\n")
        _util.write(self.repo, "src/app/store/db.py", "x = 1\n")
        _util.write(self.repo, "src/app/CLAUDE.md",
                    "# App\n\n## Gotchas\n- `store/db.py` and `store/` hold the pool\n")
        _util.write(self.repo, ".claude/rules/store.md",
                    "---\npaths:\n  - src/app/store/**\n---\n# Store\n\n## Rules\n"
                    "- `db.py` and `app/store/db.py` own it; branch from `dev/working`; "
                    "fixtures go in `data/`; never `src/app/gone.py`\n")
        _util.commit(self.repo, "app")
        _kit.git(self.repo, "branch", "dev/working")
        got = self.cli("batch")["inputs"]["dangling"]
        self.assertEqual([d["ref"] for d in got], ["src/app/gone.py"])


class UnmergedAudit(Base):
    install = True

    def test_audit_on_another_branch_reports_no_false_orphans(self):
        """The sweep diffs from the merge-base: a rules file the audit branch added is not an
        orphan seen from main (the skill would tombstone it)."""
        _util.commit(self.repo, "install")  # on main, so the hooks stay when we switch back
        _kit.git(self.repo, "switch", "-q", "-c", "work")
        _util.write(self.repo, ".claude/rules/new.md", "# New\n\n- a rule\n")
        _util.commit(self.repo, "add a rules file")
        self.ledger(PAST)
        _kit.git(self.repo, "switch", "-q", "main")
        self.assertEqual(self.cli("batch")["inputs"]["orphans"], [])


class Batch(Base):
    install = True

    def test_batch_files_and_covered_until(self):
        _util.transcript(self.repo, "s1", [("user", "first ask"), ("assistant", "done")])
        _util.transcript(self.repo, "s2", [("user", "second ask"), ("assistant", "done")])
        self.assertEqual(cu.backfill(self.repo), 2)
        stale = _util.write(self.repo, ".context-kit-batch/old.txt", "from the last audit\n")

        out = self.cli("batch")
        records = cu.records_after(self.repo, "")
        self.assertEqual(out["covered_until"], records[-1]["ts_end"])
        files = [Path(f) for f in out["files"]]
        self.assertEqual([f.name for f in files], [f"{r['id']}.txt" for r in records])
        self.assertIn("first ask", files[0].read_text(encoding="utf-8"))
        self.assertTrue(all(f.parent == self.repo / ".context-kit-batch" for f in files))
        self.assertFalse(stale.exists())
        self.assertEqual(set(out["inputs"]), {"dangling", "orphans", "drift", "restated",
                                             "live_lines"})
        with mock.patch.object(cu, "BATCH_MAX", 1):
            self.assertEqual([r["id"] for r in cu.batch_records(self.repo)], [records[0]["id"]])

    def test_records_ending_together_keep_one_order(self):
        """Two sessions can end on the same millisecond; the batch order must not then depend on
        the order the filesystem lists them in."""
        for name, sid in (("a", "s2"), ("b", "s1")):  # listed a, b: id order is the reverse
            _util.write(_kit.upkeep_dir(self.repo), f"sessions/{name}.json",
                        json.dumps({"id": sid, "ts_end": "2026-11-01T09:00:00.000Z"}))
        self.assertEqual([r["id"] for r in cu.records_after(self.repo, "")], ["s1", "s2"])


class OneAuditAtATime(Base):
    install = True

    def test_an_owner_batch_waits_for_a_headless_audit_of_the_same_sessions(self):
        """A session start can launch the headless audit while the owner runs one by hand in
        that same session: two audits of one batch pay twice and can disagree."""
        state = _kit.upkeep_dir(self.repo) / "state.json"

        def batch(**env):
            return subprocess.run(
                [sys.executable, str(self.repo / ".claude" / "hooks" / "context_upkeep.py"),
                 "batch"], cwd=self.repo, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=120,
                env={**os.environ, **env})

        for status, reason, needle in (("running", "", "is running"), ("ready", "", "waits on"),
                                       ("failed", "orphan branch", "waits on")):
            _util.write(state.parent, state.name, json.dumps(
                {"status": status, "reason": reason, "pid": 1, "started": PAST,
                 "base": "a" * 40}))
            done = batch()
            self.assertEqual(done.returncode, 3, done.stderr)
            self.assertIn(needle, done.stderr)
            self.assertEqual(batch(CONTEXT_KIT_HEADLESS="1").returncode, 0)  # the run itself
        for done in ({"status": "idle"}, {"status": "failed", "reason": "budget"}):
            _util.write(state.parent, state.name, json.dumps(done))
            self.assertEqual(batch().returncode, 0)


class Figures(Base):
    def test_figures_from_records(self):
        records = [
            {"id": "a", "ts_start": "2026-10-01T10:00:00Z", "ts_end": "2026-10-01T11:00:00Z",
             "turns": [{"ts": "", "owner": "build the export"}, {"ts": "", "owner": "no, I said CSV"}]},
            {"id": "b", "ts_start": "2026-10-02T10:00:00Z", "ts_end": "2026-10-02T11:00:00Z",
             "turns": [{"ts": "", "owner": "ship it"}, {"ts": "", "owner": "still broken"}]},
        ]
        self.assertEqual(cu.figures(self.repo, records),
                         {"asks": 4, "corrections": 2, "rate": 0.5})
        self.assertEqual(cu.figures(self.repo, [])["rate"], 0.0)


class Ledger(Base):
    def test_ledger_models_by_use_and_last(self):
        for sid, model in (("m1", "claude-opus-4-5"), ("m2", "claude-sonnet-4-5-20250929"),
                           ("m3", "claude-sonnet-4-5")):
            _util.transcript(self.repo, sid, [("user", "go"), ("assistant", "done")], model=model)
        self.assertEqual(cu.backfill(self.repo), 3)
        self.assertEqual(cu.ledger(self.repo)["last"], "")
        self.ledger(PAST)
        out = cu.ledger(self.repo)
        self.assertEqual(out["models"], ["claude-sonnet-4-5", "claude-opus-4-5"])
        self.assertEqual(out["last"], "docs/references/session-audits/2026-02-01-0900.md")
        self.assertEqual(out["figures"]["asks"], 3)


class AuditSessions(Base):
    def test_audit_sessions_reads_store(self):
        path = _util.transcript(self.repo, "storeonly1", [("user", "kept in the store"),
                                                          ("assistant", "ok")])
        self.assertEqual(cu.backfill(self.repo), 1)
        path.unlink()  # Claude Code deleted it: only the store has it now
        out = self.tmp / "condensed"
        with mock.patch.object(audit_sessions, "REPO_ROOT", self.repo), \
                contextlib.redirect_stdout(io.StringIO()):
            code = audit_sessions.main(["--since", "2020-01-01", "--out", str(out),
                                    "--projects", str(self.tmp / "no such dir")])
        self.assertEqual(code, 0)
        self.assertIn("kept in the store", (out / "turns-storeonl.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
