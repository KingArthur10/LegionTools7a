---
name: new-tool
description: Scaffold a new tool in tools/<name>/ following this repo's conventions (README, entry point, tests, root README entry). Use when the user asks to create, start, or add a new tool.
---

# Scaffold a new tool

Arguments: the tool name and a one-line description of what it should do.
If either is missing, ask for it.

1. **Name**: normalise to `kebab-case`. Confirm `tools/<name>/` doesn't already exist.
2. **Branch**: if on `main`, create `feat/<name>` first.
3. **Investigate before coding**: check the relevant sysfs/procfs paths, drivers and
   commands actually exist on this machine (read-only commands only). Summarise
   findings and propose a short plan; wait for the user to agree.
4. **Language**: Python unless it is a thin wrapper around shell commands (then Bash).
5. **Create files**:
   - `tools/<name>/README.md` — sections: Purpose, Requirements (packages, root?,
     kernel/driver), Usage (with `--help` output), Examples, Safety notes (if it changes state).
   - Python: `tools/<name>/<name_snake>.py` with PEP 723 header, `argparse`,
     `--dry-run` if it modifies state, `main() -> int`. Make it executable
     (`chmod +x`) with `#!/usr/bin/env -S uv run --script` shebang.
   - Python: `tools/<name>/tests/test_<name_snake>.py` — test parsing/logic against
     fixture data (fake sysfs trees in `tmp_path`), never against real hardware writes.
   - Bash: `tools/<name>/<name>.sh` with `set -euo pipefail`, a `usage()` function.
6. **Register**: add a row to the Tools table in the root `README.md`.
7. **Verify**: run `make lint test`, fix issues, then run the tool's `--help`
   (and a read-only or `--dry-run` invocation) to show it works.
8. **Commit** with `feat(<name>): add <name> tool` — only after checks pass.
