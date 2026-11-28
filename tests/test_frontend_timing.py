from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from testgate.bundle import runtime_archive
from testgate.policy import load, validate
from testgate.timing import PhaseTiming


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = os.environ.get("TESTGATE_FRONTEND_NODE_MODULES")
AVAILABLE = os.name == "posix" and DEPENDENCIES and shutil.which("node")


class FrontendCommandTests(unittest.TestCase):
    def test_adapter_names_and_direct_commands(self):
        policy, _ = load()
        for framework, arguments in (("vitest", ["run", "--maxWorkers=1"]), ("jest", ["--runInBand"]), ("playwright", ["test", "--workers=1"])):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as temporary:
                settings = {"adapter": framework, "startup_seconds": 30, "execution_seconds": 10}
                validate({**policy, "timing": settings})
                timing = PhaseTiming(settings)
                try:
                    command = timing.prepare([framework, *arguments], Path(temporary), docker=True)
                    self.assertEqual(command[:3], ["node", "/testgate-reports/" + timing.signal.parent.name + "/run.cjs", framework])
                    self.assertTrue((timing.signal.parent / "phase.cjs").is_file())
                finally:
                    timing.close()
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_interactive_and_non_execution_modes_are_rejected(self):
        for framework, subcommand in (("vitest", ["run"]), ("jest", []), ("playwright", ["test"])):
            settings = {"adapter": framework, "startup_seconds": 30, "execution_seconds": 10}
            for option in ("--watch", "--watchAll=true", "--watch-all", "--ui", "--ui-host=localhost", "--debug", "--list", "--listTests", "--showConfig"):
                with self.subTest(framework=framework, option=option), tempfile.TemporaryDirectory() as temporary:
                    timing = PhaseTiming(settings)
                    with self.assertRaises(ValueError):
                        timing.prepare([framework, *subcommand, option], Path(temporary))
                    self.assertIsNone(timing.temporary)

    def test_node_entry_points_preserve_the_selected_node_and_test_arguments(self):
        cases = (
            ("vitest", "node_modules/vitest/vitest.mjs", ["run", "sample.test.js"], ["sample.test.js"]),
            ("jest", "node_modules/jest/bin/jest.js", ["--runInBand"], ["--runInBand"]),
            ("playwright", "node_modules/@playwright/test/cli.js", ["test", "--workers=1"], ["--workers=1"]),
        )
        for framework, script, arguments, expected in cases:
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as temporary:
                timing = PhaseTiming({"adapter": framework, "startup_seconds": 30, "execution_seconds": 10})
                try:
                    command = timing.prepare(["/opt/node/bin/node", script, *arguments], Path(temporary))
                    self.assertEqual(command[0], "/opt/node/bin/node")
                    self.assertEqual(command[2:], [framework, *expected])
                finally:
                    timing.close()


class FrontendFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="testgate-frontend-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.project = self.directory / "project"
        self.project.mkdir()
        (self.project / "node_modules").symlink_to(Path(DEPENDENCIES).resolve(), target_is_directory=True)
        (self.project / "package.json").write_text('{"private":true}', encoding="utf-8")
        self.reports = self.directory / "reports"
        self.framework_report = self.directory / "framework-report"
        self.setup_report = self.directory / "setup-report"
        self.config_loads = self.directory / "config-loads"
        self.configure()

    def configure(self):
        report = json.dumps(str(self.framework_report))
        hook = {"vitest": "onTestRunEnd", "jest": "onRunComplete", "playwright": "onEnd"}[self.framework]
        reporter = self.project / "reporter.cjs"
        reporter.write_text(
            f"module.exports = class {{ {hook}() {{ require('node:fs').writeFileSync({report}, 'finished'); }} onError(error) {{ console.error(error); }} }};\n",
            encoding="utf-8",
        )
        setup = self.project / "existing-setup.cjs"
        setup.write_text(
            f"require('node:fs').writeFileSync({json.dumps(str(self.setup_report))}, 'configured');\n",
            encoding="utf-8",
        )
        if self.framework == "vitest":
            config = {"test": {"reporters": [str(reporter)], "setupFiles": [str(setup)], "maxWorkers": 1, "minWorkers": 1}}
            filename = "vitest.config.cjs"
        elif self.framework == "jest":
            config = {"reporters": [str(reporter)], "setupFilesAfterEnv": [str(setup)], "testEnvironment": "node", "injectGlobals": False, "automock": True, "cache": False}
            filename = "jest.config.cjs"
        else:
            # These fixtures are plain CommonJS and need no Babel transform.
            config = {"reporter": [[str(reporter)]], "globalSetup": str(setup), "workers": 1, "testMatch": "**/*.spec.cjs", "build": {"external": ["**/*.cjs"]}}
            setup.write_text(f"module.exports = () => require('node:fs').writeFileSync({json.dumps(str(self.setup_report))}, 'configured');\n", encoding="utf-8")
            filename = "playwright.config.cjs"
        (self.project / filename).write_text(
            f"require('node:fs').appendFileSync({json.dumps(str(self.config_loads))}, 'load\\n');\nmodule.exports = {json.dumps(config)};\n",
            encoding="utf-8",
        )

    def source(self, *, loading=0, setup=False, fail=False):
        pause = f"Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, {loading});\n"
        if self.framework == "vitest":
            imports = "import {test, expect, beforeAll} from 'vitest';\n"
            fixture = "beforeAll(() => new Promise(() => {}));\n" if setup else ""
            filename = "sample.test.mjs"
        elif self.framework == "jest":
            imports = "const {test, expect, beforeAll} = require('@jest/globals');\n"
            fixture = "beforeAll(() => new Promise(() => {}));\n" if setup else ""
            filename = "sample.test.cjs"
        else:
            imports = "const {test, expect} = require('@playwright/test');\n"
            # Playwright imports files in both the loader and the worker. Delay
            # the worker to verify its loading is still in the startup phase.
            pause = f"if (process.env.TEST_WORKER_INDEX !== undefined) {{ {pause} }}\n"
            fixture = "test.beforeAll(() => new Promise(() => {}));\n" if setup else ""
            filename = "sample.spec.cjs"
        return filename, imports + pause + fixture + f"test('sample', () => expect(1).toBe({2 if fail else 1}));\n"

    def invoke(self, *, loading=0, setup=False, fail=False, startup=6, execution=0.5, archive=False):
        filename, source = self.source(loading=loading, setup=setup, fail=fail)
        (self.project / filename).write_text(source, encoding="utf-8")
        policy, _ = load()
        policy["limits"]["seconds"] = 6
        policy["timing"] = {"adapter": self.framework, "startup_seconds": startup, "execution_seconds": execution}
        arguments = {"vitest": ["run"], "jest": ["--runInBand"], "playwright": ["test"]}[self.framework]
        policy["command"] = [self.framework, *arguments]
        path = self.directory / "policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        result_path = self.directory / "result.json"
        tool = [sys.executable, "-m", "testgate"]
        if archive:
            archive_path = self.directory / "testgate.pyz"
            archive_path.write_bytes(runtime_archive())
            tool = [sys.executable, str(archive_path)]
        completed = subprocess.run(
            [*tool, "--policy", str(path), "--cwd", str(self.project), "--report-dir", str(self.reports), "--json", str(result_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=8,
            env={**os.environ, "CI": "true"},
        )
        self.assertTrue(result_path.exists(), completed.stdout + completed.stderr)
        result = json.loads(result_path.read_text())
        self.assertEqual(list(self.reports.iterdir()), [], "adapter files must stay outside the project and be removed")
        return completed, result

    def test_loading_budget_and_existing_configuration_are_preserved(self):
        completed, result = self.invoke(loading=650)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertGreaterEqual(result["timing"]["startup_seconds"], 0.65)
        self.assertLess(result["timing"]["execution_seconds"], 0.5)
        self.assertTrue(self.framework_report.is_file(), "the configured reporter must still run")
        self.assertTrue(self.setup_report.is_file(), "the configured setup must still run")
        if self.framework == "jest":
            self.assertEqual(self.config_loads.read_text(), "load\n")

    def test_hanging_import_hits_startup_budget(self):
        completed, result = self.invoke(loading=30000, startup=0.4)
        self.assertEqual(completed.returncode, 124, completed.stdout + completed.stderr)
        self.assertEqual(result["violations"], ["startup time budget exceeded"])
        self.assertIsNone(result["timing"]["execution_seconds"])

    def test_before_all_consumes_execution_budget(self):
        completed, result = self.invoke(setup=True, execution=0.2)
        self.assertEqual(completed.returncode, 124, completed.stdout + completed.stderr)
        self.assertEqual(result["violations"], ["execution time budget exceeded"], completed.stdout + completed.stderr)

    def test_test_failures_preserve_framework_exit_status_in_zipapp(self):
        completed, result = self.invoke(fail=True, archive=True)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(result["command_exit_code"], 1)
        self.assertIsNotNone(result["timing"]["execution_seconds"])


@unittest.skipUnless(AVAILABLE, "requires Node and TESTGATE_FRONTEND_NODE_MODULES")
class VitestTimingTests(FrontendFixture, unittest.TestCase):
    framework = "vitest"


@unittest.skipUnless(AVAILABLE, "requires Node and TESTGATE_FRONTEND_NODE_MODULES")
class JestTimingTests(FrontendFixture, unittest.TestCase):
    framework = "jest"


@unittest.skipUnless(AVAILABLE, "requires Node and TESTGATE_FRONTEND_NODE_MODULES")
class PlaywrightTimingTests(FrontendFixture, unittest.TestCase):
    framework = "playwright"


@unittest.skipUnless(DEPENDENCIES and os.environ.get("TESTGATE_NODE_DOCKER_TEST_IMAGE"), "requires frontend dependencies and a Node Docker test image")
class DockerFrontendTimingTests(unittest.TestCase):
    def test_node_adapter_signals_across_the_report_mount(self):
        from testgate.runner import run

        with tempfile.TemporaryDirectory(prefix="testgate-node-container-") as temporary:
            directory = Path(temporary)
            project = directory / "project"
            project.mkdir()
            copy_file = os.link if Path(DEPENDENCIES).stat().st_dev == project.stat().st_dev else shutil.copy2
            shutil.copytree(DEPENDENCIES, project / "node_modules", symlinks=True, copy_function=copy_file)
            (project / "package.json").write_text('{"private":true}', encoding="utf-8")
            (project / "jest.config.cjs").write_text("module.exports = {testEnvironment:'node',cache:false};", encoding="utf-8")
            (project / "sample.test.cjs").write_text(
                "beforeAll(() => new Promise(() => {})); test('sample', () => expect(true).toBe(true));\n",
                encoding="utf-8",
            )
            policy, digest = load()
            policy["limits"]["seconds"] = 6
            policy["backend"] = {"type": "docker", "image": os.environ["TESTGATE_NODE_DOCKER_TEST_IMAGE"]}
            policy["timing"] = {"adapter": "jest", "startup_seconds": 4, "execution_seconds": 0.2}
            result, tail = run(policy, digest, project, ["jest", "--runInBand"], report_dir=directory / "reports")
            self.assertEqual(result["exit_code"], 124, tail.decode(errors="replace"))
            self.assertEqual(result["violations"], ["execution time budget exceeded"])
            self.assertIsNotNone(result["timing"]["execution_seconds"])
            self.assertEqual(list((directory / "reports").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
