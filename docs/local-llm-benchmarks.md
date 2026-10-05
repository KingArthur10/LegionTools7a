# Local LLM benchmarks

Measured on this machine (Legion 7 15ASH11, Radeon 8060S `gfx1151`, 52 GiB GTT,
kernel 7.2.8, Mesa 26.2.3, ROCm 7.1.1) with llama.cpp v0.5.0
(`tools/local-llm/llm_bench.py`, `llm_eval.py`).

## Speed (2026-10-04)

`llama-bench`, flash attention on, q8_0 KV cache, all layers on GPU, 3 repetitions.
ROCm runs use `ROCBLAS_USE_HIPBLASLT=1`. Values in tokens/second.

| Model | Backend | Prompt 4K | Gen 128 | Prompt 4K @16K ctx | Gen 128 @16K ctx |
| ----- | ------- | --------: | ------: | -----------------: | ---------------: |
| Qwen-AgentWorld 35B-A3B UD-Q5_K_M | ROCm | **1181** | 45.7 | 822 | 38.9 |
| | Vulkan | 1106 | **57.6** | 819 | **54.0** |
| Qwen3.6 35B-A3B UD-Q4_K_M | ROCm | **1242** | 50.8 | 853 | 42.7 |
| | Vulkan | 1196 | **62.3** | 861 | **58.3** |
| Qwen3-Coder 30B-A3B Q4_K_M | ROCm | **1396** | 70.0 | **568** | 34.2 |
| | Vulkan | 1168 | **89.5** | 461 | **62.0** |
| Devstral Small 2 24B Q4_K_M (dense) | ROCm | 380 | 14.8 | 281 | 12.2 |
| | Vulkan | 363 | 14.4 | 262 | 13.0 |
| gpt-oss-20b MXFP4 | ROCm | **1701** | 69.3 | **1149** | 57.6 |
| | Vulkan | 1513 | **74.1** | 988 | **68.2** |
| Qwen3.8 27B Q4_K_M (dense, smoke test) | ROCm | 267 (pp512) | 11.9 | | |
| | Vulkan | 259 (pp512) | 12.7 | | |

### Findings

- **ROCm wins prompt processing by 4–20%; Vulkan wins generation by 7–80%**, with
  the biggest gap at depth (Qwen3-Coder: 62 vs 34 tok/s at 16K).
- **Vulkan is the default backend.** llama-server reuses the cached prefix, so an
  offline Claude Code turn processes only new tokens and is generation-bound; even
  a fresh 10K-token MCP request comes out even or ahead on Vulkan. It also avoids
  the ROCm runtime dependency.
- The hybrid-attention Qwen3.6 / AgentWorld models keep ~70% of prompt speed at
  16K context; full-attention Qwen3-Coder drops to ~40%.
- Dense models are bandwidth-bound at 12–15 tok/s generation, as predicted
  (~256 GB/s ÷ ~16 GB of weights). MoE models with ~3B active run 4–6× faster.
