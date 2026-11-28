"""Supervise optional framework phases without changing the target project."""
from __future__ import annotations

import tempfile
from importlib.resources import files
from pathlib import Path, PurePosixPath

from .policy import DEFAULT_PHASE_SECONDS


class PhaseTiming:
    def __init__(self, settings):
        self.settings = {
            "startup_seconds": DEFAULT_PHASE_SECONDS,
            "execution_seconds": DEFAULT_PHASE_SECONDS,
            **settings,
        }
        self.temporary = None
        self.signal = None
        self.execution_started = None

    def prepare(self, command, report_dir, *, docker=False):
        framework = self.settings["adapter"]
        if framework == "pytest" and command[1:3] != ["-m", "pytest"]:
            raise ValueError("pytest timing requires a direct Python command: python -m pytest [args]")
        if framework != "pytest":
            node, arguments = self._frontend_command(command)
        self.temporary = tempfile.TemporaryDirectory(prefix="testgate-timing-", dir=report_dir)
        directory = Path(self.temporary.name)
        self.signal = directory / "phase"
        target = PurePosixPath("/testgate-reports") / directory.name if docker else directory
        resources = files("testgate")
        if framework == "pytest":
            adapter = directory / "pytest_adapter.py"
            adapter.write_bytes(resources.joinpath(adapter.name).read_bytes())
            return [command[0], str(target / adapter.name), str(target / self.signal.name), *command[3:]]
        for resource in resources.joinpath("frontend").iterdir():
            if resource.name.endswith(".cjs"):
                (directory / resource.name).write_bytes(resource.read_bytes())
        return [node, str(target / "run.cjs"), framework, *arguments]

    def _frontend_command(self, command):
        framework = self.settings["adapter"]
        executable = command[0].replace("\\", "/").rsplit("/", 1)[-1]
        if executable in ("node", "nodejs", "node.exe") and len(command) > 1:
            script = command[1].replace("\\", "/")
            scripts = {
                "vitest": ("vitest/vitest.mjs", ".bin/vitest"),
                "jest": ("jest/bin/jest.js", ".bin/jest"),
                "playwright": ("playwright/cli.js", "@playwright/test/cli.js", ".bin/playwright"),
            }
            if not any(script.endswith(suffix) for suffix in scripts[framework]):
                raise ValueError(f"{framework} timing requires the framework's Node CLI entry point")
            node, arguments = command[0], command[2:]
        elif executable in (framework, framework + ".cmd"):
            node, arguments = "node", command[1:]
        else:
            raise ValueError(f"{framework} timing requires a direct {framework} or node command")
        subcommand = {"vitest": "run", "playwright": "test"}.get(framework)
        if subcommand is not None:
            if not arguments or arguments[0] != subcommand:
                raise ValueError(f"{framework} timing requires '{framework} {subcommand}'")
            arguments = arguments[1:]
        for argument in arguments:
            option, _, value = argument.partition("=")
            interactive = option in ("--watch", "--watchAll", "--watch-all", "--ui", "--ui-host", "--ui-port", "--debug")
            if framework == "vitest" and option == "-w":
                interactive = True
            if interactive and value != "false":
                raise ValueError("phase timing requires a single non-interactive test run")
            if option in ("--list", "--listTests", "--showConfig", "--help", "--version", "-h", "-v"):
                raise ValueError("phase timing requires executing tests, not listing configuration or tests")
        return node, arguments

    def observe(self, now):
        try:
            with self.signal.open("rb") as stream:
                marker = stream.read(32)
        except FileNotFoundError:
            return
        if marker == b"execution\n":
            if self.execution_started is None:
                self.execution_started = now
        elif not b"execution\n".startswith(marker):
            raise ValueError(f"invalid {self.settings['adapter']} timing signal")

    def remaining(self, now, started):
        if self.execution_started is None:
            return self.settings["startup_seconds"] - (now - started), "startup"
        startup_remaining = self.settings["startup_seconds"] - (self.execution_started - started)
        if startup_remaining <= 0:
            return startup_remaining, "startup"
        return self.settings["execution_seconds"] - (now - self.execution_started), "execution"

    def metrics(self, started, finished):
        boundary = self.execution_started if self.execution_started is not None else finished
        return {
            "adapter": self.settings["adapter"],
            "startup_seconds": round(boundary - started, 6),
            "execution_seconds": round(finished - boundary, 6) if self.execution_started is not None else None,
        }

    def close(self):
        if self.temporary is not None:
            self.temporary.cleanup()
