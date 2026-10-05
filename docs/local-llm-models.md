# Local LLM model shortlist

Phase 2 research (2026-10-04). Candidates to benchmark and evaluate in phase 3;
nothing here is a final choice. Sizes are GGUF file sizes from Hugging Face.

## Constraints on this machine

- **GPU memory budget: 52 GiB (≈55.8 GB)** of GTT out of 64 GiB unified RAM.
  Weights + KV cache + compute buffers must fit, and the desktop still needs RAM.
  Practical ceiling for one model's weights: **~40 GB** with 64K context.
- **Memory bandwidth (~256 GB/s) favours MoE.** Generation speed is bounded by
  the *active* weights read per token: a dense 27–31B Q4 model tops out around
  10–25 tok/s; a 30–35B MoE with ~3B active runs at 50–70 tok/s here.
- **One large model resident at a time.** Swapping 20–40 GB models costs seconds
  per switch, so the primary model must cover both roles below.
- **Offline Claude Code needs reliable tool calling and ≥64K context.**

## Roles

| Role | Needs |
| ---- | ----- |
| Primary coder | Drives offline Claude Code; default for the MCP tool. Tool calling, code quality, 64K+ context |
| Fast helper (optional) | Bulk summarise/classify. Only if it fits beside the primary |
| Embeddings | Semantic code search for the MCP tool. Small, stays loaded |
| Unrestricted variant | Abliterated build of the primary for offline-only projects. Tested separately |

## Candidates

### Primary coder

| Model | Type | Quant → size | Why it's here |
| ----- | ---- | ------------ | ------------- |
| **Qwen-AgentWorld 35B-A3B** (`unsloth/Qwen-AgentWorld-35B-A3B-GGUF`) | MoE, 3B active | Q5_K_M 26.5 GB, Q8_0 36.9 GB | Best agentic score per second in a 23-model Strix Halo benchmark (8/10, ~51 tok/s) |
| **Qwen3.6-35B-A3B** (`unsloth/Qwen3.6-35B-A3B-MTP-GGUF`) | MoE, 3B active, MTP | Q4_K_M 22.7 GB | Fastest strong MoE there (~60 tok/s); MTP draft heads for speculative decoding |
| **Qwen3-Coder 30B-A3B** (`unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`) | MoE, 3.3B active | Q4_K_M 18.6 GB | Speed baseline (~66 tok/s); widely used with Claude Code |
| **Qwen3.8-27B** (`unsloth/Qwen3.8-27B-GGUF`) | Dense, MTP | Q4_K_M 16.5 GB | Highest-quality dense in that benchmark (8/10) but ~20–25 tok/s |
| **Devstral Small 2 24B** (`unsloth/Devstral-Small-2-24B-Instruct-2512-GGUF`) | Dense | Q4_K_M 14.3 GB | Purpose-built for agentic coding; Apache-2.0 |
| **Qwen3-Coder-Next 80B-A3B** (`unsloth/Qwen3-Coder-Next-GGUF`) | MoE, 3B active | UD-Q3_K_XL 36.3 GB | Largest coder that fits; scored only 5/10 there. Your Q4 (51.7 GB) does not fit with context |

### Fast helper

| Model | Quant → size | Note |
| ----- | ------------ | ---- |
| gpt-oss-20b (`ggml-org/gpt-oss-20b-GGUF`) | MXFP4 12.1 GB (native format) | MoE, very fast, Apache-2.0 |
| Gemma 4 E4B (`unsloth/gemma-4-E4B-it-GGUF`) | Q4_K_M 5.0 GB | Small enough to sit beside any primary |

### Embeddings

| Model | Quant → size | Note |
| ----- | ------------ | ---- |
| Qwen3-Embedding-0.6B (`Qwen/Qwen3-Embedding-0.6B-GGUF`) | Q8_0 0.6 GB | Tiny, always-on default |
| Qwen3-Embedding-4B (`Qwen/Qwen3-Embedding-4B-GGUF`) | Q4_K_M 2.5 GB | Quality step up |
| jina-embeddings-v4 text-code (`jinaai/jina-embeddings-v4-text-code-GGUF`) | Q4_K_M 1.9 GB | Code-retrieval adapter |

### Already downloaded (Ollama, `/var/lib/ollama`)

Reused as-is (Ollama blobs are plain GGUF) and compared against their official builds:
`qwen3-coder-next-abliterated:q4_K` (51.7 GB; offload test only),
`gemma-4-abliterated:31b` (19.9 GB), `Qwen3.8-27B-Uncensored` (17.7 GB),
`gemma-4-abliterated:e4b` (9.6 GB).

## Excluded

| Model | Why |
| ----- | --- |
| gpt-oss-120b | Best quality in the benchmark (9/10) but ~62.6 GB: exceeds the 52 GiB GPU budget |
| Qwen3.5-122B-A10B | Only fits at ~Q2; quality loss outweighs size |
| Mistral Medium 3.5 128B | ~3 tok/s here |
| vLLM-served models | Experimental on gfx1151 (GPU resets reported) |

## Download plan for phase 3 (~96 GB, 233 GB free)

Tier 1: AgentWorld Q5_K_M, Qwen3.6-35B-A3B-MTP Q4_K_M, Qwen3-Coder-30B Q4_K_M,
Devstral Small 2 Q4_K_M, gpt-oss-20b, Qwen3-Embedding 0.6B + 4B.
Tier 2 (only if Tier 1 leaves a gap): Qwen3.8-27B official, Qwen3-Coder-Next
UD-Q3_K_XL, jina-embeddings-v4. The abliterated variant is chosen after the primary.

## Caveats

- The reference benchmark ran on a 128 GB Strix Halo with ~10 tasks per model, so
  its scores are a starting point, not a verdict. Our phase 3 eval decides.
- llama.cpp v0.5.0 removed the rocWMMA flash-attention path (PR #26046) that
  older Strix Halo tuning guides recommend.
- Check each model's licence before relying on it (Qwen/Devstral/gpt-oss:
  Apache-2.0; Gemma: Gemma terms).

## Sources

- [Strix Halo benchmark: 23 coding deployments (Soot / Silicon, Aug 2026)](https://www.soothill.io/blog/2026/08/14/coding-model-benchmark-strix-halo/)
- [llama.cpp: Vulkan vs ROCm on Strix Halo (Aug 2026)](https://www.soothill.io/blog/2026/08/03/llamacpp-vulkan-vs-rocm-strix-halo/)
- [Tuning llama.cpp on Strix Halo (msbs.com)](https://msbs.com/writing/tuning-llama-cpp-strix-halo/)
- [New in llama.cpp: Anthropic Messages API](https://huggingface.co/blog/ggml-org/anthropic-messages-api-in-llamacpp)
- Hugging Face model APIs for repository names and file sizes
