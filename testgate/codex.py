"""Own the complete Codex integration without modifying target repositories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path
from urllib.request import Request, build_opener

from . import __version__
from .bundle import runtime_archive
from .locations import outside_project, temporary_parent
from .policy import HTTPSRedirects


OWNER = "nexus-testgate"
MANIFEST = ".testgate-install.json"
RESOURCES = ("SKILL.md", "scripts/run.py")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _destination(home):
    if (home / "skills" / OWNER).is_symlink():
        raise ValueError("refusing to manage a symlinked skill directory")
    return outside_project(home / "skills" / OWNER)


def _launcher_path(bin_dir):
    name = "testgate.cmd" if os.name == "nt" else "testgate"
    if (bin_dir / name).is_symlink():
        raise ValueError("refusing to replace a symlinked launcher")
    return outside_project(bin_dir / name)


def _launcher_content(destination):
    helper = destination / "scripts" / "run.py"
    if os.name == "nt":
        if any(character in str(helper) for character in ('%', '\n', '\r', '"')):
            raise ValueError("Windows launcher path contains unsupported characters")
        return f'@echo off\r\npython "{helper}" %*\r\nexit /b %errorlevel%\r\n'.encode()
    return f"#!/bin/sh\nexec python {shlex.quote(str(helper))} \"$@\"\n".encode()


def _audit(destination, bin_dir=None):
    if destination.is_symlink():
        raise ValueError("refusing to manage a symlinked skill directory")
    try:
        record = json.loads((destination / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("existing skill is not a Testgate-managed installation") from exc
    expected = {*RESOURCES, "runtime/testgate.pyz"}
    if not isinstance(record, dict) or not isinstance(record.get("files"), dict) or not isinstance(record.get("launcher"), dict):
        raise ValueError("invalid Testgate installation manifest")
    if record.get("owner") != OWNER or record.get("format") != 1 or set(record["files"]) != expected or not re.fullmatch(r"\d+\.\d+\.\d+", str(record.get("version", ""))):
        raise ValueError("invalid Testgate installation manifest")
    actual = set()
    for item in destination.rglob("*"):
        if item.is_symlink():
            raise ValueError("installed skill contains a symlink; refusing to overwrite or remove it")
        if item.is_file():
            actual.add(item.relative_to(destination).as_posix())
        elif item.is_dir() and item.relative_to(destination).as_posix() not in ("scripts", "runtime"):
            raise ValueError("installed skill has an unmanaged directory")
    if actual != expected | {MANIFEST}:
        raise ValueError("installed skill has extra or missing files; preserve local changes before updating")
    for relative, digest in record["files"].items():
        if _digest((destination / relative).read_bytes()) != digest:
            raise ValueError(f"installed file was modified: {relative}")
    raw_launcher = Path(record["launcher"]["path"])
    if raw_launcher.is_symlink():
        raise ValueError("installed launcher was replaced by a symlink")
    launcher = outside_project(raw_launcher)
    if bin_dir is not None and launcher != _launcher_path(bin_dir):
        raise ValueError("launcher directory differs from the managed installation")
    if launcher.is_symlink() or (launcher.exists() and _digest(launcher.read_bytes()) != record["launcher"]["sha256"]):
        raise ValueError("installed launcher was modified; refusing to overwrite or remove it")
    return record, launcher


def install_local(home, bin_dir=None, *, update=False):
    raw_destination = home / "skills" / OWNER
    if raw_destination.is_symlink():
        raise ValueError("refusing to replace a symlinked skill directory")
    destination = _destination(home)
    if destination.exists():
        record, launcher = _audit(destination, bin_dir)
        if not update:
            if record["version"] == __version__ and launcher.exists():
                print(f"Already installed: {destination} ({__version__})")
                return
            raise ValueError("Testgate is already installed; use 'codex update'")
    else:
        if update:
            raise ValueError("Testgate is not installed; use 'codex install'")
        launcher = _launcher_path(bin_dir or Path.home() / ".local" / "bin")
        if launcher.exists() or launcher.is_symlink():
            raise ValueError(f"launcher already exists and is not owned by this installation: {launcher}")
    resource = files("testgate").joinpath("integrations", "codex", OWNER)
    contents = {relative: resource.joinpath(*relative.split("/")).read_bytes() for relative in RESOURCES}
    contents["runtime/testgate.pyz"] = runtime_archive()
    launcher_bytes = _launcher_content(destination)
    manifest = {
        "format": 1, "owner": OWNER, "version": __version__,
        "files": {name: _digest(data) for name, data in contents.items()},
        "launcher": {"path": str(launcher), "sha256": _digest(launcher_bytes)},
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".testgate-install-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        staged = temporary / "skill"
        staged.mkdir()
        for relative, data in contents.items():
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (staged / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        backup = temporary / "previous"
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
            with tempfile.NamedTemporaryFile(prefix=".testgate-launcher-", dir=launcher.parent, delete=False) as stream:
                pending = Path(stream.name)
                stream.write(launcher_bytes)
            try:
                pending.chmod(0o755)
                os.replace(pending, launcher)
            finally:
                pending.unlink(missing_ok=True)
        except BaseException:
            if destination.exists():
                shutil.rmtree(destination)
            if backup.exists():
                os.replace(backup, destination)
            raise
    print(f"{'Updated' if update else 'Installed'} Codex skill {__version__}: {destination}")
    print(f"Launcher: {launcher}")
    if launcher.parent not in (Path(entry).resolve() for entry in os.get_exec_path() if entry):
        print("User bin is not on PATH; the skill can use its bundled scripts/run.py directly.")
    print("The skill will be available on the next turn. No project configuration was changed.")


def uninstall(home, bin_dir=None):
    destination = _destination(home)
    if not destination.exists():
        print("Testgate Codex integration is not installed")
        return
    _, launcher = _audit(destination, bin_dir)
    launcher.unlink(missing_ok=True)
    shutil.rmtree(destination)
    print(f"Uninstalled Codex integration: {destination}")


def _download(url, maximum):
    request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": OWNER})
    with build_opener(HTTPSRedirects()).open(request, timeout=20) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("release download exceeds size limit")
    return raw


def update_latest(home, bin_dir=None):
    record, launcher = _audit(_destination(home), bin_dir)
    release = json.loads(_download("https://api.github.com/repos/Gtyro/nexus-testgate/releases/latest", 262144))
    version = release["tag_name"].removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("latest release does not have a supported version")
    if tuple(map(int, version.split("."))) <= tuple(map(int, record["version"].split("."))):
        print(f"Already up to date: {record['version']}")
        return
    assets = {asset["name"]: asset["id"] for asset in release["assets"]}
    base = "https://api.github.com/repos/Gtyro/nexus-testgate/releases/assets/"
    checksums = _download(base + str(int(assets["SHA256SUMS"])), 65536).decode()
    expected = None
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "testgate.pyz" and re.fullmatch(r"[a-f0-9]{64}", parts[0]):
            if expected is not None:
                raise ValueError("release checksum is ambiguous")
            expected = parts[0]
    payload = _download(base + str(int(assets["testgate.pyz"])), 8388608)
    if expected is None or _digest(payload) != expected:
        raise ValueError("release SHA-256 verification failed")
    with tempfile.TemporaryDirectory(prefix="testgate-update-", dir=temporary_parent()) as temporary:
        runtime = Path(temporary) / "testgate.pyz"
        runtime.write_bytes(payload)
        with zipfile.ZipFile(runtime) as archive:
            source = archive.read("testgate/__init__.py").decode()
        if f'__version__ = "{version}"' not in source:
            raise ValueError("release version does not match its runtime")
        completed = subprocess.run([
            sys.executable, str(runtime), "codex", "update", "--local", "--codex-home", str(home),
            "--bin-dir", str(launcher.parent),
        ], check=False)
        if completed.returncode:
            raise ValueError(f"release installer failed with {completed.returncode}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage Testgate's self-contained user-level Codex integration.")
    parser.add_argument("action", choices=("install", "update", "uninstall", "status"))
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--bin-dir", type=Path, help="launcher directory; defaults to ~/.local/bin")
    parser.add_argument("--local", action="store_true", help="update from the running version instead of fetching the latest release")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            install_local(args.codex_home, args.bin_dir)
        elif args.action == "update":
            if args.local:
                install_local(args.codex_home, args.bin_dir, update=True)
            else:
                update_latest(args.codex_home, args.bin_dir)
        elif args.action == "uninstall":
            uninstall(args.codex_home, args.bin_dir)
        else:
            destination = _destination(args.codex_home)
            if destination.exists():
                record, launcher = _audit(destination, args.bin_dir)
                print(f"Installed {record['version']}: {destination}\nLauncher: {launcher}")
            else:
                print("Testgate Codex integration is not installed")
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
