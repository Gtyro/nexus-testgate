from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from testgate.bundle import runtime_archive
from testgate.policy import PolicyError, load, validate
from testgate.timing import PhaseTiming


ROOT = Path(__file__).resolve().parents[1]


class TimingPolicyTests(unittest.TestCase):
    def test_phase_budgets_default_to_five_seconds_and_allow_valid_overrides(self):
        policy, _ = load()
        for adapter in ("pytest", "vitest", "jest", "playwright"):
            for overrides in ({}, {"startup_seconds": 2}, {"execution_seconds": 3}):
                with self.subTest(adapter=adapter, overrides=overrides):
                    settings = {"adapter": adapter, **overrides}
                    validate({**policy, "timing": settings})
                    timing = PhaseTiming(settings)
                    self.assertEqual(timing.remaining(100, 100), (overrides.get("startup_seconds", 5), "startup"))
                    timing.execution_started = 101
                    self.assertEqual(timing.remaining(101, 100), (overrides.get("execution_seconds", 5), "execution"))
        timing = {"adapter": "pytest", "startup_seconds": 30, "execution_seconds": 10}
        self.assertEqual(validate({**policy, "timing": timing})["timing"], timing)
        changes = [{"adapter": "unknown"}, {"unknown": 1}]
        for key in ("startup_seconds", "execution_seconds"):
            changes.extend({key: value} for value in (None, 0, -1, True, float("inf"), float("nan"), "10"))
        for change in changes:
            with self.subTest(change=change), self.assertRaises(PolicyError):
                validate({**policy, "timing": {**timing, **change}})
        for value in (None, [], {}):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                validate({**policy, "timing": value})

    def test_docker_adapter_paths_stay_in_the_external_report_mount(self):
        timing = PhaseTiming({"adapter": "pytest", "startup_seconds": 30, "execution_seconds": 10})
        with tempfile.TemporaryDirectory() as temporary:
            try:
                command = timing.prepare(["python", "-m", "pytest", "-q"], Path(temporary), docker=True)
                self.assertEqual(command[0], "python")
                self.assertEqual(command[3:], ["-q"])
                self.assertTrue(Path(command[1]).is_relative_to("/testgate-reports"))
                self.assertTrue(command[1].startswith("/testgate-reports/"))
                self.assertEqual(Path(command[2]).parent, Path(command[1]).parent)
                self.assertTrue((timing.signal.parent / "pytest_adapter.py").is_file())
            finally:
                timing.close()
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_late_phase_signal_cannot_hide_a_startup_overrun(self):
        timing = PhaseTiming({"adapter": "pytest", "startup_seconds": 1, "execution_seconds": 10})
        timing.execution_started = 102
        remaining, phase = timing.remaining(102, 100)
        self.assertEqual(phase, "startup")
        self.assertLess(remaining, 0)


