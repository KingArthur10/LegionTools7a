# local-llm

## Purpose

Run local LLMs on the Radeon 8060S (Strix Halo, `gfx1151`) with **llama.cpp**, for
use as a local tool from Claude Code (MCP) and as an offline Claude Code backend.
This directory covers the engine, the model files, benchmarking and a local
endpoint (llama-swap as a system service), an MCP server that lets Claude Code
hand work to the local model (`llm_mcp.py`), and `claude-local`, which runs
Claude Code entirely on a local model.

Why llama.cpp rather than Ollama: both of our workloads are dominated by long
prompts (Claude Code's system prompt + tool schemas, or large files/logs for the
MCP tool), and prompt processing is where a tuned llama.cpp build beats Ollama on
this chip. `llama-server` also speaks the Anthropic Messages API (`/v1/messages`)
natively. See [docs/local-llm-models.md](../../docs/local-llm-models.md) for the
model shortlist.

## Requirements

```bash
sudo dnf install rocm-hip-devel hipblas-devel rocblas-devel hipblaslt-devel rocm-cmake \
  rocminfo amdsmi glslc glslang spirv-headers-devel libshaderc-devel libcurl-devel ccache
sudo install -d -o "$USER" -g "$USER" -m 755 /srv/llm /srv/llm/models
```

Fedora's ROCm 7.1.1 already ships `gfx1151` kernels for rocBLAS and hipBLASLt.

## Usage

```bash
./build-llama-cpp.sh            # both backends (rocm | vulkan | all)
./download-models.sh            # tier-1 models from models.tsv (~97 GB)
./download-models.sh all        # every tier
./llm_bench.py speed            # llama-bench every model on both backends
./llm_eval.py run               # graded coding tasks (results: docs/local-llm-benchmarks.md)
```

| Output | Location |
| ------ | -------- |
| ROCm build | `~/.cache/legiontools7a/llama.cpp/build-rocm/bin/` |
| Vulkan build | `~/.cache/legiontools7a/llama.cpp/build-vulkan/bin/` |
| Models | `/srv/llm/models/<hf-repo>/<file>.gguf` |

Override with `LLAMA_CPP_DIR` and `LLM_MODELS_DIR`.

### Serving

