"""The context check: pruning hygiene, the root CLAUDE.md char cap, what is counted, and text
hygiene scoped to context files.

An install per test is the cost, so each test is one repo state holding every case of its kind:
one where nothing may block, one where every defect must."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_context_check`
import _util  # noqa: E402  (isolates the environment on import)

RULES = ".claude/rules/api.md"
ARCHIVE = "docs/references/lessons-archive/kit-claude-rules-api.md"
DASH = "—"
ROOT_MD = ("# Project\n\n| Topic | Read first |\n|---|---|\n| api | `.claude/rules/api.md` |\n")
HEADER = "# Lessons archive: `old.md`\n\nNever loaded as context.\n\nMore prose.\n\nAnd more.\n\n"


def entry(eid, line="a line", frm=f"{RULES}#Errors", rows=("2026-10-31T14:05:00Z moved archive "
                                                           "· evicted · budget",)):
    out = f"## s:{eid} {DASH} title of {eid}\n"
    out += f"- line: {line}\n" if line is not None else ""
    out += f"- from: {frm}\n" if frm is not None else ""
    return out + "".join(f"- {r}\n" for r in rows) + "\n"


def moved(reason_and_evidence):
    return (f"2026-10-31 moved archive · {reason_and_evidence}",)


class PruningHygiene(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(dir=_util.TMP)
        self.addCleanup(self._tmp.cleanup)
        self.repo = _util.make_repo(Path(self._tmp.name).resolve(), install=True)

    def configure(self, **more):
        path = self.repo / ".claude" / "context-kit.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg.update(more)
        path.write_text(json.dumps(cfg), encoding="utf-8")

    def check(self):
        _util.commit(self.repo, "state")
        done = subprocess.run(
            [sys.executable, str(self.repo / ".claude" / "hooks" / "context_check.py")],
            cwd=self.repo, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        return done.returncode, done.stdout

    def test_well_formed_state_passes(self):
        """Every shape the kit writes, or a repo may already hold, must not block a push."""
        pins = "".join(f"- {c * 70} [s:pin-{c} pin]\n" for c in "ab")  # 2 x 86: under 25% of 1000
        _util.write(self.repo, "CLAUDE.md", ROOT_MD)
        _util.write(self.repo, RULES, f"# API\n\n## Errors\n{pins}- a\n- a\n- same text twice\n"
                                      "- same text twice\n- one [s:hist]\n")  # untagged twins
        _util.write(self.repo, "src/api/CLAUDE.md", "# api\n\n## Gotchas\n- untagged\n")
        _util.write(self.repo, ARCHIVE, "# Lessons archive\n\n" + entry("one")
                    + entry("two", rows=moved("enforced · cite README.md"))
                    + entry("hist")  # a live line's own history is not a duplicate ID
                    # evidence that dangles later is an audit input, not a block
                    + entry("gone", rows=moved("derivable · cite no/such.py::fn"))
                    + entry("away", rows=moved("duplicate · not-live-any-more")))
        _util.write(self.repo, "docs/references/lessons-archive/_gaps.md",
                    f"# Gaps\n\n## s:gap-a {DASH} g\n- source: session\n")
        _util.write(self.repo, "docs/references/lessons-archive/other-name.md",
                    HEADER + "tombstone: old.md → deleted\n\n")  # below a header
        _util.write(self.repo, "docs/context/overflow/random-notes.md",
                    "tombstone: old/path.md → deleted\n\n# Notes\n")
        _util.write(self.repo, "docs/references/lessons-archive/old-prose.md",
                    "legacy: kept as prose\n\n# Old\n")
        _util.write(self.repo, "docs/context/overflow/kit-claude-rules-api.md", "# Overflow\n\n")
        self.configure(rules_max_chars=1000, claude_md_max_chars=5000)
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertNotIn("Pruning", out)
        self.assertNotIn("> 5000", out)

        (self.repo / "docs" / "context" / "overflow" / "dir.md").mkdir()  # the check crashes:
        code, out = self.check()  # it blocks, because the headless validate relies on it
        self.assertEqual(code, 1, out)
        self.assertIn("could not run", out)

    def test_each_defect_blocks(self):
        pins = "".join(f"- {c * 70} [s:pin-{c} pin]\n" for c in "ab")  # 2 x 86: over 25% of 500
        _util.write(self.repo, RULES, f"# API\n\n## Errors\n{pins}- one [s:dup]\n- two [s:dup]\n"
                                      "- plain\n")
        bad = {
            "nofrom": entry("nofrom", frm=None),
            "noline": entry("noline", line=None),
            "whim": entry("whim", rows=moved("whim · x")),
            "noreason": entry("noreason", rows=("2026-10-31 moved archive",)),
            "nocite": entry("nocite", rows=moved("enforced · x")),
            "badcite": entry("badcite", rows=moved("derivable · cite ::sym")),
            "baddup": entry("baddup", rows=moved("duplicate · Not An ID!")),
            "nodup": entry("nodup", rows=moved("duplicate")),
            "nostale": entry("nostale", rows=moved("stale")),
        }
        _util.write(self.repo, ARCHIVE, "# A\n\n" + "".join(bad.values()) + entry("again") * 2)
        _util.write(self.repo, "docs/references/lessons-archive/other-name.md",
                    HEADER + "legacy: prose\n\n")  # too far down to count
        _util.write(self.repo, "docs/context/overflow/random-notes.md", "# Notes\n\n- hello\n")
        _util.write(self.repo, "docs/references/lessons-archive/claude-rules-api.md",
                    "# Pre-kit prose, unmarked\n")  # the bare slug is not the kit's name
        _util.write(self.repo, "CLAUDE.md", ROOT_MD + f"| old | `{ARCHIVE}` |\n" + "x" * 200 + "\n")
        needles = ("duplicate ID s:dup", "duplicate ID s:again", *(f"s:{k}:" for k in bad),
                   "other-name.md", "random-notes.md",
                   "lessons-archive/claude-rules-api.md: not named", "routed archive", f"{RULES}: pinned lines",
                   "chars > 100\n")

        self.configure(rules_max_chars=500, claude_md_max_chars=100)
        code, out = self.check()
        self.assertEqual(code, 1, out)
        for n in needles:  # each one is a block, not a note beside another block
            self.assertIn(n, out[out.index("FAIL"):])

    def test_deep_module_files_are_budgeted_and_checked(self):
        """A routed module five levels down must not be invisible; `.claude/` is not a module."""
        deep = "src/a/b/c/CLAUDE.md"
        _util.write(self.repo, "CLAUDE.md", ROOT_MD)
        _util.write(self.repo, deep, "# deep\n\n- one [s:dup]\n- two [s:dup]\n" + "x" * 200 + "\n")
        _util.write(self.repo, ".claude/notes/CLAUDE.md", "y" * 400 + "\n")
        self.configure(claude_md_max_chars=100)
        json_path = self.repo / ".claude" / "context-kit.json"
        cfg = json.loads(json_path.read_text(encoding="utf-8"))
        cfg.pop("claude_md_globs", None)  # the default is under test
        json_path.write_text(json.dumps(cfg), encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(f"{deep}: ", out)
        self.assertIn("chars > 100", out)
        self.assertIn("duplicate ID s:dup", out)
        self.assertNotIn(".claude/notes/CLAUDE.md:", out)

    def test_a_block_comment_costs_nothing_and_an_inline_one_counts(self):
        """Claude Code strips a CLAUDE.md's block-level HTML comments before loading it, so
        the budget must not count them; a comment inside a code fence is loaded and counts."""
        _util.write(self.repo, "CLAUDE.md", ROOT_MD)
        _util.write(self.repo, RULES, "# API\n")
        note = "<!--\n" + "maintainer note\n" * 40 + "-->\n"
        _util.write(self.repo, "src/api/CLAUDE.md", "# api\n\n" + note + "- a gotcha\n")
        self.configure(claude_md_max_chars=300)
        code, out = self.check()
        self.assertEqual(code, 0, out)
        _util.write(self.repo, "src/api/CLAUDE.md", "# api\n\n```\n" + note + "```\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("src/api/CLAUDE.md: ", out)
        _util.write(self.repo, "src/api/CLAUDE.md", "# api <!-- " + "x" * 400 + " -->\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        # a comment ends at its first `-->`: the lines between two comments are loaded
        _util.write(self.repo, "src/api/CLAUDE.md",
                    "<!-- a --> x\n" + "- a real line\n" * 30 + "<!-- b -->\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)

    def test_text_hygiene_reads_context_files_only(self):
        _util.write(self.repo, "CLAUDE.md", ROOT_MD)
        _util.write(self.repo, RULES, "# API\n")
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "app.py").write_bytes(b"\xef\xbb\xbfprint(1)\n")  # not context
        code, out = self.check()
        self.assertEqual(code, 0, out)
        (self.repo / "src" / "CLAUDE.md").write_bytes(b"\xef\xbb\xbf# src\n")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("src/CLAUDE.md: UTF-8 BOM", out)


if __name__ == "__main__":
    unittest.main()
