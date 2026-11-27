"""Keep Testgate-owned files outside project and Git directories."""
from __future__ import annotations

import tempfile
from pathlib import Path


def _git_directory(path):
    return (path / "HEAD").is_file() and ((path / "objects").is_dir() or (path / "commondir").is_file())


def _worktree(path):
    marker = path / ".git"
    if marker.is_dir():
        return _git_directory(marker)
    if marker.is_file():
        with marker.open(encoding="utf-8", errors="replace") as stream:
            pointer = stream.readline(4096).strip()
        if pointer.startswith("gitdir:"):
            return _git_directory((path / pointer[7:].strip()).resolve())
    return _git_directory(path)


def outside_project(path: Path, project: Path | None = None) -> Path:
    resolved = path.expanduser().resolve()
    if project is not None and resolved.is_relative_to(project.resolve()):
        raise ValueError(f"Testgate files must be outside the project: {path}")
    for parent in (resolved, *resolved.parents):
        if _worktree(parent):
            raise ValueError(f"Testgate files must be outside Git repositories: {path}")
    return resolved


def temporary_parent(project: Path | None = None) -> Path:
    try:
        return outside_project(Path(tempfile.gettempdir()), project)
    except ValueError:
        path = outside_project(Path.home() / ".cache" / "nexus-testgate", project)
        path.mkdir(parents=True, exist_ok=True)
        return path
