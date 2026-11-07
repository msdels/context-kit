"""Task 11: the headless runner, its validation, the conditional final write, and reaping."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_runner`
import _util  # noqa: E402  (isolates the environment on import)
import _kit  # noqa: E402
import upkeep_audit as ua  # noqa: E402

RULES = ".claude/rules/api.md"
LEDGER = "docs/references/session-audits/2026-11-01-0900.md"
ROOT_MD = "# Project\n\n| Topic | Read first |\n|---|---|\n| api | `.claude/rules/api.md` |\n"
LINES = [f"api rule number {i} says something" for i in range(10)]


class Runner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # one install, copied per test: an install costs seconds
        cls._ttmp = TemporaryDirectory(dir=_util.TMP)
        cls.template = _util.make_repo(Path(cls._ttmp.name), install=True)
        _util.write(cls.template, "CLAUDE.md", ROOT_MD)
        _util.write(cls.template, RULES, "# API\n\n## Errors\n\n"
                    + "".join(f"- {t}\n" for t in LINES))
        _util.commit(cls.template, "kit and context")

    @classmethod
    def tearDownClass(cls):
        cls._ttmp.cleanup()

    def setUp(self):
        self._tmp = TemporaryDirectory(dir=_util.TMP)
        self.tmp = Path(self._tmp.name).resolve()
        self.repo = self.tmp / "sample repo"
        shutil.copytree(self.template, self.repo, symlinks=True)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: _kit.git(self.repo, "worktree", "remove", "--force",
                                         str(ua.worktree_path(self.repo))))
        self.cfg = {**_kit.config(self.repo), "audit_budget_usd": 25}
        self.log = self.tmp / "fake.log"
        env = mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log), "FAKE_EVICT": LINES[-1]})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args) -> str:
        done = _kit.git(self.repo, *args)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def run_mode(self, mode, **env) -> dict:
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": mode, **env}):
            self.assertIsNotNone(ua.start_run(self.repo, "main"))
            ua.run_audit(self.repo, self.cfg)
        return ua.load_state(self.repo)

    def fake_runs(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]

    def branch(self) -> bool:
        return _kit.git(self.repo, "rev-parse", "--verify", "-q",
                        f"refs/heads/{ua.BRANCH}").returncode == 0

    def assert_failed_and_clean(self, state: dict, why: str) -> None:
        self.assertEqual(state["status"], "failed", state)
        self.assertIn(why, state["reason"])
        self.assertEqual(state["fails"], [state["reason"]])
        self.assertFalse(self.branch())
        self.assertIsNone(ua.audit_worktree(self.repo))
        self.assertFalse(ua.worktree_path(self.repo).exists())

    def test_ok_ready_utf8_and_space_path(self):
        """A good run is `ready`, books what it cost, and the session that launched it does not
        leak into claude (CLAUDECODE makes it refuse to start) while auth does reach it."""
        ua.update_state(self.repo, fails=["died"])
        state = self.run_mode("ok", CLAUDECODE="1", CLAUDE_CODE_OAUTH_TOKEN="tok",
                              CLAUDE_PROJECT_DIR=str(self.repo))
        self.assertEqual(state["status"], "ready", state)
        self.assertEqual(state["fails"], [])
        self.assertEqual(state["month_usd"], 1.5)
        self.assertEqual(state["ledger"], LEDGER)
        (run,) = self.fake_runs()
        wt = self.repo.parent / ".context-kit-wt-sample repo"
        self.assertEqual(Path(run["cwd"]).resolve(), wt.resolve())
        self.assertEqual(Path(run["project_dir"]).resolve(), wt.resolve())
        self.assertEqual((run["headless"], run["claudecode"], run["token"]), ("1", None, "tok"))
        self.assertEqual(run["argv"][-2:], ["--", "/auditing-sessions"])
        self.assertIn("\u2014", self.git("show", f"{ua.BRANCH}:{LEDGER}"))
        self.assertNotIn(LINES[-1], self.git("show", f"{ua.BRANCH}:{RULES}"))
        self.assertIsNone(ua.audit_worktree(self.repo))  # the branch stays; the worktree goes

    def test_nothing_fails_cleans(self):
        state = self.run_mode("nothing")
        self.assert_failed_and_clean(state, "commit")
        self.assertEqual(state["month_usd"], 0.25)

    def test_contract_edit_fails(self):
        self.assert_failed_and_clean(self.run_mode("contract"), "operating-contract.md")

    def test_nocost_charges_budget(self):
        """A run that reports no cost may have spent it all: the month is charged the budget."""
        self.assertEqual(self.run_mode("nocost")["month_usd"], 25)

    def test_quickexit_and_missing_claude_cost_zero(self):
        """A claude that never ran, or quit at once with nothing, spent nothing: no charge
        (a full-budget charge here would trip the monthly cap for nothing)."""
        state = self.run_mode("quickexit")
        self.assert_failed_and_clean(state, "commit")
        self.assertEqual(state["month_usd"], 0)
        ua.update_state(self.repo, status="idle", fails=[])
        with mock.patch.object(_kit, "tool", return_value=[str(self.tmp / "no such claude")]):
            state = self.run_mode("ok")
        self.assert_failed_and_clean(state, "did not start")
        self.assertEqual(state["month_usd"], 0)

    def test_hang_tree_killed(self):
        """A hung run is killed with what it started, and the wrapper still writes its end."""
        start = time.monotonic()
        with mock.patch.object(ua, "WRAP_TIMEOUT_S", 3):
            state = self.run_mode("hang")
        self.assertLess(time.monotonic() - start, 60)
        self.assert_failed_and_clean(state, "timed out")
        gc = self.fake_runs()[0]["grandchild"]
        deadline = time.monotonic() + 10
        while ua.pid_alive(gc) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(ua.pid_alive(gc))

    def test_validate_uses_worktree(self):
        """context_check judges the audit's worktree, not the owner's checkout the helper's
        CLAUDE_PROJECT_DIR names (here, an over-budget rules file the owner has not committed)."""
        _util.write(self.repo, ".claude/rules/huge.md", "- " + "x" * 7000 + "\n")
        state = self.run_mode("ok", CLAUDE_PROJECT_DIR=str(self.repo))
        self.assertEqual(state["status"], "ready", state)

    def test_final_write_conditional(self):
        """Something else took the state mid-run (reap, a newer run): the run's final write is
        skipped, but what it spent is still booked."""
        def took_over(*args):
            ua.update_state(self.repo, pid=12345)
            return "", LEDGER
        with mock.patch.object(ua, "validate", side_effect=took_over):
            state = self.run_mode("ok")
        self.assertEqual((state["status"], state["pid"]), ("running", 12345))
        self.assertEqual(state["month_usd"], 1.5)

    def test_reap_dead_child_and_orphan_branch(self):
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNotNone(ua.start_run(self.repo, "main"))  # this process: alive, recent
        ua.reap(self.repo, now)
        self.assertEqual(ua.load_state(self.repo)["status"], "running")

        child = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
        child.wait()
        ua.update_state(self.repo, pid=child.pid, started=stamp)
        ua.reap(self.repo, now)
        state = ua.load_state(self.repo)
        self.assertEqual(state["status"], "failed", state)
        self.assertEqual(state["month_usd"], 25)
        self.assertFalse(self.branch())  # it held no audit commit: cleaned up
        self.assertIsNone(ua.audit_worktree(self.repo))

        ua.update_state(self.repo, status="idle", fails=[])
        self.git("branch", ua.BRANCH, "main")
        ua.reap(self.repo, now)
        self.assertFalse(self.branch())
        self.assertEqual(ua.load_state(self.repo)["status"], "idle")

        self.git("checkout", "-q", "-b", ua.BRANCH)
        _util.write(self.repo, LEDGER, "---\ncovered_until: 2026-11-01T09:00:00Z\n---\n")
        _util.commit(self.repo, "context: audit 2026-11-01-0900 [context-kit]")
        self.git("checkout", "-q", "main")
        ua.reap(self.repo, now)
        state = ua.load_state(self.repo)
        self.assertEqual((state["status"], state["reason"]), ("failed", "orphan branch"))
        self.assertTrue(self.branch())  # the owner's to accept or dismiss

    def test_owner_checkout_of_the_audit_branch_is_left_alone(self):
        """context/audit checked out in the owner's own checkout (or a worktree of theirs): reap
        and cleanup must not delete it, its .git or its uncommitted work."""
        self.git("checkout", "-q", "-b", ua.BRANCH)
        _util.write(self.repo, "dirty.txt", "uncommitted work\n")
        for keep in (False, True):
            ua.reap(self.repo, datetime.now(timezone.utc))
            ua.cleanup(self.repo, keep_branch=keep)
        self.assertEqual((self.repo / "dirty.txt").read_text(encoding="utf-8"), "uncommitted work\n")
        self.assertTrue(self.branch())
        self.assertIsNone(ua.audit_worktree(self.repo))
        self.git("checkout", "-q", "main")
        other = self.tmp / "theirs"
        self.git("worktree", "add", "-q", str(other), ua.BRANCH)  # their worktree, on the branch
        _util.write(other, "dirty.txt", "theirs\n")
        ua.cleanup(self.repo, keep_branch=False)
        self.assertEqual((other / "dirty.txt").read_text(encoding="utf-8"), "theirs\n")

    def test_validate_refuses(self):
        """What the audit may not do, each in its own run: edit a protected line, pass the
        net-removal cap, touch a file outside the tiers, delete a tier file."""
        _util.write(self.repo, ".claude/rules/safe.md", "# Safe\n\n## Hard Rules\n\n- never push\n")
        _util.write(self.repo, "docs/context/overflow/kit-claude-rules-api.md", "- parked line\n")
        _util.commit(self.repo, "more context")
        cases = {
            "a protected line changed": {".claude/rules/safe.md": "# Safe\n\n## Hard Rules\n\n- push\n"},
            "net-removal cap": {RULES: "# API\n\n## Errors\n"},
            "only tier files and the ledger may": {"src/app.py": "print(1)\n"},
            "tier files are moved into, never removed": {"docs/context/overflow/kit-claude-rules-api.md": None},
        }
        for why, edits in cases.items():
            with self.subTest(why):
                wt = ua.start_run(self.repo, "main")
                base = self.git("rev-parse", "main")
                for rel, text in edits.items():
                    if text is None:
                        (wt / rel).unlink()
                    else:
                        _util.write(wt, rel, text)
                _util.write(wt, LEDGER, "---\ncovered_until: 2026-11-01T09:00:00Z\n---\n")
                _kit.git(wt, "add", "-A", "--", LEDGER, *edits)
                _kit.git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m",
                         "context: audit 2026-11-01-0900 [context-kit]")
                got = ua.validate(self.repo, wt, base)[0]
                ua.cleanup(self.repo, keep_branch=False)
                self.assertIn(why, got)

    def test_stale_registered_worktree_recovered(self):
        """A crashed run's worktree, still registered and holding junk, does not stop the next."""
        stale = ua.worktree_path(self.repo)
        self.git("worktree", "add", "-q", "--detach", str(stale), "main")
        _util.write(stale, "junk.txt", "left over\n")
        wt = ua.prepare_worktree(self.repo, "main")
        self.assertIsNotNone(wt)
        self.assertEqual(ua.audit_worktree(self.repo), wt)
        self.assertFalse((wt / "junk.txt").exists())

    def test_add_failure_does_not_cleanup(self):
        """A failed add leaves what is there: that branch may hold an audit the owner wants."""
        _util.write(self.repo, "x.txt", "x\n")
        sha = _util.commit(self.repo, "on the branch")
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.git("branch", ua.BRANCH, sha)
        self.assertIsNone(ua.prepare_worktree(self.repo, "main"))
        self.assertEqual(self.git("rev-parse", ua.BRANCH), sha)


if __name__ == "__main__":
    unittest.main()
