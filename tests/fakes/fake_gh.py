"""Fake `gh` for tests: logs argv and cwd to FAKE_LOG, behaviour chosen by FAKE_GH_MODE.

Modes so far: `ok` (exit 0, the default) and `fail` (exit 1). Later tasks add modes here.
"""
from __future__ import annotations

import json
import os
import sys

log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": "gh", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")

sys.exit(1 if os.environ.get("FAKE_GH_MODE") == "fail" else 0)