@unittest.skipUnless(os.name == "posix" and importlib.util.find_spec("pytest"), "requires POSIX and pytest")
class PytestTimingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="testgate-phase-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project = self.directory / "project"
        self.project.mkdir()
        self.reports = self.directory / "reports"

    def invoke(self, source, *, startup=3, execution=0.3, total=4, arguments=(), command=None, archive=False):
        (self.project / "test_sample.py").write_text(source, encoding="utf-8")
        policy, _ = load()
        policy["limits"]["seconds"] = total
        policy["timing"] = {"adapter": "pytest", "startup_seconds": startup, "execution_seconds": execution}
        policy["command"] = command or [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *arguments]
        path = self.directory / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        result_path = self.directory / "result.json"
        result_path.unlink(missing_ok=True)
        tool = [sys.executable, "-m", "testgate"]
        if archive:
            payload = self.directory / "testgate.pyz"
            payload.write_bytes(runtime_archive())
            tool = [sys.executable, str(payload)]
        completed = subprocess.run(
            [*tool, "--policy", str(path), "--cwd", str(self.project), "--report-dir", str(self.reports), "--json", str(result_path)],
            cwd=ROOT, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True, text=True, timeout=8,
        )
        result = json.loads(result_path.read_text()) if result_path.exists() else None
        self.assertFalse(any(self.reports.glob("testgate-timing-*")), "adapter files must be removed")
        return completed, result

    def test_dependency_import_has_its_own_budget_in_source_and_zipapp(self):
        (self.project / "slow_dependency.py").write_text("import time\ntime.sleep(0.4)\n", encoding="utf-8")
        source = "import slow_dependency\ndef test_ok():\n    assert True\n"
        for archive in (False, True):
            with self.subTest(archive=archive):
                completed, result = self.invoke(source, archive=archive)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertGreaterEqual(result["timing"]["startup_seconds"], 0.4)
                self.assertLess(result["timing"]["execution_seconds"], 0.3)
                self.assertIn("startup=", completed.stdout)
                self.assertIn("execution=", completed.stdout)

    def test_hanging_dependency_import_hits_startup_budget(self):
        completed, result = self.invoke("import time\ntime.sleep(30)\ndef test_ok(): pass\n", startup=0.35)
        self.assertEqual(completed.returncode, 124, completed.stdout + completed.stderr)
        self.assertEqual(result["violations"], ["startup time budget exceeded"])
        self.assertIsNone(result["timing"]["execution_seconds"])

    def test_setup_call_and_teardown_each_consume_the_execution_budget(self):
        for phase in ("setup", "call", "teardown"):
            with self.subTest(phase=phase):
                setup = "time.sleep(30)" if phase == "setup" else "pass"
                call = "time.sleep(30)" if phase == "call" else "pass"
                teardown = "time.sleep(30)" if phase == "teardown" else "pass"
                source = (
                    "import pytest, time\n"
                    "@pytest.fixture\ndef fixture():\n"
                    f"    {setup}\n    yield\n    {teardown}\n"
                    f"def test_case(fixture):\n    {call}\n"
                )
                completed, result = self.invoke(source, execution=0.15)
                self.assertEqual(completed.returncode, 124, completed.stdout + completed.stderr)
                self.assertEqual(result["violations"], ["execution time budget exceeded"])
                self.assertGreaterEqual(result["timing"]["execution_seconds"], 0.15)

    def test_total_command_budget_still_applies(self):
        completed, result = self.invoke("import time\ntime.sleep(30)\n", total=0.15)
        self.assertEqual(completed.returncode, 124, completed.stdout + completed.stderr)
        self.assertEqual(result["violations"], ["time budget exceeded"])

    def test_success_without_executing_tests_cannot_bypass_phase_checks(self):
        completed, result = self.invoke("def test_ok(): pass\n", arguments=("--collect-only",))
        self.assertEqual(completed.returncode, 126, completed.stdout + completed.stderr)
        self.assertIn("pytest timing did not reach test execution", result["violations"])

    def test_collection_errors_and_test_failures_keep_pytest_exit_codes(self):
        for source, expected in (("raise RuntimeError('collection failed')\n", 2), ("def test_bad(): assert False\n", 1)):
            with self.subTest(expected=expected):
                completed, result = self.invoke(source)
                self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
                self.assertEqual(result["command_exit_code"], expected)

    def test_continuing_after_collection_errors_still_enforces_execution_budget(self):
        (self.project / "test_broken.py").write_text("raise RuntimeError('collection failed')\n", encoding="utf-8")
        completed, result = self.invoke(
            "import time\ndef test_slow(): time.sleep(30)\n",
            execution=0.15, arguments=("--continue-on-collection-errors",),
        )
        self.assertEqual(completed.returncode, 124, completed.stdout + completed.stderr)
        self.assertEqual(result["violations"], ["execution time budget exceeded"])

    def test_unsupported_command_fails_before_running(self):
        completed, result = self.invoke("def test_ok(): pass\n", command=[sys.executable, "-c", "print('unexpected')"])
        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires a direct Python command", completed.stderr)
        self.assertIsNone(result)
        self.assertNotIn("unexpected", completed.stdout)


@unittest.skipUnless(os.environ.get("TESTGATE_DOCKER_TEST_IMAGE") and importlib.util.find_spec("pytest"), "requires a Docker test image and host pytest")
class DockerTimingTests(unittest.TestCase):
    def test_container_reports_phases_and_enforces_execution_budget(self):
        from testgate.runner import run

        with tempfile.TemporaryDirectory(prefix="testgate-docker-phases-") as temporary:
            directory = Path(temporary)
            project = directory / "project"
            project.mkdir()
            # Supply the fixture's dependencies without network access or image changes.
            for name in ("pytest", "_pytest", "pluggy", "packaging", "iniconfig", "pygments", "py"):
                spec = importlib.util.find_spec(name)
                source = Path(spec.origin)
                if spec.submodule_search_locations is None:
                    shutil.copy2(source, project / source.name)
                else:
                    shutil.copytree(source.parent, project / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (project / "pytest.ini").write_text("[pytest]\naddopts = --assert=plain\n", encoding="utf-8")
            (project / "test_sample.py").write_text(
                "import time\ntime.sleep(0.3)\ndef test_slow(): time.sleep(30)\n", encoding="utf-8",
            )
            policy, digest = load()
            policy["limits"]["seconds"] = 5
            policy["backend"] = {"type": "docker", "image": os.environ["TESTGATE_DOCKER_TEST_IMAGE"]}
            policy["timing"] = {"adapter": "pytest", "startup_seconds": 4, "execution_seconds": 0.15}
            command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_sample.py"]
            result, tail = run(policy, digest, project, command, report_dir=directory / "reports")
            self.assertEqual(result["exit_code"], 124, tail.decode(errors="replace"))
            self.assertEqual(result["violations"], ["execution time budget exceeded"])
            self.assertGreaterEqual(result["timing"]["startup_seconds"], 0.3)
            self.assertGreaterEqual(result["timing"]["execution_seconds"], 0.15)
            self.assertEqual(list((directory / "reports").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
