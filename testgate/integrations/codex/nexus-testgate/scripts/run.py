"""Invoke the skill's bundled runtime with the project's selected Python."""
import os
import sys
from pathlib import Path

runtime = Path(__file__).resolve().parents[1] / "runtime" / "testgate.pyz"
if not runtime.is_file():
    raise SystemExit("Testgate runtime is missing; reinstall with 'testgate codex install'.")
os.execv(sys.executable, [sys.executable, "-B", str(runtime), *sys.argv[1:]])
