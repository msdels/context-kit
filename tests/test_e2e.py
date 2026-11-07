"""Task 12: the loop end to end with the fake claude: gate, run, review, accept (with a veto) or
dismiss, merge or PR, and the prompt lines. Real git, a bare remote, an installed repo."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_e2e`
import _util  # noqa: E402  (isolates the environment on import)
import _kit  # noqa: E402
import context_upkeep as cu  # noqa: E402
import upkeep_audit as ua  # noqa: E402

RULES = ".claude/rules/api.md"
LEDGER = "docs/references/session-audits/2026-11-01-0900.md"
ROOT_MD = "# Project\n\n| Topic | Read first |\n|---|---|\n| api | `.claude/rules/api.md` |\n"
LINES = [f"api rule number {i} says something" for i in range(10)]
STAMP_MS = "%Y-%m-%dT%H:%M:%S.%f"


class Loop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # one install, copied per test: an install costs seconds
        cls._ttmp = TemporaryDirectory(dir=_util.TMP)
        cls.template = _util.make_repo(Path(cls._ttmp.name), install=True)
        _util.write(cls.template, "CLAUDE.md", ROOT_MD)
        _util.write(cls.template, RULES, "# API\n\n## Errors\n\n" + "".join(f"- {t}\n" for t in LINES))
        _util.commit(cls.template, "kit and context")

    @classmethod
    def tearDownClass(cls):
        cls._ttmp.cleanup()

    def setUp(self):
        self._tmp = TemporaryDirectory(dir=_util.TMP)
        self.tmp = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.log = self.tmp / "fake.log"
        self.repo = self.tmp / "sample repo"
        shutil.copytree(self.template, self.repo, symlinks=True)
        self.addCleanup(lambda: _kit.git(self.repo, "worktree", "remove", "--force",
                                         str(ua.worktree_path(self.repo))))
        self.sessions = 0
        self.add_sessions(2)
        self.covered = datetime.now(timezone.utc).strftime(STAMP_MS)[:-3] + "Z"
        env = mock.patch.dict(os.environ, {
            "FAKE_LOG": str(self.log), "FAKE_EVICT": LINES[-1], "FAKE_CLAUDE_MODE": "ok",
            "FAKE_COVERED": self.covered, "CLAUDE_PROJECT_DIR": str(self.repo)})
        env.start()
        self.addCleanup(env.stop)

    def setup_repo(self, remote=True, **cfg):
        """auto_audit on, an audit every 2 sessions, the config committed, and a bare remote."""
        self.git("config", "contextkit.autoAudit", "true")
        path = self.repo / ".claude" / "context-kit.json"
        data = {**json.loads(path.read_text(encoding="utf-8")), "audit_every_sessions": 2,
                **{"require_pr_to_main": False}, **cfg}
        path.write_text(json.dumps(data), encoding="utf-8")
        _util.commit(self.repo, "audit config")
        if remote:
            self.remote = self.tmp / "remote.git"
            _kit.git(self.tmp, "init", "-q", "--bare", str(self.remote))
            self.git("remote", "add", "origin", str(self.remote))
            self.git("push", "-q", "--no-verify", "origin", "main")
            self.git("fetch", "-q", "origin")

    def git(self, *args, cwd=None) -> str:
        done = _kit.git(cwd or self.repo, *args)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def add_sessions(self, n):
        """n owner sessions that end now: after `self.covered` once that is set."""
        now = datetime.now(timezone.utc)
        for _ in range(n):
            self.sessions += 1
            path = _util.transcript(self.repo, f"s{self.sessions}", [("user", "ask"), ("assistant", "done")])
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
            for row in rows:
                row["timestamp"] = now.strftime(STAMP_MS)[:-3] + "Z"
            path.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")

    def helper(self):
        cu.helper(self.repo)
        return ua.load_state(self.repo)

    def runs(self) -> int:
        return len(self.log.read_text(encoding="utf-8").splitlines()) if self.log.exists() else 0

    def accept(self, *answers):
        """The real CLI, as the owner types it: the installed copy, in their checkout."""
        return subprocess.run(
            [sys.executable, str(self.repo / ".claude" / "hooks" / "context_upkeep.py"), "accept",
             *answers], cwd=self.repo, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=300)

    def branch(self) -> bool:
        return _kit.git(self.repo, "rev-parse", "--verify", "-q",
                        f"refs/heads/{ua.BRANCH}").returncode == 0

    def ready(self):
        self.assertEqual(self.helper()["status"], "ready")

    def test_full_loop_accept_with_veto(self):
        self.setup_repo()
        self.assertEqual(self.helper()["status"], "ready")
        (line,) = ua.prompt_lines(self.repo, _kit.config(self.repo), datetime.now(timezone.utc))
        u = "python .claude/hooks/context_upkeep.py"
        self.assertIn("audit ready \u2014 1 move, 0 restores, 1 item.", line)
        self.assertIn(f"git diff {ua.load_state(self.repo)['base']}...context/audit", line)
        self.assertIn(f"{u} accept 1y", line)  # paste-safe: the item's default, no placeholders
        self.assertNotIn("<n>", line)
        self.assertIn(f"{u} dismiss", line)
        self.assertNotIn(LINES[-1], self.git("show", f"{ua.BRANCH}:{RULES}"))

        done = self.accept("1n")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertIn(f"- {LINES[-1]} [s:", (self.repo / RULES).read_text(encoding="utf-8"))
        overflow = self.git("grep", "-h", "restored", "main", "--", "docs/context/overflow",
                            "docs/references/lessons-archive")
        self.assertIn("restored \u00b7 owner", overflow)
        self.assertIn("covered_until", self.git("show", f"origin/main:{LEDGER}"))
        self.assertEqual(self.git("rev-parse", "main"), self.git("rev-parse", "origin/main"))
        self.assertEqual(self.git("rev-parse", "origin/main"),
                         self.git("ls-remote", "origin", "refs/heads/main").split()[0])
        self.assertIn("- [n] 1.", self.git("show", f"main:{LEDGER}"))  # the answer is on record
        state = ua.load_state(self.repo)
        self.assertEqual((state["status"], state["fails"]), ("idle", []))
        self.assertFalse(self.branch())
        self.assertIsNone(ua.audit_worktree(self.repo))
        self.assertEqual(self.helper()["status"], "idle")  # the next session: nothing due
        self.assertEqual(self.runs(), 1)

    def test_accept_refused_while_running(self):
        self.setup_repo()
        self.ready()
        ua.update_state(self.repo, status="running", pid=os.getpid(), started="2026-11-01T09:00:00Z")
        done = self.accept("1y")
        self.assertEqual(done.returncode, 1)
        self.assertIn(str(os.getpid()), done.stderr)
        self.assertIn("2026-11-01T09:00:00Z", done.stderr)
        self.assertTrue(self.branch())
        with mock.patch.dict(os.environ, {"CONTEXT_KIT_HEADLESS": "1"}):
            self.assertEqual(ua.accept(self.repo, [], _kit.config(self.repo)), 2)
            self.assertEqual(ua.dismiss(self.repo), 2)
        self.assertEqual(ua.dismiss(self.repo), 1)

    def test_dismiss_resets_and_needs_new_sessions(self):
        self.setup_repo()
        self.ready()
        ua.update_state(self.repo, fails=["old"])
        self.assertEqual(ua.dismiss(self.repo), 0)
        state = ua.load_state(self.repo)
        self.assertEqual((state["status"], state["fails"]), ("idle", []))
        self.assertFalse(self.branch())
        self.assertIsNone(ua.audit_worktree(self.repo))
        self.assertFalse(cu.due(self.repo, _kit.config(self.repo), datetime.now(timezone.utc))[0])
        self.assertEqual(self.helper()["status"], "idle")
        self.assertEqual(self.runs(), 1)  # the same sessions are not audited again
        self.add_sessions(2)
        self.assertEqual(self.helper()["status"], "ready")
        self.assertEqual(self.runs(), 2)

    def test_failed_twice_stops_and_prompts(self):
        self.setup_repo()
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "nothing"}):
            self.assertEqual(self.helper()["status"], "failed")
            self.assertEqual(self.helper()["status"], "failed")
            self.assertEqual(self.runs(), 2)
            self.helper()
            self.assertEqual(self.runs(), 2)  # no third run
            (line,) = ua.prompt_lines(self.repo, _kit.config(self.repo), datetime.now(timezone.utc))
            self.assertIn("audit failed twice", line)
            self.assertIn("python .claude/hooks/context_upkeep.py dismiss", line)
            self.assertIn("audit.log", line)
            self.assertEqual(ua.dismiss(self.repo), 0)
            self.helper()
        self.assertEqual(self.runs(), 3)  # dismiss reopened the gate

    def test_pr_mode_then_remote_merge_reopens_gate(self):
        self.setup_repo(require_pr_to_main=True)
        self.ready()
        main_before = self.git("rev-parse", "main")
        done = self.accept("1y")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("gh pr create --fill --head context/audit", done.stdout)
        self.assertEqual(self.git("rev-parse", "main"), main_before)  # nothing merged locally
        self.assertTrue(self.git("ls-remote", "origin", f"refs/heads/{ua.BRANCH}"))
        self.assertFalse(self.branch())
        self.assertEqual(ua.load_state(self.repo)["status"], "idle")
        self.add_sessions(2)
        self.helper()
        self.assertEqual(self.runs(), 1)  # the PR is open: no second audit on top of it
        before = (cu.due(self.repo, _kit.config(self.repo), datetime.now(timezone.utc))[1],
                  cu.watermark(self.repo))
        # the PR merges on the remote (fast-forward: the branch descends from main)
        self.git("push", "-q", "--no-verify", "origin", f"refs/remotes/origin/{ua.BRANCH}:refs/heads/main")
        self.helper()
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{ua.BRANCH}"), "")
        self.assertEqual(self.runs(), 2)  # the gate is open again
        after = (cu.due(self.repo, _kit.config(self.repo), datetime.now(timezone.utc))[1],
                 cu.watermark(self.repo))
        self.assertEqual(after, before)  # the merged ledger and `covered` are one mark, not two

    def test_accept_refuses_stale_main(self):
        """Local main has a commit origin/main lacks while origin/main moved on: no merge, no push,
        and the audit stays for a retry."""
        self.setup_repo()
        self.ready()
        other = self.tmp / "other"
        _kit.git(self.tmp, "clone", "-q", str(self.remote), str(other))
        _util.write(other, "theirs.txt", "x\n")
        _util.commit(other, "theirs")
        self.git("push", "-q", "origin", "main", cwd=other)
        _util.write(self.repo, "mine.txt", "x\n")
        _util.commit(self.repo, "mine")
        mine, remote = self.git("rev-parse", "main"), self.git("ls-remote", "origin", "main")
        self.assertTrue(remote)
        done = self.accept("1y")
        self.assertEqual(done.returncode, 1)
        self.assertIn("origin/main", done.stderr)
        self.assertEqual(self.git("rev-parse", "main"), mine)
        self.assertEqual(self.git("ls-remote", "origin", "main"), remote)
        self.assertNotEqual(_kit.git(self.repo, "cat-file", "-e", f"main:{LEDGER}").returncode, 0)
        self.assertEqual((ua.load_state(self.repo)["status"], self.branch()), ("ready", True))

    def edit_branch_ledger(self, edit):
        """Rewrite the audit branch's ledger and commit it there, as a hostile or broken audit
        might have left it."""
        wt = self.tmp / "edit-wt"
        self.git("worktree", "add", "-q", str(wt), ua.BRANCH)
        path = wt / LEDGER
        _util.write(wt, LEDGER, edit(path.read_text(encoding="utf-8")))
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-a", "-m", "edit",
                 cwd=wt)
        self.git("worktree", "remove", "--force", str(wt))

    def test_accept_refuses_injected_commands(self):
        """A ledger item is text on a branch: only a tier command with --owner runs, never a
        shell, and the refusal leaves the audit for dismiss and the kit worktree gone."""
        self.setup_repo()
        self.ready()
        for cmd in ("python .claude/hooks/context_upkeep.py restore x --owner && touch PWNED",
                    "python -c \"open('PWNED', 'w')\"",
                    "touch PWNED"):
            with self.subTest(cmd):
                self.edit_branch_ledger(lambda t, c=cmd: re.sub(
                    r"`[^`]+`$", lambda _: f"`{c}`", t, flags=re.MULTILINE))
                done = self.accept("1n")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertFalse((self.repo / "PWNED").exists())
                self.assertFalse((ua.worktree_path(self.repo) / "PWNED").exists())
                self.assertIsNone(ua.audit_worktree(self.repo))
                self.assertFalse(ua.worktree_path(self.repo).exists())
                self.assertEqual((ua.load_state(self.repo)["status"], self.branch()), ("ready", True))

    def test_accept_never_changes_a_protected_line(self):
        """An undo runs only `restore` (here: it would archive a different line); an apply that
        touches a protected line is caught on the answers commit. Either way nothing merges and
        the branch is as the run left it."""
        safety = "never drop the prod db"
        text = (self.repo / RULES).read_text(encoding="utf-8")
        _util.write(self.repo, RULES, text + f"\n## Safety\n\n- {safety}\n")
        self.setup_repo()
        self.ready()
        cmds = {"undo": f'`python .claude/hooks/context_upkeep.py move "{LINES[0]}" archive evicted --owner`',
                "apply": f'`python .claude/hooks/context_upkeep.py revise "{safety}" "drop it" --owner`'}
        for kind, answer in (("undo", "1n"), ("apply", "1y")):
            with self.subTest(kind):
                self.edit_branch_ledger(lambda t, k=kind: re.sub(
                    r"(undo|apply): `[^`]+`$", lambda _: f"{k}: {cmds[k]}", t, flags=re.MULTILINE))
                tip = self.git("rev-parse", ua.BRANCH)
                done = self.accept(answer)
                self.assertEqual(done.returncode, 1, done.stdout)
                live = (self.repo / RULES).read_text(encoding="utf-8")
                self.assertIn(f"- {safety}", live)
                self.assertIn(f"- {LINES[0]}", live)
                self.assertEqual(self.git("rev-parse", ua.BRANCH), tip)
                self.assertEqual(ua.load_state(self.repo)["status"], "ready")

    def test_accept_merge_conflict_aborts_and_keeps_the_audit(self):
        self.setup_repo()
        self.ready()
        text = (self.repo / RULES).read_text(encoding="utf-8")  # main changes the line the audit evicted
        _util.write(self.repo, RULES, text.replace(LINES[-1], LINES[-1] + " (reworded)"))
        _util.commit(self.repo, "reword")
        self.git("push", "-q", "--no-verify", "origin", "main")
        main = self.git("rev-parse", "main")
        done = self.accept("1y")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("dismiss", done.stderr)
        self.assertEqual(self.git("rev-parse", "main"), main)
        self.assertNotEqual(_kit.git(self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        self.assertEqual((ua.load_state(self.repo)["status"], self.branch()), ("ready", True))
        self.assertIsNone(ua.audit_worktree(self.repo))

    def test_accept_validates_an_orphan_branch(self):
        """A `failed` state whose branch no run vouched for is checked like a run's result."""
        self.setup_repo()
        self.ready()
        # a second commit: not one audit commit. A real change, or autocrlf sees none to commit
        self.edit_branch_ledger(lambda t: t + "\n")
        ua.update_state(self.repo, status="failed", reason="orphan branch")
        done = self.accept("1y")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("dismiss", done.stderr)
        self.assertIn("not one audit commit", done.stderr)
        self.assertEqual(self.git("rev-parse", "main"), self.git("rev-parse", "origin/main"))
        self.assertNotEqual(_kit.git(self.repo, "cat-file", "-e", f"main:{LEDGER}").returncode, 0)

    def test_accept_no_remote(self):
        self.setup_repo(remote=False)
        self.ready()
        done = self.accept("1y")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(LINES[-1], (self.repo / RULES).read_text(encoding="utf-8"))  # kept evicted
        self.assertIn("covered_until", self.git("show", f"main:{LEDGER}"))
        self.assertEqual(ua.load_state(self.repo)["status"], "idle")
        self.assertFalse(self.branch())

    def test_concurrent_attention_one_run(self):
        """Two sessions start together: their helpers launch one audit, and nothing they wrote is
        torn."""
        self.setup_repo()
        script = str(self.repo / ".claude" / "hooks" / "context_upkeep.py")
        procs = [subprocess.Popen([sys.executable, script, "_helper", "--repo", str(self.repo)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) for _ in range(2)]
        for p in procs:
            self.assertEqual(p.wait(timeout=300), 0)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(ua.load_state(self.repo)["status"], "ready")
        cdir = _kit.upkeep_dir(self.repo)
        for f in [cdir / "state.json", cdir / "due.json", *(cdir / "sessions").glob("*.json")]:
            json.loads(f.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
