# local-llm

## Purpose

Run local LLMs on the Radeon 8060S (Strix Halo, `gfx1151`) with **llama.cpp**, for
use as a local tool from Claude Code (MCP) and as an offline Claude Code backend.
This directory covers the engine and the model files; benchmarking, serving and
the MCP tool are later phases.

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
```

| Output | Location |
| ------ | -------- |
| ROCm build | `~/.cache/legiontools7a/llama.cpp/build-rocm/bin/` |
| Vulkan build | `~/.cache/legiontools7a/llama.cpp/build-vulkan/bin/` |
| Models | `/srv/llm/models/<hf-repo>/<file>.gguf` |

Override with `LLAMA_CPP_DIR` and `LLM_MODELS_DIR`.

## Examples

```bash
B=~/.cache/legiontools7a/llama.cpp
$B/build-vulkan/bin/llama-cli --list-devices
ROCBLAS_USE_HIPBLASLT=1 $B/build-rocm/bin/llama-bench \
  -m /srv/llm/models/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf \
  -fa 1 -p 512,4096 -n 128
```

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
