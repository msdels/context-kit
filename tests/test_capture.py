"""Task 2: session capture — read_record, backfill, the watermark and due, and `attention`."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_capture` and discover
import _util  # noqa: E402  (isolates the environment on import)
import _kit  # noqa: E402
import context_upkeep as cu  # noqa: E402

UPKEEP = Path(cu.__file__).resolve()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(dir=_util.TMP)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.projects = self.tmp / "projects"
        patcher = mock.patch.dict(os.environ, {"CONTEXT_KIT_PROJECTS": str(self.projects)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = _util.make_repo(self.tmp)

    def sessions(self, repo=None) -> list[Path]:
        return sorted((_kit.upkeep_dir(repo or self.repo) / "sessions").glob("*.json"))

    def ledger(self, covered_until: str, name="2026-10-01-0900"):
        _util.write(self.repo, f"docs/references/session-audits/{name}.md",
                    f"---\ncovered_until: {covered_until}\nmodels: claude-opus-4-5\n---\n# audit\n")
        return _util.commit(self.repo, "ledger")


class ReadRecord(Base):
    def test_read_record(self):
        path = _util.transcript(
            self.repo, "s1", [("user", "fix the bug"), ("assistant", "on it"),
                              ("user", "thanks, now ship"), ("assistant", "shipped")],
            model="claude-sonnet-4-5-20250929", files=[self.repo / "a.py"])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type": "user", "message": {"role": "user", "content": "trunc')  # cut off
        rec = cu.read_record(path)
        self.assertEqual((rec["id"], rec["entrypoint"], rec["cwd"]), ("s1", "cli", str(self.repo)))
        self.assertEqual(rec["model_family"], "claude-sonnet-4-5")
        self.assertEqual(rec["owner_turns"], 2)
        self.assertEqual([t["owner"] for t in rec["turns"]], ["fix the bug", "thanks, now ship"])
        self.assertTrue(all(t["ts"].endswith("Z") for t in rec["turns"]))
        self.assertLess(rec["ts_start"], rec["ts_end"])
        self.assertEqual(rec["files"], [str(self.repo / "a.py")])
        self.assertIn("fix the bug", rec["text"])
        self.assertEqual(rec["start_sha"], "")

        sdk = _util.transcript(self.repo, "s2", [("user", "hi")], entrypoint="sdk-cli")
        self.assertIsNone(cu.read_record(sdk))
        self.assertEqual(cu.model_family("<synthetic>"), "")


class Backfill(Base):
    def test_backfill_worktree_cwd_filter_and_rerun(self):
        sibling = _util.make_repo(self.tmp, "sample repo-2")  # its slug has ours as a prefix
        wt = self.tmp / "side tree"
        self.assertEqual(_kit.git(self.repo, "worktree", "add", "-q", str(wt), "-b", "side").returncode, 0)
        _util.transcript(self.repo, "main1", [("user", "one")])
        _util.transcript(wt.resolve(), "wt1", [("user", "two")])
        _util.transcript(sibling, "sib1", [("user", "three")])
        _util.transcript(self.repo, "misfiled", [("user", "four")], cwd=sibling)
        blank = _util.transcript(self.repo, "blank", [("user", "five")])
        lines = [json.loads(x) for x in blank.read_text(encoding="utf-8").splitlines()]
        blank.write_text("".join(json.dumps({**r, "cwd": ""}) + "\n" for r in lines),
                         encoding="utf-8")
        before = os.getcwd()
        os.chdir(self.repo)  # Path("").resolve() is the process cwd: here, the repo
        self.addCleanup(os.chdir, before)
        sha = _kit.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        cu.record_start(self.repo, "main1", sha)  # attention's stub
        cu.record_start(self.repo, "main1", "f" * 40)  # a resume keeps the first start

        self.assertEqual(cu.backfill(self.repo), 2)
        self.assertEqual([p.stem for p in self.sessions()], ["main1", "wt1"])
        self.assertEqual(cu.backfill(self.repo), 0)  # nothing changed
        _util.transcript(self.repo, "main1", [("user", "one"), ("user", "one more")])
        self.assertEqual(cu.backfill(self.repo), 1)  # a grown transcript is recaptured
        rec = json.loads(self.sessions()[0].read_text(encoding="utf-8"))
        self.assertEqual((rec["start_sha"], rec["owner_turns"]), (sha, 2))

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_backfill_through_symlinked_parent(self):
        real = self.tmp / "real parent"
        real.mkdir()
        repo = _util.make_repo(real, "sample repo")
        link = self.tmp / "link parent"
        os.symlink(real, link)
        _util.transcript(repo, "s1", [("user", "hi")], cwd=link / "sample repo")
        self.assertEqual(cu.backfill(link / "sample repo"), 1)
        self.assertEqual([r["id"] for r in cu.records_after(repo, "")], ["s1"])

    @unittest.skipUnless(os.name == "nt", "drive letters are a Windows thing")
    def test_backfill_lowercase_drive(self):
        path = _util.transcript(self.repo, "s1", [("user", "hi")],
                                cwd=str(self.repo)[0].lower() + str(self.repo)[1:])
        lower = path.parent.with_name(path.parent.name[0].lower() + path.parent.name[1:])
        path.parent.rename(lower)
        self.assertEqual(cu.backfill(self.repo), 1)

    def raw(self, sid, lines):
        path = self.projects / _util.project_slug(self.repo) / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_bad_or_raising_transcript_does_not_halt_capture(self):
        stamp = {"timestamp": "2026-10-31T10:00:00.000Z", "cwd": str(self.repo), "entrypoint": "cli"}
        self.raw("a-bad", [{"type": "user", "message": "a bare string", **stamp}, "5", "[1]"])
        _util.transcript(self.repo, "a-boom", [("user", "x")])
        _util.transcript(self.repo, "z-good", [("user", "hi")])
        real = cu.read_record

        def read(path):
            if Path(path).stem == "a-boom":
                raise ValueError("embedded null byte")
            return real(path)

        with mock.patch.object(cu, "read_record", read):
            cu.helper(self.repo)
        self.assertEqual([p.stem for p in self.sessions()], ["z-good"])
        cdir = _kit.upkeep_dir(self.repo)
        self.assertIn("embedded null byte", (cdir / "audit.log").read_text(encoding="utf-8"))
        due = json.loads((cdir / "due.json").read_text(encoding="utf-8"))
        self.assertEqual(due["n"], 1)

    def test_concurrent_backfill(self):
        for i in range(8):
            _util.transcript(self.repo, f"s{i}", [("user", f"turn {i}")] * 3)
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.repo)}
        procs = [subprocess.Popen([sys.executable, str(UPKEEP), "_helper", "--repo", str(self.repo)],
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual((p.returncode, err), (0, ""))
        files = self.sessions()
        self.assertEqual(len(files), 8)
        for f in files:
            self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["owner_turns"], 3)
        self.assertEqual(list(files[0].parent.glob("*.tmp")), [])
        self.assertIn("due", json.loads((files[0].parent.parent / "due.json").read_text(encoding="utf-8")))

    def test_prune(self):
        for sid in ("old", "new"):
            _util.transcript(self.repo, sid, [("user", sid)])
        cu.backfill(self.repo)
        path = next(p for p in self.sessions() if p.stem == "old")  # make "old" months old
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["ts_end"] = "2026-01-01T00:00:00.000Z"
        path.write_text(json.dumps(rec), encoding="utf-8")
        now = datetime.now(timezone.utc)
        self.assertEqual(cu.prune(self.repo, "", now), 0)  # nothing is covered yet
        self.assertEqual(cu.prune(self.repo, "2026-02-01T00:00:00.000Z", now), 1)
        self.assertEqual([p.stem for p in self.sessions()], ["new"])


class DueAndWatermark(Base):
    def make(self, n):
        for i in range(n):
            _util.transcript(self.repo, f"s{i:02d}", [("user", f"turn {i}")])
            time.sleep(0.005)  # distinct millisecond timestamps
        cu.backfill(self.repo)

    def test_due_and_watermark(self):
        cfg = _kit.config()
        now = datetime.now(timezone.utc)
        self.assertEqual(cu.watermark(self.repo), "")
        self.make(1)
        self.assertEqual(cu.due(self.repo, cfg, now), (False, 1))
        self.ledger("2020-01-01T00:00:00.000Z")  # 21 days past the mark, with one record
        self.assertEqual(cu.due(self.repo, cfg, now), (True, 1))
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.ledger(yesterday, name="2026-10-02-0900")  # the newest ledger wins
        self.assertEqual(cu.due(self.repo, cfg, now), (False, 1))
        self.make(10)  # s00 again, and nine more
        self.assertEqual(cu.due(self.repo, cfg, now), (True, 10))

        records = cu.records_after(self.repo, "")
        self.ledger(records[7]["ts_end"], name="2026-10-03-0900")
        self.assertEqual(cu.watermark(self.repo), records[7]["ts_end"])
        self.assertEqual(cu.due(self.repo, cfg, now), (False, 2))
        self.assertEqual([r["id"] for r in cu.records_after(self.repo, cu.watermark(self.repo))],
                         [r["id"] for r in records[8:]])

        # an audit committed on `work` covers its sessions from main too: no due line, no
        # re-audit of what it applied
        _kit.git(self.repo, "switch", "-q", "-c", "work")
        self.ledger(records[-1]["ts_end"], name="2026-10-04-0900")
        _kit.git(self.repo, "switch", "-q", "main")
        self.assertEqual(cu.watermark(self.repo), records[-1]["ts_end"])
        self.assertEqual(cu.due(self.repo, cfg, now), (False, 0))

        # a teammate's unmerged ledger (a remote-tracking ref) or a stashed one is not ours: it
        # must not hide this clone's sessions
        late = "2099-01-01T00:00:00.000Z"
        _kit.git(self.repo, "switch", "-q", "-c", "theirs")
        sha = self.ledger(late, name="2026-10-05-0900")
        _kit.git(self.repo, "switch", "-q", "main")
        _kit.git(self.repo, "update-ref", "refs/remotes/origin/theirs", sha)
        _kit.git(self.repo, "update-ref", "refs/stash", sha)
        _kit.git(self.repo, "branch", "-q", "-D", "theirs")
        self.assertEqual(cu.watermark(self.repo), records[-1]["ts_end"])

        # nor is the Stage B audit branch, even as a local branch
        _kit.git(self.repo, "switch", "-q", "-c", "context/audit")
        self.ledger(late, name="2026-10-06-0900")
        _kit.git(self.repo, "switch", "-q", "main")
        self.assertEqual(cu.watermark(self.repo), records[-1]["ts_end"])

        # but a ledger merged on origin/<main> (PR mode, not pulled yet) is ours: without it the
        # next audit double-spends on sessions the merged one covered
        _kit.git(self.repo, "switch", "-q", "-c", "pr")
        merged = self.ledger(late, name="2026-10-07-0900")
        _kit.git(self.repo, "switch", "-q", "main")
        _kit.git(self.repo, "branch", "-q", "-D", "pr")
        _kit.git(self.repo, "update-ref", "refs/remotes/origin/main", merged)
        self.assertEqual(cu.watermark(self.repo), late)


class Seed(Base):
    def test_seed_on_adoption_keeps_every_captured_session_due(self):
        """F9: a seed at `now` would hide every pre-kit session from the first audit."""
        for sid in ("a1", "a2", "a3"):
            _util.transcript(self.repo, sid, [("user", f"ask {sid}"), ("assistant", "done")])
            time.sleep(0.005)
        path = cu.seed(self.repo)  # backfills first: the store may not be filled yet
        _util.commit(self.repo, "setup")
        self.assertEqual(path.parent, self.repo / "docs" / "references" / "session-audits")
        self.assertEqual(len(cu.records_after(self.repo, cu.watermark(self.repo))), 3)
        self.assertEqual(cu.due(self.repo, {"audit_every_sessions": 3},
                                datetime.now(timezone.utc)), (True, 3))

    def test_seed_refused_once_a_ledger_gives_a_watermark(self):
        """A seed would move the watermark back and re-audit what was covered."""
        import upkeep_tiers as ut
        _util.transcript(self.repo, "a1", [("user", "ask"), ("assistant", "done")])
        self.ledger("2026-10-01T00:00:00.000Z")
        with self.assertRaises(ut.GuardError) as caught:
            cu.seed(self.repo)
        self.assertIn("2026-10-01T00:00:00.000Z", str(caught.exception))
        self.assertIn("delete", str(caught.exception))


class Attention(Base):
    def setUp(self):
        super().setUp()
        _kit.git(self.repo, "config", "contextkit.autoAudit", "true")
        self.cdir = _kit.upkeep_dir(self.repo)
        env = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.repo)})
        env.start()
        self.addCleanup(env.stop)
        # Only the module's own `subprocess` is faked: _kit.git must keep running real git.
        self.popen_mock = mock.MagicMock()
        fake = mock.patch.object(cu, "subprocess", mock.Mock(
            Popen=self.popen_mock, DEVNULL=subprocess.DEVNULL))
        fake.start()
        self.addCleanup(fake.stop)

    def run_attention(self, payload=None):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload or {"session_id": "s1"}))), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(cu.main(["attention"]), 0)
        return out.getvalue()

    def test_attention_json_only_fast_and_detached(self):
        self.assertEqual(self.run_attention(), "")
        stub = json.loads((self.cdir / "sessions" / "s1.json").read_text(encoding="utf-8"))
        self.assertEqual(stub["start_sha"], _kit.git(self.repo, "rev-parse", "HEAD").stdout.strip())

        _kit.atomic_write(self.cdir / "due.json", json.dumps({"due": False, "n": 3, "since": ""}))
        self.assertEqual(self.run_attention(), "")

        _kit.atomic_write(self.cdir / "due.json",
                          json.dumps({"due": True, "n": 12, "since": "2026-11-01T00:00:00.000Z"}))
        out = self.run_attention()
        msg = json.loads(out)
        self.assertEqual(list(msg), ["systemMessage"])
        self.assertEqual(msg["systemMessage"],
                         "context: audit due — 12 sessions since 2026-11-01. Run /auditing-sessions")
        self.assertEqual(out.count("\n"), 1)
        # while the headless run of that audit is going, "due" would send the owner to a
        # `batch` that refuses: say nothing
        _kit.atomic_write(self.cdir / "state.json", json.dumps({"status": "running", "pid": 1}))
        self.assertEqual(self.run_attention(), "")
        (self.cdir / "state.json").unlink()

        # env only: a transcript that says sdk is never opened, so this still counts as owner
        sdk = _util.transcript(self.repo, "s2", [("user", "hi")], entrypoint="sdk-cli")
        self.popen_mock.reset_mock()
        with mock.patch.object(_kit, "git", wraps=_kit.git) as git:
            self.run_attention({"session_id": "s2", "transcript_path": str(sdk)})
        self.assertEqual(git.call_count, 2)  # contextkit.autoAudit, and one rev-parse
        self.assertTrue((self.cdir / "sessions" / "s2.json").exists())
        self.popen_mock.assert_called_once()

        (argv,), kw = self.popen_mock.call_args
        self.assertEqual(argv[1:], [str(UPKEEP), "_helper", "--repo", str(self.repo)])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[stream], subprocess.DEVNULL)
        if os.name == "nt":
            flags = kw["creationflags"]
            self.assertEqual(flags & 0x01000208, 0x01000208)  # detached, new group, breakaway
        else:
            self.assertTrue(kw["start_new_session"])

    @unittest.skipUnless(os.name == "nt", "job objects are a Windows thing")
    def test_windows_retries_without_breakaway(self):
        self.popen_mock.side_effect = [OSError("access denied"), mock.MagicMock()]
        self.run_attention()
        self.assertEqual(self.popen_mock.call_count, 2)
        self.assertEqual(self.popen_mock.call_args.kwargs["creationflags"], 0x208)

    def test_attention_does_nothing_off_the_owner_path(self):
        _kit.atomic_write(self.cdir / "due.json", json.dumps({"due": True, "n": 12, "since": ""}))
        for env in ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, {"CONTEXT_KIT_HEADLESS": "1"}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(self.run_attention(), "")
        _kit.git(self.repo, "config", "--unset", "contextkit.autoAudit")
        self.assertEqual(self.run_attention(), "")
        self.assertFalse((self.cdir / "sessions").exists())
        self.popen_mock.assert_not_called()

    def test_attention_by_hand_never_blocks(self):
        """Run bare from a shell it must not wait on a payload nobody will send."""
        reads = []

        class Tty(io.StringIO):
            def isatty(self):
                return True

            def read(self, *a):
                reads.append(1)  # a real terminal would block here
                return ""
        with mock.patch.object(sys, "stdin", Tty()), mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(cu.main(["attention"]), 0)
        self.assertEqual(reads, [])


if __name__ == "__main__":
    unittest.main()
