"""The capture lock, and the installed hooks passing a strict host linter."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_kit` and discover
import _util  # noqa: E402  (isolates the environment on import)
import _kit  # noqa: E402


class KitHelpers(unittest.TestCase):
    def test_locked_breaks_a_stale_lock_and_times_out_on_a_live_one(self):
        # backfill.lock: a crashed helper's lock must not stop capture for good
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "n.lock"
            lock.write_text("")  # a crashed holder, 40 s old
            os.utime(lock, (time.time() - 40, time.time() - 40))
            with _kit.locked(lock, wait=0.5):
                self.assertTrue(lock.exists())
            self.assertFalse(lock.exists())

            lock.write_text("")  # a live holder: waiting out `wait` raises
            with self.assertRaises(TimeoutError):
                with _kit.locked(lock, wait=0.2):
                    pass
            self.assertTrue(lock.exists())

            # a lock whose stat keeps failing still times out, never spins forever
            real, got = Path.stat, []
            def stat(p, *a, **kw):
                if p.name == lock.name:
                    raise PermissionError("denied")
                return real(p, *a, **kw)

            def take():
                try:
                    with _kit.locked(lock, wait=0.2):
                        pass
                except TimeoutError as exc:
                    got.append(exc)
            with mock.patch.object(Path, "stat", stat):
                t = threading.Thread(target=take, daemon=True)
                t.start()
                t.join(5)
            self.assertFalse(t.is_alive())
            self.assertEqual(len(got), 1)


class InstalledHooksPassTheHostLinter(unittest.TestCase):
    """A host's ruff config must not fail the kit's scripts: a repo whose CI lints everything
    would fail every build, `accept`'s push included."""

    @unittest.skipUnless(shutil.which("ruff"), "ruff is not on PATH")
    def test_a_strict_host_ruff_passes_the_installed_hooks(self):
        with TemporaryDirectory() as tmp:
            repo = _util.make_repo(Path(tmp), install=True)
            _util.write(repo, "pyproject.toml", (
                '[tool.ruff]\nline-length = 79\ntarget-version = "py314"\n'
                '[tool.ruff.lint]\nselect = ["E", "F", "I", "B", "UP", "SIM", "N"]\n'))
            done = subprocess.run(
                [shutil.which("ruff"), "check", "--no-cache", ".claude/hooks"], cwd=repo,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
