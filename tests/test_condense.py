"""F-A2, F-B1, F-B7: `condense` counts only the owner's own turns, with their own replies."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _util  # noqa: E402,F401  (puts hooks/ on sys.path)
import audit_sessions  # noqa: E402


def user(text, n, **extra):
    content = text if isinstance(text, (str, list)) else str(text)
    return {"type": "user", "timestamp": f"2026-10-20T10:{n:02d}:00.000Z",
            "message": {"role": "user", "content": content}, **extra}


def agent(text, n):
    return {"type": "assistant", "timestamp": f"2026-10-20T10:{n:02d}:30.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class OwnerTurns(unittest.TestCase):
    def test_only_the_owner_is_an_owner_turn(self):
        records = [
            user("fix the bug", 1, origin={"kind": "human"}),
            agent("on it", 1),
            # tool result: structured, no text block
            user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}], 2,
                 toolUseResult={"stdout": "ok"}),
            # real shapes, as Claude Code writes them
            user("Another Claude session sent a message: done [Subagent hand-back]", 3,
                 isMeta=True, origin={"kind": "peer", "from": "a1"}),
            user("<task-notification><task-id>x</task-id></task-notification>", 4,
                 origin={"kind": "task-notification"}),
            user("Base directory for this skill: /x\n# Skill body", 5, isMeta=True,
                 sourceToolUseID="t2"),
            user("Stop hook feedback: You ended on an offer", 6, isMeta=True),
            user("[Usage limit reached; a short grace allowance remains]", 7, isMeta=True,
                 usageLimitNote=True),
            user("This session is being continued from a previous conversation", 8,
                 isCompactSummary=True),
            # the same shapes with no flags at all: the text alone must still exclude them
            user("Another Claude session sent a message: x", 9),
            user("[Subagent hand-back] lane C finished", 10),
            user("Base directory for this skill: /y", 11),
            user("Stop hook feedback: Wait", 12),
            user("PreToolUse:Bash hook blocking error: denied", 14),
            user("<system-reminder>be careful</system-reminder>", 15),
            user("<command-name>/clear</command-name>", 16),
            user("<local-command-stdout>ok</local-command-stdout>", 17),
            user("[Earlier usage-limit notes no longer apply]", 18),
            user("# Update Config Skill\nbody", 19),
            user("# Claude in Chrome browser automation\nbody", 20),
            user("## Page contract - read this first", 21),
            user("[Request interrupted by user]", 22),
            user("thanks, now ship it", 23, origin={"kind": "human"}),
            agent("shipped", 23),
            # F-B1: a `!` command the owner ran keeps its own turn; the later reply is its own
            user("<bash-input>railway login</bash-input>", 24),
            user("<bash-stdout></bash-stdout><bash-stderr>Cannot login</bash-stderr>", 25),
            agent("the login failed", 25),
        ]
        text, count, turns = audit_sessions.condense_records(records)
        self.assertEqual([t["owner"] for t in turns],
                         ["fix the bug", "thanks, now ship it", "! railway login"])
        self.assertEqual(count, 3)
        by_turn = text.split("### TURN ")
        self.assertIn("on it", by_turn[1])
        self.assertIn("shipped", by_turn[2])
        self.assertNotIn("the login failed", by_turn[2])
        self.assertIn("the login failed", by_turn[3])


if __name__ == "__main__":
    unittest.main()
