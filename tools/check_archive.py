"""Verify that the distributed zipapp preserves CLI and test failure statuses."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

archive = Path(__file__).resolve().parents[1] / "dist" / "testgate.pyz"
with tempfile.TemporaryDirectory() as temporary:
    cases = [(["--policy", str(Path(temporary) / "missing.json")], 2)]
    if os.name == "posix":
        cases.append((["--", sys.executable, "-c", "raise SystemExit(7)"], 7))
    for arguments, expected in cases:
        result = subprocess.run(
            [sys.executable, str(archive), *arguments], capture_output=True, timeout=10,
        )
        if result.returncode != expected:
            raise SystemExit(f"Archive returned {result.returncode}; expected {expected}")
print("Standalone archive failure statuses verified")
