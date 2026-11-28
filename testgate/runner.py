from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tempfile
from pathlib import Path

from .backends import Backend
from . import __version__
from .reports import check_reports, snapshot
from .locations import outside_project, temporary_parent
from .timing import PhaseTiming


TAIL_BYTES = 4096


def _read_output(stream, messages, stopped, read_size):
    try:
        while not stopped.is_set():
            chunk = os.read(stream.fileno(), read_size)
            while not stopped.is_set():
                try:
                    messages.put(chunk, timeout=0.05)
                    break
                except queue.Full:
                    pass
            if not chunk:
                return
    except (OSError, ValueError) as exc:
        if not stopped.is_set():
            messages.put(exc)


def run(policy, digest, root, command, report_dir=None):
    if report_dir is not None:
        report_dir = outside_project(report_dir, root)
        report_dir.mkdir(parents=True, exist_ok=True)
        return _run(policy, digest, root, command, report_dir)
    with tempfile.TemporaryDirectory(prefix="testgate-reports-", dir=temporary_parent(root)) as temporary:
        return _run(policy, digest, root, command, Path(temporary))


def _run(policy, digest, root, command, report_dir):
    timing = PhaseTiming(policy["timing"]) if "timing" in policy else None
    try:
        if timing is not None:
            command = timing.prepare(
                command, report_dir, docker=policy.get("backend", {}).get("type") == "docker",
            )
        return _supervise(policy, digest, root, command, report_dir, timing)
    finally:
        if timing is not None:
            timing.close()


def _supervise(policy, digest, root, command, report_dir, timing):
    checks = policy.get("checks", {})
    before = snapshot(report_dir, checks)
    backend = Backend(policy, root)
    invocation = backend.prepare(command, report_dir=report_dir)
    result = {
        "tool_version": __version__, "policy": policy["name"], "policy_sha256": digest,
        "backend": backend.settings["type"], "passed": False, "exit_code": 0,
        "command_exit_code": None, "violations": [],
        "seconds": 0.0, "output_bytes": 0, "output_lines": 0, "max_line_bytes": 0,
    }
    limits = policy["limits"]
    messages = queue.Queue(maxsize=4)
    stopped = threading.Event()
    process = None
    reader = None
    tail = bytearray()
    current_line = 0
    completed_lines = 0
    eof = False
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            invocation, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, start_new_session=os.name == "posix",
            env={**os.environ, "TESTGATE_REPORT_DIR": str(report_dir)},
        )
        reader = threading.Thread(
            target=_read_output,
            args=(process.stdout, messages, stopped, min(65536, limits["output_bytes"] + 1)),
            daemon=True,
        )
        reader.start()
        while True:
            now = time.monotonic()
            remaining = limits["seconds"] - (now - started)
            if remaining <= 0:
                result["violations"].append("time budget exceeded")
                result["exit_code"] = 124
                break
            if timing is not None:
                timing.observe(now)
                phase_remaining, phase = timing.remaining(now, started)
                if phase_remaining <= 0:
                    result["violations"].append(f"{phase} time budget exceeded")
                    result["exit_code"] = 124
                    break
                remaining = min(remaining, phase_remaining)
            if eof and process.poll() is not None:
                result["command_exit_code"] = process.returncode
                if process.returncode:
                    result["violations"].append(f"test command exited with {process.returncode}")
                    result["exit_code"] = process.returncode if process.returncode > 0 else 128 - process.returncode
                elif timing is not None and timing.execution_started is None:
                    result["violations"].append(f"{timing.settings['adapter']} timing did not reach test execution")
                    result["exit_code"] = 126
                break
            try:
                chunk = messages.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if isinstance(chunk, Exception):
                raise OSError(f"cannot read test output: {chunk}")
            if not chunk:
                eof = True
                continue
            result["output_bytes"] += len(chunk)
            tail.extend(chunk)
            del tail[:-TAIL_BYTES]
            pieces = chunk.split(b"\n")
            longest = current_line + len(pieces[0])
            if len(pieces) > 1:
                longest = max(longest, *(len(part) for part in pieces[1:]))
                completed_lines += len(pieces) - 1
                current_line = len(pieces[-1])
            else:
                current_line = longest
            result["max_line_bytes"] = max(result["max_line_bytes"], longest)
            result["output_lines"] = completed_lines + bool(current_line)
            for metric, limit in (("output_bytes", "output_bytes"), ("output_lines", "output_lines"), ("max_line_bytes", "line_bytes")):
                if result[metric] > limits[limit]:
                    result["violations"].append(f"{limit} budget exceeded")
            if result["violations"]:
                result["exit_code"] = 125
                break
    finally:
        finished = time.monotonic()
        stopped.set()
        try:
            if process is not None or backend.name:
                backend.cleanup(process)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
            if reader is not None:
                reader.join(timeout=0.2)
    result["seconds"] = round(time.monotonic() - started, 6)
    if timing is not None:
        result["timing"] = timing.metrics(started, finished)
    if not result["violations"]:
        failures, metrics = check_reports(root, checks, before, report_root=report_dir)
        result["checks"] = metrics
        if failures:
            result["violations"].extend(failures)
            result["exit_code"] = 126
    result["passed"] = not result["violations"]
    return result, bytes(tail)
