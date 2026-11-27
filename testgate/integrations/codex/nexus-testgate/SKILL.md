---
name: nexus-testgate
description: Run project tests with enforced time and output budgets using the installed Testgate runtime. Use when executing a test suite or checking test runtime/output costs; keep all Testgate integration, configuration and reports outside the target repository.
---

# Nexus Testgate

Run the project's existing test command through Testgate. This skill includes its own runtime; it does not depend on a source checkout, another skill, or changes to global instructions.

Use `testgate -- <test command>` when the installed launcher is on PATH. Otherwise run `python <this skill directory>/scripts/run.py -- <test command>`. Resolve that path from the location of this SKILL.md, not from the project directory.

Examples:

```bash
testgate -- make test
testgate -- python -m pytest -q --tb=short
```

Choose the project's authoritative full test command when checking its full-suite budget. Preserve requested test scope. A budget failure is a failed check; report the reason without relaxing limits, skipping tests, or bypassing the wrapper to obtain a passing result.

Keep tool files, policies and reports outside the target project, including ignored directories and Git configuration. Do not add integration instructions, dependencies, wrappers or targets to that project. Testgate creates an external temporary report directory; commands can reference it using `{testgate_reports}` in arguments or `TESTGATE_REPORT_DIR` in their environment. Use an external `--report-dir` or `--json` path only when results must be retained.

The default native backend supports Linux/macOS. On Windows use an existing WSL environment or a centrally configured Docker policy; do not install project dependencies or a container engine merely to make this skill work. An unsupported environment or unavailable dependency is a reported blocker.

Successful runs print a summary. Failed runs print the reason and a bounded output tail. This skill routes execution through a budget checker; it does not intercept commands issued outside it.
