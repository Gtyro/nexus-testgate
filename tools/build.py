"""Build a zipapp without fetching build tools or runtime dependencies."""

import hashlib
import shutil
import tempfile
import zipapp
from pathlib import Path


root = Path(__file__).resolve().parents[1]
destination = root / "dist"
destination.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as temporary:
    app = Path(temporary)
    shutil.copytree(root / "testgate", app / "testgate", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    output = destination / "testgate.pyz"
    zipapp.create_archive(app, output, interpreter="/usr/bin/env python3", main="testgate.cli:main", compressed=True)
digest = hashlib.sha256(output.read_bytes()).hexdigest()
policy = destination / "default-policy.json"
shutil.copyfile(root / "testgate" / "default-policy.json", policy)
policy_digest = hashlib.sha256(policy.read_bytes()).hexdigest()
(destination / "SHA256SUMS").write_text(
    f"{digest}  testgate.pyz\n{policy_digest}  default-policy.json\n", encoding="utf-8"
)
print(output)
