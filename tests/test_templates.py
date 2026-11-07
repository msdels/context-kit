"""A fresh install, with the kit's own templates filled in and placed where INSTALL.md puts
them, passes the context check. A template line the check reads as a defect would fail the
first CI run of every new repo."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `unittest tests.test_templates`
import _util  # noqa: E402  (isolates the environment on import)


class FreshInstall(unittest.TestCase):
    def test_fresh_install_passes_the_check(self):
        with TemporaryDirectory(dir=_util.TMP) as tmp:
            repo = _util.make_repo(Path(tmp).resolve(), install=True)
            cfg = json.loads((repo / ".claude/context-kit.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["rules_max_line"], 300)
            self.assertNotIn("pruning", cfg)

            tpl = repo / ".claude/context-kit/templates"
            fill = lambda p: re.sub(r"\{\{[^}]*\}\}", "x", p.read_text(encoding="utf-8"))
            # Every placeholder becomes `x`, so the routing rows name `x/CLAUDE.md`.
            _util.write(repo, "CLAUDE.md", fill(tpl / "CLAUDE.md.tmpl"))
            for rel in ("x/CLAUDE.md", "tests/CLAUDE.md"):
                _util.write(repo, rel, fill(tpl / "module-CLAUDE.md.tmpl"))
            for name in ("mission.md", "domain-model.md"):
                _util.write(repo, f"docs/context/{name}", fill(tpl / f"docs-context/{name}.tmpl"))
            _util.write(repo, "docs/context/design-decisions.md",
                        fill(tpl / "docs-context/design-decisions.md"))
            _util.commit(repo, "setup")
            done = subprocess.run(
                [sys.executable, str(repo / ".claude/hooks/context_check.py")], cwd=repo,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertNotIn("Unrouted", done.stdout)
            self.assertNotIn("unconditional", done.stdout)  # no rules file loads at launch


class SkillCommands(unittest.TestCase):
    def test_every_skill_command_parses(self):
        """A skill that tells the agent a command the CLI rejects stalls the audit mid-run."""
        import contextlib
        import io
        import shlex
        import context_upkeep as cu
        cmds = set(cu.parser()._subparsers._group_actions[0].choices)
        checked, used = 0, {}
        for skill in sorted((_util.KIT / "skills").glob("*/SKILL.md")):
            text = skill.read_text(encoding="utf-8")
            if "context_upkeep.py" not in text:
                continue
            uses_u = "`U` below means" in text
            fenced = re.findall(r"(?ms)^```[^\n]*\n(.*?)^```", text)
            spans = re.findall(r"`([^`]+)`", re.sub(r"(?ms)^```.*?^```", "", text))
            spans += [ln for block in fenced for ln in block.split("\n") if ln.startswith("U ")]
            for span in spans:
                span = re.sub(r"\s#.*", "", span) if span.startswith("U ") else span
                # a placeholder or a quoted phrase is one argument; [optional] parts are kept
                argv = shlex.split(re.sub(r"<[^>]*>|\"[^\"]*\"", "1", span).replace("[", " ")
                                   .replace("]", " "))
                if uses_u:
                    if argv[:1] != ["U"] or len(argv) < 2:  # `U` itself, in its definition
                        continue
                    argv = argv[1:]
                elif len(argv) < 2 or argv[0] not in cmds:  # a bare `seen ...` after the path
                    continue
                with self.subTest(skill=skill.parent.name, span=span), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    try:
                        cu.parser().parse_args(argv)
                    except SystemExit:
                        self.fail(err.getvalue().strip().splitlines()[-1])
                checked += 1
                used.setdefault(skill.parent.name, set()).add(argv[0])
        self.assertGreater(checked, 20)
        # a renamed command in context-sync would be skipped above as "not a command"
        self.assertLessEqual({"move", "seen", "revise"}, used.get("context-sync", set()))


if __name__ == "__main__":
    unittest.main()
