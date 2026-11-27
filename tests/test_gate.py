from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from testgate.policy import HTTPSRedirects, PolicyError, load, validate
from testgate.backends import Backend
from testgate.backends import _group_has_exited


ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def test_builtin_has_a_version_and_fingerprint(self):
        policy, digest = load()
        self.assertEqual(policy["command"], ["make", "test"])
        self.assertEqual(len(digest), 64)

    def test_unknown_or_unsupported_constraints_are_rejected(self):
        original, _ = load()
        changes = [
            ("limits", {**original["limits"], "second": 10}),
            ("backend", {"type": "native", "memory_bytes": 1000}),
            ("backend", []),
            ("limits", {**original["limits"], "seconds": float("nan")}),
            ("limits", {**original["limits"], "output_lines": True}),
            ("checks", {"junit": {"path": "../other.xml"}}),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value), self.assertRaises(PolicyError):
                validate({**original, field: value})

    def test_https_policy_is_verified_and_never_silently_replaced(self):
        policy, _ = load()
        raw = json.dumps(policy).encode()
        with patch("testgate.policy.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(raw)
            loaded, digest = load("https://example.invalid/policy.json", hashlib.sha256(raw).hexdigest())
            self.assertEqual(loaded, policy)
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Accept"), "application/octet-stream")
        with patch("testgate.policy.build_opener") as opener:
            opener.return_value.open.side_effect = OSError("offline")
            with self.assertRaises(OSError):
                load("https://example.invalid/policy.json")

    def test_policy_mismatch_duplicates_and_http_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_text('{"version": 1, "version": 1}')
            with self.assertRaises(PolicyError):
                load(str(path))
            policy, _ = load()
            path.write_text(json.dumps(policy))
            with self.assertRaises(PolicyError):
                load(str(path), "0" * 64)
        with self.assertRaises(PolicyError):
            load("http://example.invalid/policy.json")
        with self.assertRaises(PolicyError):
            HTTPSRedirects().redirect_request(None, None, 302, "", {}, "http://example.invalid/policy.json")

    def test_docker_refuses_unsupported_limits(self):
        backend = Backend({"backend": {"type": "docker", "image": "python:3.11-slim", "cpus": 1}}, ROOT)
        with patch("testgate.backends.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"CpuCfsQuota": false}'
            with self.assertRaisesRegex(ValueError, "does not support"):
                backend.prepare(["python", "-V"])

    def test_docker_arguments_include_resources_and_separate_creation(self):
        backend = Backend({"backend": {
            "type": "docker", "image": "python:3.11-slim", "memory_bytes": 1024,
            "cpus": 0.5, "pids": 8,
        }}, ROOT)
        with patch("testgate.backends.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({"MemoryLimit": True, "SwapLimit": True, "CpuCfsQuota": True, "PidsLimit": True})
            invocation = backend.prepare(["python", "-V"])
            created = run.call_args_list[1].args[0]
            self.assertEqual(invocation[:3], ["docker", "start", "--attach"])
            for flag in ("--read-only", "--memory", "--memory-swap", "--cpus", "--pids-limit", "--network", "--tmpfs"):
                self.assertIn(flag, created)
            self.assertEqual(created[created.index("--network") + 1], "none")

    @unittest.skipUnless(os.name == "posix", "POSIX process-group behavior")
    def test_darwin_permission_error_only_ignored_for_exited_groups(self):
        backend = Backend({"backend": {"type": "native"}}, ROOT)
        with patch("testgate.backends.os.name", "posix"), patch("testgate.backends.sys.platform", "darwin"), patch("testgate.backends.os.killpg", side_effect=PermissionError), patch("testgate.backends._group_has_exited") as exited:
            process = type("Process", (), {"pid": 12345})()
            exited.return_value = True
            backend.cleanup(process)
            exited.return_value = False
            with self.assertRaises(PermissionError):
                backend.cleanup(process)

    def test_group_exit_probe_does_not_ignore_live_members_or_probe_errors(self):
        with patch("testgate.backends.subprocess.run") as probe:
            probe.return_value.returncode = 0
            probe.return_value.stdout = '12345 Z\n67890 S\n'
            self.assertTrue(_group_has_exited(12345))
            probe.return_value.stdout = '12345 S\n'
            self.assertFalse(_group_has_exited(12345))
            probe.return_value.returncode = 1
            self.assertFalse(_group_has_exited(12345))


@unittest.skipUnless(os.name == "posix", "native runner requires POSIX")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.outputs = tempfile.TemporaryDirectory()
        self.addCleanup(self.outputs.cleanup)
        self.report_dir = Path(self.outputs.name) / "reports"
        self.report_dir.mkdir()

    def invoke(self, code, *, limits=None, checks=None, policy_changes=None, extra=()):
        policy, _ = load()
        policy["name"] = "integration-test"
        policy["command"] = [sys.executable, "-c", "import os; from pathlib import Path; " + code]
        policy["limits"].update(limits or {})
        policy["checks"] = checks or {}
        policy.update(policy_changes or {})
        path = self.root / "policy.json"
        path.write_text(json.dumps(policy))
        result_path = Path(self.outputs.name) / "result.json"
        result_path.unlink(missing_ok=True)
        completed = subprocess.run(
            [sys.executable, "-m", "testgate", "--policy", str(path), "--cwd", str(self.root), "--json", str(result_path), "--report-dir", str(self.report_dir), *extra],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result_path.read_text()) if result_path.exists() else None
        return completed, data

    def test_success_combines_streams_and_suppresses_details(self):
        completed, result = self.invoke(
            "import os; os.write(1, b'hello'); os.write(2, b'world')",
            limits={"output_bytes": 10},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["passed"])
        self.assertEqual(result["output_bytes"], 10)
        self.assertNotIn("hello", completed.stdout)

    def test_test_failure_preserves_status_and_diagnostics(self):
        completed, result = self.invoke("import sys; print('expected 2, got 3'); sys.exit(7)")
        self.assertEqual(completed.returncode, 7)
        self.assertEqual(result["command_exit_code"], 7)
        self.assertIn("expected 2, got 3", completed.stdout)

    def test_output_byte_and_line_limits(self):
        cases = [
            ("import os\nwhile True: os.write(2, b'x' * 65536)", {"output_bytes": 8192}, "output_bytes"),
            ("print('one\\ntwo')", {"output_lines": 1}, "output_lines"),
            ("print('x' * 20)", {"line_bytes": 10}, "line_bytes"),
            ("print('x')", {"output_bytes": 0}, "output_bytes"),
        ]
        for code, limits, metric in cases:
            with self.subTest(metric=metric, limits=limits):
                completed, result = self.invoke(code, limits=limits)
                self.assertEqual(completed.returncode, 125, completed.stderr)
                self.assertIn(f"{metric} budget exceeded", result["violations"])
                self.assertLess(len(completed.stdout.encode()), 5000)

    def test_closed_output_does_not_bypass_timeout(self):
        completed, result = self.invoke(
            "import os, time; os.close(1); os.close(2); time.sleep(30)", limits={"seconds": 0.2}
        )
        self.assertEqual(completed.returncode, 124, completed.stderr)
        self.assertIn("time budget exceeded", result["violations"])

    def test_timeout_kills_descendants(self):
        code = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "pathlib.Path('child.pid').write_text(str(child.pid)); time.sleep(30)"
        )
        completed, _ = self.invoke(code, limits={"seconds": 0.5})
        self.assertEqual(completed.returncode, 124, completed.stderr)
        pid = int((self.root / "child.pid").read_text())
        if sys.platform == "linux":
            status = Path(f"/proc/{pid}/status")
            deadline = time.monotonic() + 1
            while status.exists():
                try:
                    state = next(line for line in status.read_text().splitlines() if line.startswith("State:"))
                except FileNotFoundError:
                    break
                if "Z" in state:
                    break
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    self.fail(f"child survived timeout: {state}")
                time.sleep(0.01)

    def test_report_failure_cannot_be_hidden_by_exit_zero(self):
        xml = '<testsuite><testcase><skipped/></testcase></testsuite>'
        completed, result = self.invoke(
            f"from pathlib import Path; (Path(os.environ['TESTGATE_REPORT_DIR']) / 'junit.xml').write_text({xml!r})",
            checks={"junit": {"path": "junit.xml", "max_skipped": 0}},
        )
        self.assertEqual(completed.returncode, 126, completed.stderr)
        self.assertIn("JUnit skipped tests exceed budget", result["violations"])

    def test_missing_stale_and_empty_reports_fail(self):
        for mode in ("missing", "stale", "empty"):
            with self.subTest(mode=mode):
                path = self.report_dir / "junit.xml"
                path.unlink(missing_ok=True)
                if mode == "stale":
                    path.write_text('<testsuite><testcase/></testsuite>')
                code = "pass" if mode != "empty" else "from pathlib import Path; (Path(os.environ['TESTGATE_REPORT_DIR']) / 'junit.xml').write_text('<testsuite/>')"
                completed, _ = self.invoke(code, checks={"junit": {"path": "junit.xml"}})
                self.assertEqual(completed.returncode, 126, completed.stderr)

    def test_coverage_and_artifact_budgets(self):
        code = (
            "from pathlib import Path; "
            "(Path(os.environ['TESTGATE_REPORT_DIR']) / 'coverage.xml').write_text('<coverage line-rate=\"0.7\" branch-rate=\"0.6\"/>'); "
            "Path('artifact.bin').write_bytes(b'x' * 20)"
        )
        completed, result = self.invoke(code, checks={
            "coverage": {"path": "coverage.xml", "min_line_rate": 0.8},
            "artifacts": {"paths": ["artifact.bin"], "max_bytes": 10},
        })
        self.assertEqual(completed.returncode, 126, completed.stderr)
        self.assertEqual(len(result["violations"]), 2)

    def test_fresh_reports_can_pass(self):
        code = (
            "from pathlib import Path; "
            "(Path(os.environ['TESTGATE_REPORT_DIR']) / 'junit.xml').write_text('<testsuites><testsuite><testcase/></testsuite></testsuites>'); "
            "(Path(os.environ['TESTGATE_REPORT_DIR']) / 'coverage.xml').write_text('<coverage line-rate=\"0.9\"/>')"
        )
        completed, result = self.invoke(code, checks={
            "junit": {"path": "junit.xml", "min_tests": 1},
            "coverage": {"path": "coverage.xml", "min_line_rate": 0.8},
        })
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["checks"]["junit"]["tests"], 1)

    def test_fixed_command_cannot_be_overridden(self):
        completed, result = self.invoke("pass", policy_changes={"allow_command_override": False}, extra=("--", "echo", "bypass"))
        self.assertEqual(completed.returncode, 2)
        self.assertIsNone(result)
        self.assertIn("does not permit command overrides", completed.stderr)

    def test_artifact_symlink_outside_project_fails(self):
        (self.root / "artifact").symlink_to(ROOT / "pyproject.toml")
        completed, _ = self.invoke("pass", checks={"artifacts": {"paths": ["artifact"], "max_bytes": 99999}})
        self.assertEqual(completed.returncode, 126)


@unittest.skipUnless(os.environ.get("TESTGATE_DOCKER_TEST_IMAGE"), "set TESTGATE_DOCKER_TEST_IMAGE to run container integration tests")
class DockerTests(unittest.TestCase):
    def test_timeout_removes_the_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, digest = load()
            policy["backend"] = {"type": "docker", "image": os.environ["TESTGATE_DOCKER_TEST_IMAGE"]}
            policy["command"] = ["python", "-c", "import time; time.sleep(30)"]
            policy["limits"]["seconds"] = 0.2
            name = "testgate-" + root.name.replace("-", "")
            from testgate.runner import run
            with patch("testgate.backends.uuid.uuid4") as uuid:
                uuid.return_value.hex = name.removeprefix("testgate-")
                result, _ = run(policy, digest, root, policy["command"])
            self.assertEqual(result["exit_code"], 124)
            inspected = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=10)
            self.assertNotEqual(inspected.returncode, 0, "test container was left behind")

    def test_container_enforces_kernel_resource_and_network_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, _ = load()
            policy["backend"] = {
                "type": "docker", "image": os.environ["TESTGATE_DOCKER_TEST_IMAGE"],
                "memory_bytes": 67108864, "cpus": 0.5, "pids": 16,
                "tmpfs_bytes": 1048576, "network": "none",
            }
            code = (
                "import pathlib, os; p = pathlib.Path; "
                "v2 = p('/sys/fs/cgroup/memory.max').exists(); "
                "memory = p('/sys/fs/cgroup/memory.max' if v2 else '/sys/fs/cgroup/memory/memory.limit_in_bytes'); "
                "assert memory.read_text().strip() == '67108864'; "
                "pids = p('/sys/fs/cgroup/pids.max' if v2 else '/sys/fs/cgroup/pids/pids.max'); "
                "assert pids.read_text().strip() == '16'; "
                "quota, period = (map(int, p('/sys/fs/cgroup/cpu.max').read_text().split()) if v2 else "
                "(int(p('/sys/fs/cgroup/cpu/cpu.cfs_quota_us').read_text()), int(p('/sys/fs/cgroup/cpu/cpu.cfs_period_us').read_text()))); "
                "assert quota / period == 0.5; "
                "assert len(p('/proc/net/route').read_text().splitlines()) == 1; "
                "assert os.statvfs('/tmp').f_blocks * os.statvfs('/tmp').f_frsize <= 1048576; "
                "p(os.environ['TESTGATE_REPORT_DIR'], 'junit.xml').write_text('<testsuite><testcase/></testsuite>'); "
                "p('/workspace/verified').write_text('ok')"
            )
            policy["checks"] = {"junit": {"path": "junit.xml"}}
            policy["command"] = ["python", "-c", code]
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(policy))
            completed = subprocess.run(
                [sys.executable, "-m", "testgate", "--policy", str(policy_path), "--cwd", str(root)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual((root / "verified").read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
