# local-llm

## Purpose

Run local LLMs on the Radeon 8060S (Strix Halo, `gfx1151`) with **llama.cpp**, for
use as a local tool from Claude Code (MCP) and as an offline Claude Code backend.
This directory covers the engine, the model files, benchmarking and a local
endpoint (llama-swap as a system service); the MCP tool is a later phase.

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

## Notes

- llama.cpp and every model file are **pinned** (commit; size + SHA-256 in
  `models.tsv`). Bump them deliberately and re-run the benchmarks.
- `ROCBLAS_USE_HIPBLASLT=1` routes rocBLAS matmuls through hipBLASLt's tuned
  gfx1151 kernels; set it for the ROCm build.
- llama.cpp v0.5.0 removed rocWMMA flash attention (`GGML_HIP_ROCWMMA_FATTN`,
  PR #26046); older Strix Halo guides that recommend it are out of date.
- Ollama (`/usr/local/bin/ollama`) is untouched for now; it's retired in phase 4.
  Most of its downloaded models use Ollama-specific tensor layouts that llama.cpp
  can't load (see docs/local-llm-models.md), so don't assume blobs are reusable.
