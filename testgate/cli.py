from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from urllib.error import URLError

from . import __version__
from .policy import PolicyError, load
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enforce shared test budgets across projects.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--policy", default=os.environ.get("TESTGATE_POLICY"), help="local JSON file or HTTPS URL; default: built-in policy")
    parser.add_argument("--policy-sha256", default=os.environ.get("TESTGATE_POLICY_SHA256"))
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--json", type=Path, help="write a machine-readable result")
    parser.add_argument("--show-policy", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    def terminate(signum, frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        policy, digest = load(args.policy, args.policy_sha256)
        if args.show_policy:
            print(json.dumps({"sha256": digest, "policy": policy}, ensure_ascii=False, indent=2))
            return 0
        if command and not policy.get("allow_command_override", False):
            raise PolicyError("this policy does not permit command overrides")
        if not args.cwd.is_dir():
            raise ValueError("--cwd must be an existing project directory")
        result, tail = run(policy, digest, args.cwd.resolve(), command or policy["command"])
        print(
            f"{'PASS' if result['passed'] else 'FAIL'}: {result['seconds']:.3f}s; "
            f"{result['output_bytes']} output bytes; {result['output_lines']} lines; "
            f"policy={result['policy']} sha256={digest[:12]}"
        )
        for violation in result["violations"]:
            print(violation)
        if not result["passed"] and tail:
            print("--- output tail (at most 4096 bytes; full log is not retained) ---")
            print(tail.decode("utf-8", errors="replace"), end="")
            if not tail.endswith(b"\n"):
                print()
        if args.json:
            args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result["exit_code"]
    except (OSError, ValueError, RuntimeError, URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    raise SystemExit(main())
