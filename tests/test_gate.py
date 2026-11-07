"""Task 10: the headless audit's state, fetch, remote-merge check, failure streak and gate."""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_gate`
import _util  # noqa: E402  (isolates the environment on import)
import _kit  # noqa: E402
import upkeep_audit as ua  # noqa: E402

LEDGER = "docs/references/session-audits/2026-11-01-0900.md"


class Base(unittest.TestCase):
    def setUp(self):
        # best-effort: on Windows a just-killed fetch can still hold audit.log for a moment, and
        # _util removes its whole TMP at exit anyway
        self.tmp = Path(mkdtemp(dir=_util.TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = _util.make_repo(self.tmp)
        self.now = datetime.now(timezone.utc)

    def git(self, *args) -> str:
        done = _kit.git(self.repo, *args)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()


class State(Base):
    def test_update_state_expect(self):
        """The run's final write must not clobber a newer run's state (spec section 3B, the wrapper)."""
        self.assertTrue(ua.update_state(self.repo, status="running", pid=111))
        self.assertFalse(ua.update_state(self.repo, expect={"pid": 222, "status": "running"},
                                         status="failed"))
        self.assertEqual(ua.load_state(self.repo)["status"], "running")
        self.assertTrue(ua.update_state(self.repo, expect={"pid": 111, "status": "running"},
                                        status="ready"))
        self.assertEqual(ua.load_state(self.repo)["status"], "ready")

    def test_streak_stop(self):
        self.assertTrue(ua.streak_stopped({"fails": ["died", "budget", "budget"]}))
        self.assertFalse(ua.streak_stopped({"fails": ["budget", "died"]}))

    def test_month_cap_includes_next_run(self):
        """The cap holds with the next run's full budget added, not just what is spent."""
        cfg = {"audit_budget_usd": 25, "audit_month_usd": 100}
        month = self.now.strftime("%Y-%m")
        self.assertTrue(ua.month_ok({"month": month, "month_usd": 75}, cfg, self.now))
        self.assertFalse(ua.month_ok({"month": month, "month_usd": 80}, cfg, self.now))
        self.assertTrue(ua.month_ok({"month": "2000-01", "month_usd": 99}, cfg, self.now))


class Remote(Base):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        _kit.git(self.tmp, "init", "-q", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "origin", "main")
        self.git("update-ref", "-d", "refs/remotes/origin/main")

    def test_fetch_refspec_works_before_branch_exists(self):
        """No context/* branch on the remote yet: the glob refspec must not fail the fetch."""
        self.assertTrue(ua.fetch(self.repo, "main"))
        self.git("rev-parse", "--verify", "refs/remotes/origin/main")

    def test_fetch_timeout_no_hang(self):
        """A transport that never answers (a hung ssh grandchild) is killed, not waited on."""
        self.git("remote", "set-url", "origin", "ssh://example.invalid/x.git")
        sleeper = f'"{Path(sys.executable).as_posix()}" -c "import time; time.sleep(60)"'
        env = {"GIT_SSH_COMMAND": sleeper, "GIT_SSH_VARIANT": "simple"}
        with mock.patch.object(ua, "FETCH_TIMEOUT_S", 2), mock.patch.dict(os.environ, env):
            start = time.monotonic()
            self.assertFalse(ua.fetch(self.repo, "main"))
        self.assertLess(time.monotonic() - start, 10)

    def test_remote_audit_states(self):
        self.assertEqual(ua.remote_audit(self.repo, "main"), "none")
        self.git("checkout", "-q", "-b", ua.BRANCH)
        _util.write(self.repo, LEDGER, "---\ncovered_until: 2026-11-01T09:00:00Z\n---\n")
        _util.commit(self.repo, "context: audit 2026-11-01-0900 [context-kit]")
        self.git("push", "-q", "origin", ua.BRANCH)
        self.git("checkout", "-q", "main")
        self.git("branch", "-D", ua.BRANCH)
        self.assertTrue(ua.fetch(self.repo, "main"))
        self.assertEqual(ua.remote_audit(self.repo, "main"), "unmerged")
        self.git("merge", "-q", f"origin/{ua.BRANCH}")
        self.git("push", "-q", "origin", "main")
        self.assertTrue(ua.fetch(self.repo, "main"))
        hook = self.repo / ".git" / "hooks" / "pre-push"  # a deletion pushes no content: no lint
        _util.write(hook.parent, hook.name, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.assertEqual(ua.remote_audit(self.repo, "main"), "merged")
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{ua.BRANCH}"), "")


class Launch(Base):
    def test_a_reaping_helper_does_not_stop_a_due_one(self):
        """Two helpers start together: the one that is not due holds launch.lock for its reap. The
        due one must still reach the gate, or neither launches (CI saw 0 runs)."""
        lock = _kit.upkeep_dir(self.repo) / "launch.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        release = threading.Timer(0.3, lock.unlink)
        release.start()
        with mock.patch.object(ua, "_local_why", return_value=""), \
                mock.patch.object(ua, "fetch", return_value=False), \
                mock.patch.object(ua, "gate", return_value=(False, "test", "main")) as gate:
            ua.launch(self.repo, self.now, True)
        release.join()
        gate.assert_called_once()


class Gate(Base):
    def setUp(self):
        super().setUp()
        self.git("branch", "nokit")
        for rel in ua.KIT_FILES:
            _util.write(self.repo, rel, "kit\n")
        _util.commit(self.repo, "kit")
        self.git("config", "contextkit.autoAudit", "true")
        self.cfg = {**_kit.DEFAULTS, "audit_every_sessions": 0}  # due with no records

    def assert_gate(self, ok: bool, cfg: dict | None = None):
        got = ua.gate(self.repo, cfg or self.cfg, self.now)
        self.assertEqual(got[0], ok, got)

    def test_gate_conditions(self):
        """Each condition alone stops the launch; with all of them restored it launches."""
        self.assertEqual(ua.gate(self.repo, self.cfg, self.now), (True, "", "main"))
        with self.subTest("auto_audit off"):
            self.git("config", "--unset", "contextkit.autoAudit")
            self.assert_gate(False)
            self.git("config", "contextkit.autoAudit", "true")
        with self.subTest("kit not on base"):
            self.assert_gate(False, {**self.cfg, "work_branch": "nokit"})
        with self.subTest("not due"):
            self.assert_gate(False, {**self.cfg, "audit_every_sessions": 10})
        with self.subTest("not idle"):
            ua.update_state(self.repo, status="ready")
            self.assert_gate(False)
            ua.update_state(self.repo, status="idle")
        with self.subTest("local branch"):
            self.git("branch", ua.BRANCH)
            self.assert_gate(False)
            self.git("branch", "-D", ua.BRANCH)
        with self.subTest("unmerged remote audit"):
            _util.write(self.repo, LEDGER, "---\ncovered_until: 2026-11-01T09:00:00Z\n---\n")
            sha = _util.commit(self.repo, "ledger")
            self.git("reset", "-q", "--hard", "HEAD~1")
            self.git("update-ref", f"refs/remotes/origin/{ua.BRANCH}", sha)
            self.assert_gate(False)
            self.git("update-ref", "-d", f"refs/remotes/origin/{ua.BRANCH}")
        with self.subTest("failure streak"):
            ua.update_state(self.repo, fails=["budget", "budget"])
            self.assert_gate(False)
            ua.update_state(self.repo, fails=[])
        with self.subTest("month cap"):
            ua.update_state(self.repo, month=self.now.strftime("%Y-%m"), month_usd=90.0)
            self.assert_gate(False)
            ua.update_state(self.repo, month_usd=0.0)
        self.assert_gate(True)


if __name__ == "__main__":
    unittest.main()
