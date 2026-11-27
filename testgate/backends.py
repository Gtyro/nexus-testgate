from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
import uuid


class Backend:
    def __init__(self, policy, root):
        self.settings = policy.get("backend", {"type": "native"})
        self.root = root
        self.name = None

    def prepare(self, command, report_dir=None):
        if report_dir is not None:
            report_target = "/testgate-reports" if self.settings["type"] == "docker" else str(report_dir)
            command = [argument.replace("{testgate_reports}", report_target) for argument in command]
        if self.settings["type"] == "native":
            if os.name != "posix":
                raise ValueError("native backend requires Linux/macOS; use the Docker backend on Windows")
            return command
        self.name = f"testgate-{uuid.uuid4().hex}"
        settings = self.settings
        info = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=10,
        )
        if info.returncode:
            raise ValueError("Docker daemon is not available")
        capabilities = json.loads(info.stdout)
        for key, capability in (("memory_bytes", "MemoryLimit"), ("memory_bytes", "SwapLimit"), ("cpus", "CpuCfsQuota"), ("pids", "PidsLimit")):
            if key in settings and not capabilities.get(capability):
                raise ValueError(f"Docker host does not support requested {key} limit ({capability})")
        mount = str(self.root.resolve())
        if "," in mount:
            raise ValueError("Docker workspace paths cannot contain commas")
        args = [
            "docker", "create", "--pull", "never", "--name", self.name, "--init", "--read-only",
            "--network", settings.get("network", "none"),
            "--mount", f"type=bind,source={mount},target=/workspace",
            "--workdir", "/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--tmpfs", f"/tmp:rw,size={settings.get('tmpfs_bytes', 67108864)}",
        ]
        if report_dir is not None:
            if "," in str(report_dir):
                raise ValueError("Docker report paths cannot contain commas")
            args.extend([
                "--mount", f"type=bind,source={report_dir},target=/testgate-reports",
                "--env", "TESTGATE_REPORT_DIR=/testgate-reports",
            ])
        for key, flag in (("cpus", "--cpus"), ("pids", "--pids-limit"), ("memory_bytes", "--memory")):
            if key in settings:
                args.extend([flag, str(settings[key])])
        if "memory_bytes" in settings:
            args.extend(["--memory-swap", str(settings["memory_bytes"])])
        try:
            created = subprocess.run(
                [*args, settings["image"], *command],
                capture_output=True, text=True, timeout=30,
            )
        except BaseException:
            self.cleanup(None)
            raise
        if created.returncode:
            self.cleanup(None)
            raise ValueError(f"cannot create test container: {created.stderr.strip()[:1000]}")
        return ["docker", "start", "--attach", self.name]

    def cleanup(self, process):
        if process is not None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin can report EPERM for an already empty process group.
                    # Only accept it after confirming that no live group member remains.
                    if sys.platform != "darwin" or not _group_has_exited(process.pid):
                        raise
            elif process.poll() is None:
                process.kill()
        if self.name:
            # Remove by name even if the docker client or container already exited.
            removed = subprocess.run(
                ["docker", "rm", "--force", self.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10,
            )
            if removed.returncode and b"No such container" not in removed.stderr:
                raise RuntimeError(f"could not remove test container {self.name}")


def _group_has_exited(group):
    inspected = subprocess.run(
        ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=2,
    )
    if inspected.returncode:
        return False
    for line in inspected.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == str(group) and not fields[1].startswith("Z"):
            return False
    return True