`install-server.sh` installs [llama-swap](https://github.com/mostlygeek/llama-swap)
as a systemd service: one endpoint on `http://127.0.0.1:8080` that starts the
requested model's `llama-server` (Vulkan build) on demand. **`install` and
`uninstall` need root**; `prepare` must not be run as root.

```bash
./build-llama-cpp.sh vulkan
./install-server.sh prepare          # fetch llama-swap, verify SHA-256, validate config
sudo ./install-server.sh install     # install + enable + start the service
sudo ./install-server.sh uninstall   # remove service and binaries (keeps models)
```

What `install` changes:

| Path | Contents |
| ---- | -------- |
| `/etc/sysusers.d/llm.conf` | system user `llm` (groups `render`, `video`) |
| `/opt/llama.cpp/` | llama.cpp Vulkan install (replaced on every install) |
| `/opt/llama-swap/llama-swap` | llama-swap binary (pinned version + checksum) |
| `/etc/llama-swap/config.yaml` | copy of `llama-swap.yaml`; a differing old one is backed up as `config.yaml.bak.<epoch>` |
| `/etc/llama-swap/templates/` | patched chat templates from `templates/` |
| `/etc/systemd/system/llama-swap.service` | hardened unit, loopback only |

Models in `llama-swap.yaml` (numbers from docs/local-llm-benchmarks.md):

| Name | Model | Notes |
| ---- | ----- | ----- |
| `qwen3-coder` | Qwen3-Coder 30B-A3B Q4_K_M | default; fast, non-thinking; 64K context |
| `agentworld` | Qwen-AgentWorld 35B-A3B UD-Q5_K_M | thinking; long agentic sessions; 64K context |
| `embed` | Qwen3-Embedding 0.6B Q8_0 | embeddings; stays loaded |

The two chat models swap (one loaded at a time, unloaded after 30 min idle);
`embed` stays resident alongside. To change models, edit `llama-swap.yaml` and
re-run `prepare` + `install`.

## Examples

```bash
B=~/.cache/legiontools7a/llama.cpp
$B/build-vulkan/bin/llama-cli --list-devices
ROCBLAS_USE_HIPBLASLT=1 $B/build-rocm/bin/llama-bench \
  -m /srv/llm/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf \
  -fa 1 -p 512,4096 -n 128
```

### Using the endpoint

```bash
curl -s localhost:8080/v1/models
curl -s localhost:8080/v1/messages -H 'content-type: application/json' -d '{
  "model": "qwen3-coder", "max_tokens": 200,
  "messages": [{"role": "user", "content": "Write a bash one-liner to count lines in *.py"}]}'
curl -s localhost:8080/v1/embeddings -H 'content-type: application/json' \
  -d '{"model": "embed", "input": "hello"}'
journalctl -u llama-swap -f          # load/unload and server logs
```

The first request to a model waits for it to load (up to `healthCheckTimeout`).

### Local tools inside Claude Code (MCP)

`llm_mcp.py` is an MCP server (standard library only) that Claude Code calls to
hand work to the local model. Claude passes **paths**, never file contents, and
gets back a short result.

| Tool | Does |
| ---- | ---- |
| `local_task(task, files?, test_command?, protect?)` | Runs a `claude-local` worker in the project to implement a well-specified change, then runs `test_command` itself and reports the result, the changed files (+/− lines) and the worker's summary. Saves Claude the output tokens of writing the code |
| `local_ask(question, paths)` | Answers a question about files, directories or globs with `path:line` citations. Input larger than the 64K context is split; each part is read for notes and the notes are combined |
| `local_search(query, path?)` | Semantic search with `embed`. Returns `path:start-end` ranges plus a one-line preview. The index is cached in `~/.cache/legiontools7a/llm-mcp/` and only changed files are re-embedded. One call embeds at most ~1,200 new chunks (~1 min); a bigger first index says it is incomplete and continues on the next call. Files over 300 KB are left out |

```bash
claude mcp add -s user local-llm -- "$PWD/llm_mcp.py"    # once; available in every project
./llm_mcp.py task "Create ram_store.py with class RamChatStore ..." \
    --file ram_store.py --test "uv run pytest -q test_ram_store.py"   # local_task from a shell
./llm_mcp.py ask "where is the context size read?" claude-local.sh   # same as the tool, from a shell
./llm_mcp.py search "SHA-256 check of model files"
```

- **What it reads:** files under the project (Claude Code's working directory, or
  `--root`) and the temp directory (`--allow DIR` adds more). Directories follow
  `.gitignore`. Secret-looking names (`.env*`, keys, `*credentials*`, `*secret*`,
  `~/.ssh`) and binaries are skipped, and the answer lists what was skipped.
- **Limits:** 100K tokens per call, counted with the model's tokenizer. Reading
  runs at ~370 tokens/s, so that is 4-5 minutes; narrow the paths beyond that.
  llama-swap serves one request at a time, so parallel calls queue. After 30 minutes idle, the first call waits ~10 s for the model
  to load.
- **Trust:** answers come from a 30B model. The server's instructions tell Claude
  to treat them as leads and to read cited lines itself before editing.
- **`local_task` workflow:** Claude writes the spec and the tests, the worker
  implements, and Claude reviews `git diff`. Rules the server enforces:
  - git repositories only (with a commit), so changes can be reviewed and undone;
  - the worker may edit files **inside the project only** and run only
    `test_command`; it gets no MCP servers (no recursion). It runs in
    `acceptEdits` mode with no unscoped `Edit`/`Write` allow rules: a rule without a
    path allows writing anywhere, and an early worker did write a copy of its
    output into another repo. Refused actions are listed in the report;
  - existing test files (`test_*`, `*_test.*`, `*.spec.*`, `tests/`) and any
    `protect` paths are denied to the worker, and if one changes anyway the server
    restores it and puts a warning at the top of the report;
  - new files outside `files` are flagged `NOT REQUESTED`;
  - one task at a time, 15 min by default (max 28: Claude Code drops a silent
    stdio call after 30), with progress messages every 15 s.
  First real run (in-memory chat store, 5 spec tests): 2.6 min, tests passed.
  Before protection existed, a worker facing a broken test command rewrote the
  spec's tests to make them pass, which is why they're protected now.
- **Measured (reading tools):** in A/B runs `local_ask` saved Opus no tokens: Opus already greps and reads
  only what it needs, and the local model was slower and missed a root cause in
  a long log. It is installed as an optional tool, not recommended to Claude. See
  [docs/local-llm-benchmarks.md](../../docs/local-llm-benchmarks.md#hybrid-reading-via-mcp-2026-10-18).
- **Skip it under `claude-local`:** there the "local tool" is the same model
  Claude Code already runs on.

### Claude Code on a local model

`claude-local.sh` starts Claude Code with every request going to llama-swap
instead of Anthropic. Your normal `claude` setup and login are untouched, so
switching back is just running `claude`.

```bash
ln -s "$PWD/claude-local.sh" ~/.local/bin/claude-local   # once

claude-local                         # qwen3-coder (default)
claude-local --model agentworld      # thinking model, better at long sessions
claude-local -p "explain main.py"    # any other claude arguments pass through
claude-local --list                  # models on the endpoint
eval "$(claude-local --env)"         # export the settings into this shell instead
```

It checks the endpoint is up, loads the model, and sets:

| Setting | Why |
| ------- | --- |
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` | point at llama-swap (the token is a placeholder); `ANTHROPIC_API_KEY` is unset |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` | all the same model: llama-swap keeps one chat model loaded, so mixing would reload it on every subagent call |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | the server's real context (read from `/upstream/<model>/props`); otherwise Claude Code assumes 200K and never compacts in time |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192` | the default 32K output reservation is half of a 64K window |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_COST_WARNINGS` | no telemetry; the cost shown is meaningless for a local model |
| `--permission-mode default` (unless you pass your own) | auto mode's safety classifier would run on the local model: each check took over Claude Code's 60 s limit and was denied, and a small local model shouldn't be trusted to judge risk anyway |

`--env` prints only the environment variables, so a `claude` started that way
keeps your configured permission mode. Add `--permission-mode default` yourself.

Expected noise: an `unrecognized_model` line and a note that claude.ai connectors
are disabled (they need your claude.ai login, which isn't used here). `/model`
works, but switching to the other chat model reloads it (~10 s).

Tested on Claude Code 2.1.292: both models complete an edit → run tests → fix loop
(10/10 tests). Claude Code's own system prompt and tools take ~10K tokens of the
64K context. Prompt caching works: llama-server reuses the shared prefix.

### Working offline

`claude-local` runs Claude Code with no internet connection. Tested with
networking off (`nmcli networking off`) on Claude Code 2.1.292: start-up, a
multi-step write → test → fix session and tool approvals all worked. A debug log
of a run with no network showed connections to `127.0.0.1` only: no Anthropic
API, telemetry or update checks.

Checklist:

- `systemctl is-active llama-swap` reports `active`. The service starts at boot.
- The models are in `/srv/llm/models` (`./download-models.sh` needs the network).
- Start with `claude-local`, not `claude`: plain `claude` still talks to Anthropic.
- The first reply takes ~30–40 s while the model reads Claude Code's system
  prompt. Later turns reuse it from the cache.
- Not available offline: claude.ai connectors, web search/fetch, and any MCP
  server that calls an online service.
- `llm_bench.py` and `llm_eval.py` have no dependencies, so `uv run` works offline.

## Notes

- **`templates/agentworld.jinja`**: Claude Code sends a `system` message in the
  middle of the conversation (environment info after the first user turn).
  Qwen-AgentWorld's embedded template rejects that with HTTP 500 ("System message
  must be at the beginning"), and `CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM=0`
  doesn't stop it. The patched copy renders such messages in place. Qwen3-Coder's
  template already accepts them.
- llama.cpp and every model file are **pinned** (commit; size + SHA-256 in
  `models.tsv`). Bump them deliberately and re-run the benchmarks.
- `ROCBLAS_USE_HIPBLASLT=1` routes rocBLAS matmuls through hipBLASLt's tuned
  gfx1151 kernels; set it for the ROCm build.
- llama.cpp v0.5.0 removed rocWMMA flash attention (`GGML_HIP_ROCWMMA_FATTN`,
  PR #26046); older Strix Halo guides that recommend it are out of date.
- Ollama (`/usr/local/bin/ollama`) is untouched for now; it's retired in phase 4.
  Most of its downloaded models use Ollama-specific tensor layouts that llama.cpp
  can't load (see docs/local-llm-models.md), so don't assume blobs are reusable.
