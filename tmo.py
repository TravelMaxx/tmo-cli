#!/usr/bin/env python3
"""Repo wrapper: forwards to the unified dispatcher in lib/."""
import subprocess
import sys
from pathlib import Path

sys.exit(subprocess.run(
    [sys.executable, str(Path(__file__).parent / "lib" / "tmo_run.py")] + sys.argv[1:]
).returncode)
