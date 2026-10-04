# LegionTools7a

Tools I've made to help do things on my Lenovo Legion 7a (AMD Ryzen AI Max "Strix Halo") running Fedora.

## Tools

| Tool | Description |
| ---- | ----------- |
| [face-unlock](tools/face-unlock/) | Windows Hello-style face unlock (Howdy + IR camera) for sudo, lock screen and polkit |

Each tool lives in `tools/<name>/` with its own README.

## Development

Requires [`uv`](https://docs.astral.sh/uv/) (`sudo dnf install uv`).

```bash
make setup   # install dev deps + pre-commit hooks
make lint    # ruff, shellcheck, gitleaks, misc checks
make test    # pytest
```

See [CLAUDE.md](CLAUDE.md) for conventions, the git workflow, and hardware safety rules.
