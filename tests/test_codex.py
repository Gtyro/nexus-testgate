from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from testgate import __version__
from testgate.bundle import runtime_archive
from testgate.codex import update_latest


def fingerprint(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


class CodexIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = runtime_archive()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="testgate-integration-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "existing.txt").write_text("existing project content")
        self.home = self.base / "codex home"
        self.bin = self.base / "user bin"
        self.skill = self.home / "skills" / "nexus-testgate"
        self.alias = self.bin / ("testgate.cmd" if os.name == "nt" else "testgate")
        self.runtime = self.base / "download.pyz"
        self.runtime.write_bytes(self.payload)

    def manage(self, action, *extra, runtime=None):
        return subprocess.run([
            sys.executable, str(runtime or self.runtime), "codex", action,
            "--codex-home", str(self.home), "--bin-dir", str(self.bin), *extra,
        ], cwd=self.project, capture_output=True, text=True, timeout=15)

    def install(self):
        completed = self.manage("install")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_complete_lifecycle_is_independent_and_leaves_project_unchanged(self):
        before = fingerprint(self.project)
        self.install()
        self.assertTrue((self.skill / "SKILL.md").is_file())
        self.assertTrue(self.alias.is_file())
        self.runtime.unlink()  # Installed skill must not depend on the original download.
        installed_runtime = self.skill / "runtime" / "testgate.pyz"
        status = self.manage("status", runtime=installed_runtime)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(__version__, status.stdout)
        updated = self.manage("update", "--local", runtime=installed_runtime)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        helper = self.skill / "scripts" / "run.py"
        if os.name == "posix":
            ran = subprocess.run([
                str(self.alias), "--", sys.executable, "-c", "print('tests passed')",
            ], cwd=self.project, capture_output=True, text=True, timeout=10)
            self.assertEqual(ran.returncode, 0, ran.stderr + ran.stdout)
        checked = subprocess.run([
            sys.executable, str(helper), "--version",
        ], cwd=self.project, capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout.strip(), __version__)
        removed = self.manage("uninstall", runtime=installed_runtime)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.alias.exists())
        self.assertEqual(fingerprint(self.project), before)

    def test_install_is_idempotent_and_does_not_overwrite_foreign_files(self):
        self.install()
        before = fingerprint(self.home)
        second = self.manage("install")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(fingerprint(self.home), before)
        (self.skill / "SKILL.md").write_text("local customization")
        for action, options in (("update", ("--local",)), ("uninstall", ())):
            completed = self.manage(action, *options)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("modified", completed.stderr)
        self.assertEqual((self.skill / "SKILL.md").read_text(), "local customization")

    def test_foreign_skill_and_launcher_are_preserved(self):
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("foreign skill")
        failed = self.manage("install")
        self.assertEqual(failed.returncode, 2)
        self.assertEqual((self.skill / "SKILL.md").read_text(), "foreign skill")
        (self.skill / "SKILL.md").unlink()
        self.skill.rmdir()
        self.bin.mkdir()
        self.alias.write_text("foreign command")
        failed = self.manage("install")
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(self.alias.read_text(), "foreign command")

    def test_git_worktree_install_locations_are_rejected_without_changes(self):
        before = fingerprint(self.project)
        for arguments in (
            ("--codex-home", str(self.project / ".codex"), "--bin-dir", str(self.bin)),
            ("--codex-home", str(self.home), "--bin-dir", str(self.project / "bin")),
        ):
            completed = subprocess.run([
                sys.executable, str(self.runtime), "codex", "install", *arguments,
            ], cwd=self.project, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 2, completed.stdout)
            self.assertIn("outside Git", completed.stderr)
            self.assertEqual(fingerprint(self.project), before)

    @unittest.skipUnless(os.name == "posix", "symlink creation requires POSIX")
    def test_modified_launcher_symlink_does_not_remove_its_target(self):
        self.install()
        external = self.base / "external-command"
        external.write_bytes(self.alias.read_bytes())
        self.alias.unlink()
        self.alias.symlink_to(external)
        completed = self.manage("uninstall")
        self.assertEqual(completed.returncode, 2)
        self.assertTrue(external.is_file())
        self.assertTrue(self.skill.is_dir())

    def test_online_update_verifies_download_and_updates_skill_and_runtime_together(self):
        self.install()
        original = fingerprint(self.home)
        major, minor, patch_number = map(int, __version__.split("."))
        future_version = f"{major}.{minor}.{patch_number + 1}"
        release = json.dumps({"tag_name": "v" + future_version, "assets": [
            {"name": "SHA256SUMS", "id": 1}, {"name": "testgate.pyz", "id": 2},
        ]}).encode()

        def github_responses(responses):
            bodies = iter(responses)

            def open_request(request, timeout):
                accepted = {"application/json", "application/vnd.github+json"} if request.full_url.endswith("/releases/latest") else {"application/octet-stream"}
                if request.get_header("Accept") not in accepted:
                    raise HTTPError(request.full_url, 415, "Unsupported Media Type", {}, None)
                return io.BytesIO(next(bodies))

            return open_request

        with patch("testgate.codex.build_opener") as opener:
            opener.return_value.open.side_effect = github_responses([release, b"0" * 64 + b"  testgate.pyz\n", b"corrupt"])
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                update_latest(self.home, self.bin)
        self.assertEqual(fingerprint(self.home), original)
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.payload)) as source, zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                raw = source.read(item)
                if item.filename == "testgate/__init__.py":
                    raw = raw.replace(__version__.encode(), future_version.encode())
                target.writestr(item, raw)
        future = output.getvalue()
        checksum = hashlib.sha256(future).hexdigest().encode() + b"  testgate.pyz\n"
        with patch("testgate.codex.build_opener") as opener:
            opener.return_value.open.side_effect = github_responses([release, checksum, future])
            update_latest(self.home, self.bin)
        record = json.loads((self.skill / ".testgate-install.json").read_text())
        self.assertEqual(record["version"], future_version)
        checked = subprocess.run([
            sys.executable, str(self.skill / "scripts" / "run.py"), "--version",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout.strip(), future_version)

    @unittest.skipUnless(os.name == "posix", "native test runner requires POSIX")
    def test_reports_and_json_stay_outside_the_target_repository(self):
        self.install()
        before = fingerprint(self.project)
        policy_path = self.base / "policy.json"
        with zipfile.ZipFile(io.BytesIO(self.payload)) as archive:
            policy = json.loads(archive.read("testgate/default-policy.json"))
        policy["checks"] = {"junit": {"path": "junit.xml", "min_tests": 1, "max_skipped": 0}}
        policy_path.write_text(json.dumps(policy))
        result = self.base / "result.json"
        reports = self.base / "reports"
        command = [
            str(self.alias), "--policy", str(policy_path), "--json", str(result), "--report-dir", str(reports),
            "--", sys.executable, "-c",
            "import os; from pathlib import Path; "
            "Path(os.environ['TESTGATE_REPORT_DIR'], 'junit.xml').write_text('<testsuite><testcase/></testsuite>')",
        ]
        completed = subprocess.run(command, cwd=self.project, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(json.loads(result.read_text())["passed"])
        self.assertTrue((reports / "junit.xml").is_file())
        self.assertEqual(fingerprint(self.project), before)
        for flag in ("--json", "--report-dir"):
            forbidden = subprocess.run([
                str(self.alias), flag, str(self.project / "tool-output"), "--", sys.executable, "-c", "pass",
            ], cwd=self.project, capture_output=True, text=True, timeout=10)
            self.assertEqual(forbidden.returncode, 2)
            self.assertIn("outside", forbidden.stderr)
        self.assertEqual(fingerprint(self.project), before)


if __name__ == "__main__":
    unittest.main()
