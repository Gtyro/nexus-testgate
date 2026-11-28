"""Executed by the target's Python; pytest remains a target dependency."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main():
    signal = Path(sys.argv[1])
    arguments = sys.argv[2:]
    # Match `python -m pytest`: the project's cwd comes first on sys.path.
    sys.path[0] = os.getcwd()
    sys.argv = ["pytest", *arguments]
    import pytest

    class TimingPlugin:
        def pytest_configure(self, config):
            if getattr(config.option, "numprocesses", None) not in (None, 0) or hasattr(config, "workerinput"):
                raise pytest.UsageError("Testgate phase timing does not support pytest-xdist workers")

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtestloop(self, session):
            options = session.config.option
            collection_aborted = session.testsfailed and not options.continue_on_collection_errors
            if not options.collectonly and not collection_aborted:
                with signal.open("xb") as stream:
                    stream.write(b"execution\n")

    return pytest.main(arguments, plugins=[TimingPlugin()])


if __name__ == "__main__":
    raise SystemExit(main())
