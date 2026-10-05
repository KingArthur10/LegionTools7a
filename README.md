# LegionTools7a

Tools I've made to help do things on my Lenovo Legion 7a (AMD Ryzen AI Max "Strix Halo") running Fedora.

## Tools

| Tool | Description |
| ---- | ----------- |
| [battery-threshold](tools/battery-threshold/) | Show/set battery charge mode and hold a custom 80–100% charge limit |

Each tool lives in `tools/<name>/` with its own README.

## Development

Requires [`uv`](https://docs.astral.sh/uv/) (`sudo dnf install uv`).

```bash
make setup   # install dev deps + pre-commit hooks
make lint    # ruff, shellcheck, gitleaks, misc checks
make test    # pytest
```

See [CLAUDE.md](CLAUDE.md) for conventions, the git workflow, and hardware safety rules.
