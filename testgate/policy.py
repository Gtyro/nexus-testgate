from __future__ import annotations

import hashlib
import json
import math
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


MAX_POLICY_BYTES = 262144


class PolicyError(ValueError):
    pass


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise PolicyError("policy redirects must remain on HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _keys(value, allowed, label):
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must be an object")
    unknown = value.keys() - set(allowed)
    if unknown:
        raise PolicyError(f"unknown {label} fields: {', '.join(sorted(unknown))}")


def _number(value, label, *, minimum=0, integer=False):
    types = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, types):
        raise PolicyError(f"{label} must be {'an integer' if integer else 'a number'}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < minimum:
        raise PolicyError(f"{label} must be finite and >= {minimum}")


def relative_path(value):
    if (
        not isinstance(value, str) or not value or "\\" in value or ":" in value
        or Path(value).is_absolute() or ".." in Path(value).parts
    ):
        raise PolicyError("report/artifact paths must be relative paths within the project")


def validate(policy):
    _keys(policy, ("version", "name", "command", "allow_command_override", "limits", "backend", "checks"), "policy")
    if type(policy.get("version")) is not int or policy["version"] != 1:
        raise PolicyError("unsupported policy version; expected 1")
    if not isinstance(policy.get("name"), str) or not 1 <= len(policy["name"]) <= 80 or not policy["name"].isprintable():
        raise PolicyError("policy name must contain 1-80 printable characters")
    command = policy.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x or "\0" in x for x in command):
        raise PolicyError("command must be a nonempty list of argument strings")
    if type(policy.get("allow_command_override", False)) is not bool:
        raise PolicyError("allow_command_override must be boolean")
    limits = policy.get("limits")
    _keys(limits, ("seconds", "output_bytes", "output_lines", "line_bytes"), "limits")
    for key in ("seconds", "output_bytes", "output_lines", "line_bytes"):
        if key not in limits:
            raise PolicyError(f"limits.{key} is required")
        _number(limits[key], f"limits.{key}", minimum=0, integer=key != "seconds")
    if limits["seconds"] == 0:
        raise PolicyError("limits.seconds must be greater than zero")
    backend = policy.get("backend", {"type": "native"})
    _keys(backend, ("type", "image", "memory_bytes", "cpus", "pids", "tmpfs_bytes", "network"), "backend")
    if backend.get("type") == "native":
        _keys(backend, ("type",), "native backend")
    elif backend.get("type") == "docker":
        _keys(backend, ("type", "image", "memory_bytes", "cpus", "pids", "tmpfs_bytes", "network"), "docker backend")
        if not isinstance(backend.get("image"), str) or not backend["image"] or backend["image"].startswith("-"):
            raise PolicyError("docker backend requires an image")
        for key in ("memory_bytes", "pids", "tmpfs_bytes", "cpus"):
            if key in backend:
                _number(backend[key], f"backend.{key}", minimum=1 if key != "cpus" else 0.001, integer=key != "cpus")
        if backend.get("network", "none") not in ("none", "bridge"):
            raise PolicyError("docker network must be none or bridge")
    else:
        raise PolicyError("backend.type must be native or docker")
    checks = policy.get("checks", {})
    _keys(checks, ("junit", "coverage", "artifacts"), "checks")
    for name, check in checks.items():
        fields = {
            "junit": ("path", "min_tests", "max_skipped"),
            "coverage": ("path", "min_line_rate", "min_branch_rate"),
            "artifacts": ("paths", "max_bytes"),
        }[name]
        _keys(check, fields, f"checks.{name}")
        if name == "artifacts":
            paths = check.get("paths")
            if not isinstance(paths, list) or not paths:
                raise PolicyError("artifacts.paths must be a nonempty list")
            for path in paths:
                relative_path(path)
            _number(check.get("max_bytes"), "artifacts.max_bytes", integer=True)
        else:
            relative_path(check.get("path"))
            for key in fields[1:]:
                if key in check:
                    _number(check[key], f"{name}.{key}", integer=name == "junit")
                    if name == "coverage" and check[key] > 1:
                        raise PolicyError(f"coverage.{key} must be between 0 and 1")
            if name == "coverage" and not any(key in check for key in fields[1:]):
                raise PolicyError("coverage requires at least one threshold")
    return policy


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"duplicate policy field: {key}")
        result[key] = value
    return result


def load(source=None, expected_sha256=None):
    if source is None:
        raw = files("testgate").joinpath("default-policy.json").read_bytes()
    elif source.startswith("https://"):
        with build_opener(HTTPSRedirects()).open(source, timeout=10) as response:
            raw = response.read(MAX_POLICY_BYTES + 1)
    elif "://" in source:
        raise PolicyError("remote policies require HTTPS")
    else:
        with Path(source).open("rb") as stream:
            raw = stream.read(MAX_POLICY_BYTES + 1)
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyError("policy exceeds 256 KiB")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest != expected_sha256.lower():
        raise PolicyError("policy SHA-256 does not match")
    try:
        policy = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise PolicyError(f"invalid policy JSON: {exc}") from exc
    return validate(policy), digest
