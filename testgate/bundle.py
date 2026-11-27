"""Build the same self-contained runtime from source, wheels, or a zipapp."""
from __future__ import annotations

import io
import zipfile
from importlib.resources import files


def runtime_archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        def add(name, content):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, content)

        def walk(directory, prefix):
            for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
                if item.name == "__pycache__" or item.name.endswith((".pyc", ".pyo")):
                    continue
                name = prefix + "/" + item.name
                if item.is_dir():
                    walk(item, name)
                else:
                    add(name, item.read_bytes())

        walk(files("testgate"), "testgate")
        add("__main__.py", b"from testgate.cli import main\nraise SystemExit(main())\n")
    return output.getvalue()
