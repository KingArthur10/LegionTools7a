# LegionTools7a

A collection of small, self-contained tools for a **Lenovo Legion 7a** laptop
with an **AMD Ryzen AI Max "Strix Halo"** APU, running **Fedora Linux**.

## Target hardware & platform

- CPU/GPU: AMD Strix Halo (Zen 5 CPU + RDNA 3.5 iGPU, `gfx1151`), unified memory
  shared between CPU and GPU (VRAM carve-out set in BIOS / via GTT).
- NPU: AMD XDNA 2 (`amdxdna` kernel driver).
- OS: Fedora 44 KDE (Wayland), kernel 7.2, SELinux **enforcing**, `dnf` package manager.
- Relevant interfaces: `/sys/class/drm`, `/sys/class/hwmon`, `/sys/devices/system/cpu`,
  `/sys/firmware/acpi/platform_profile`, `powerprofilesctl`, `amd-pstate`, ROCm (`rocm-smi`, `amd-smi`).
- Never assume a path exists — probe and fail with a clear message if hardware or a
  driver interface is missing.

**Full specs: [docs/system-specs.md](docs/system-specs.md)** — read it before working
on hardware-related tools. Key facts: Ryzen AI MAX+ 392 / Radeon 8060S, 64 GiB unified
memory, GPU at `/sys/class/drm/card1`, NPU at `/dev/accel/accel0`, power limits and
fan RPM via `lenovo-wmi-other`, two platform-profile handlers (`amd-pmf` and
`lenovo-wmi-gamezone`), `tuned-ppd` instead of `power-profiles-daemon`.
`nvme0n1` is a BitLocker-encrypted Windows drive — never write to it.

## Repository layout

```
tools/<tool-name>/      One directory per tool, fully self-contained
  README.md             What it does, requirements, usage, examples (required)
  <tool-name>[.py|.sh]  Entry point
  tests/                Tests (required for Python tools)
docs/                   Cross-cutting notes (hardware findings, decisions)
.claude/                Shared Claude Code settings and skills
```

Tool directory names are `kebab-case`. Each tool must be usable on its own; do not
create cross-tool imports. If shared code becomes genuinely necessary, discuss first.

## Languages & conventions

- **Python ≥ 3.12** is the default. Prefer the standard library; add third-party
  dependencies only when they clearly pay off, and declare them with PEP 723 inline
  script metadata so the tool runs with `uv run <script>`.
  - Type hints on all functions; `argparse` for CLIs; `pathlib` for paths.
  - Use `logging`, not bare `print`, for diagnostics; `print` is for actual output.
  - `main()` returns an exit code; guard with `if __name__ == "__main__":`.
- **Bash** is fine for thin wrappers around system commands. Start with
  `#!/usr/bin/env bash` and `set -euo pipefail`; quote all variables; must pass `shellcheck`.
- Every tool supports `--help`. Tools that change system state support `--dry-run`.
- Prefer reading state without root. When root is required, say so in `--help`/README
  and check `os.geteuid()` up front instead of failing halfway.

## Safety rules (this repo touches real hardware)

- **Never** run commands that write to `/sys`, `/proc`, firmware, BIOS/EC, MSRs, fan
  curves, power limits, or GRUB/kernel args without asking the user first — even if
  the tool being developed is meant to do exactly that. Use `--dry-run` to demonstrate.
- Never use `sudo` without explicit user approval for that specific command.
- Tools that modify system state must: show what will change, offer `--dry-run`,
  and where practical record the previous value so it can be restored.
- Don't disable SELinux or suggest doing so as a fix.

## Commands

```bash
make setup     # install pre-commit hooks (needs uv: `sudo dnf install uv`)
make lint      # ruff check + ruff format --check + shellcheck via pre-commit
make format    # auto-format Python and shell
make test      # run all tests with pytest
```

Run `make lint test` before every commit. CI runs the same checks.

## Git workflow

- `main` is always working. Never commit directly to `main`; never force-push `main`.
- Branch names: `feat/<tool>-<thing>`, `fix/<tool>-<thing>`, `docs/...`, `chore/...`.
- [Conventional Commits](https://www.conventionalcommits.org/): `feat(fan-monitor): add JSON output`.
  Scope is the tool name when the change is tool-specific.
- Small, focused commits; one logical change each. Don't mix refactors with features.
- Merge via pull request (`gh pr create`) so CI runs.
- Never commit secrets, machine serial numbers, or dumps containing personal data.

## Working style for Claude

- For anything beyond a trivial change, propose a short plan before writing code.
- Verify hardware facts (sysfs paths, driver names, tool flags) on this machine
  (`ls`, `cat`, `--help`) rather than relying on memory — Strix Halo support is new
  and changes between kernel versions.
- New tool? Use the `/new-tool` skill so structure stays consistent.
- When adding a tool, also add it to the table in the root `README.md`.
- Record notable hardware discoveries in `docs/hardware-notes.md`.
