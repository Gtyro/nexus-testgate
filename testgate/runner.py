from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

from .backends import Backend
from . import __version__
from .reports import check_reports, snapshot


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


def run(policy, digest, root, command):
    checks = policy.get("checks", {})
    before = snapshot(root, checks)
    backend = Backend(policy, root)
    invocation = backend.prepare(command)
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
        )
        reader = threading.Thread(
            target=_read_output,
            args=(process.stdout, messages, stopped, min(65536, limits["output_bytes"] + 1)),
            daemon=True,
        )
        reader.start()
        while True:
            remaining = limits["seconds"] - (time.monotonic() - started)
            if remaining <= 0:
                result["violations"].append("time budget exceeded")
                result["exit_code"] = 124
                break
            if eof and process.poll() is not None:
                result["command_exit_code"] = process.returncode
                if process.returncode:
                    result["violations"].append(f"test command exited with {process.returncode}")
                    result["exit_code"] = process.returncode if process.returncode > 0 else 128 - process.returncode
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
    if not result["violations"]:
        failures, metrics = check_reports(root, checks, before)
        result["checks"] = metrics
        if failures:
            result["violations"].extend(failures)
            result["exit_code"] = 126
    result["passed"] = not result["violations"]
    return result, bytes(tail)
