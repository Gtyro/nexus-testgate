from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path


MAX_REPORT_BYTES = 8 * 1024 * 1024


def project_path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path leaves project: {relative}")
    return path


def signature(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return None


def snapshot(root, checks):
    return {
        name: signature(project_path(root, check["path"]))
        for name, check in checks.items() if name in ("junit", "coverage")
    }


def _xml(root, check, previous):
    path = project_path(root, check["path"])
    current = signature(path)
    if current is None:
        raise ValueError(f"missing report: {check['path']}")
    if current == previous:
        raise ValueError(f"report was not regenerated: {check['path']}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"report exceeds 8 MiB: {check['path']}")
    return ET.fromstring(raw)


def check_reports(root, checks, before, report_root=None):
    report_root = root if report_root is None else report_root
    failures = []
    metrics = {}
    for name, check in checks.items():
        try:
            if name == "junit":
                document = _xml(report_root, check, before.get(name))
                if document.tag not in ("testsuite", "testsuites"):
                    raise ValueError("expected JUnit testsuite/testsuites root")
                cases = list(document.iter("testcase"))
                skipped = sum(case.find("skipped") is not None for case in cases)
                failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
                metrics[name] = {"tests": len(cases), "skipped": skipped, "failed": failed}
                if len(cases) < check.get("min_tests", 1):
                    failures.append("JUnit test count below minimum")
                if skipped > check.get("max_skipped", 0):
                    failures.append("JUnit skipped tests exceed budget")
                if failed or any(suite.find("error") is not None for suite in document.iter("testsuite")):
                    failures.append("JUnit reports failures/errors")
            elif name == "coverage":
                document = _xml(report_root, check, before.get(name))
                if document.tag != "coverage":
                    raise ValueError("expected Cobertura coverage root")
                metrics[name] = {}
                for metric in ("line", "branch"):
                    threshold = check.get(f"min_{metric}_rate")
                    if threshold is None:
                        continue
                    rate = float(document.attrib[f"{metric}-rate"])
                    if not math.isfinite(rate) or not 0 <= rate <= 1:
                        raise ValueError(f"invalid {metric} coverage rate")
                    metrics[name][f"{metric}_rate"] = rate
                    if rate < threshold:
                        failures.append(f"{metric} coverage below {threshold:g}")
            else:
                total = 0
                visited = set()
                for relative in check["paths"]:
                    start = project_path(root, relative)
                    if start.is_symlink():
                        raise ValueError("artifact symlinks are not supported")
                    if not start.exists():
                        raise ValueError(f"missing artifact: {relative}")
                    paths = [start] if start.is_file() else start.rglob("*")
                    for path in paths:
                        if path.is_symlink():
                            raise ValueError("artifact symlinks are not supported")
                        resolved = path.resolve()
                        if resolved in visited:
                            continue
                        visited.add(resolved)
                        if len(visited) > 100000:
                            raise ValueError("artifact scan exceeds 100000 entries")
                        if path.is_file():
                            total += path.stat().st_size
                            if total > check["max_bytes"]:
                                failures.append("artifact bytes exceed budget")
                                break
                    if total > check["max_bytes"]:
                        break
                metrics[name] = {"bytes": total}
        except (OSError, ValueError, KeyError, ET.ParseError) as exc:
            failures.append(f"{name}: {exc}")
    return failures, metrics
