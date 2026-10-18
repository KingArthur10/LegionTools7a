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

## Quality (2026-10-04)

`llm_eval.py`, Vulkan, temperature 0.2, `max_tokens` 8192, 3 attempts per task.
`log_root_cause` was re-run at 64K context (the first run overflowed 32K, a harness
bug); `write_tests` ran in both passes (6 attempts). All five models loaded and ran
at 64K context with a q8_0 KV cache within the 52 GiB budget.

| Model | log_root_cause | diff_bug | write_tests | implement | json_extract | agentic_fix | Score | Wall time* |
| ----- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | --: |
| **Qwen3-Coder 30B-A3B** | 3/3 | 3/3 | 6/6 | 3/3 | 3/3 | 3/3 | **21/21** | **~1 min** |
| Devstral Small 2 24B | 3/3 | 3/3 | 5/6 | 3/3 | 3/3 | 3/3 | 20/21 | ~4 min |
| Qwen-AgentWorld 35B-A3B | 3/3 | 3/3 | 3/6 | 3/3 | 3/3 | 3/3 | 18/21 | ~10 min |
| Qwen3.6 35B-A3B | 3/3 | 3/3 | 2/6 | 3/3 | 3/3 | 3/3 | 17/21 | ~9 min |
| gpt-oss-20b | 2/3† | 3/3 | 4/6 | 0/3‡ | 3/3 | 3/3 | 15/21 | ~10 min |

\* First pass, all six tasks × 3 attempts. † 32K run; 64K re-run 2/3.
‡ HTTP 500: gpt-oss emitted `<|channel|>final code<|message|>`, which llama.cpp
v0.5.0's Harmony parser rejects. That would fail in real use too.

### Why models failed

- **Thinking budget exhausted** (Qwen3.6 ×2, gpt-oss ×2, "no tests ran" / empty
  answer): these models reason before answering and hit the 8,192-token limit with
  no final output after ~140 s. For tool-style delegation they need thinking
  disabled or a much larger budget.
- **Arguable spec gap** (AgentWorld ×1): tests expected `ValueError` for `None`;
  the reference raises `TypeError`. The docstring only says "invalid" text.
- **Wrong assumption** (Devstral ×1): expected `ValueError` for an input the
  reference accepts.

### Caveats

- **Ceiling effect.** These tasks are small; every model passed `diff_bug`,
  `json_extract` and the agentic fix 3/3. The eval shows reliability on
  delegation-sized work, not capability on hard multi-step problems, where the
  reference Strix Halo benchmark ranked AgentWorld (8/10) well above Qwen3-Coder
  (5/10).
- 3 attempts per task is a small sample; treat one-attempt differences as noise.

## Hybrid reading via MCP (2026-10-18)

Does handing reading to the local model (`llm_mcp.py`: `local_ask`, `local_search`)
save Claude tokens? Claude Code 2.1.292, Opus 5.5, `claude -p --output-format json`,
one run per cell. Conditions: **A** no MCP server; **B** server available plus a
system-prompt note recommending it for bulk reading; **C** prompt requires reading
through the local tools (Read allowed only to verify two line ranges).

| Task | A | B | C |
| ---- | :-: | :-: | :-: |
| Root cause in a 6,000-line synthetic service log (387 KB) | $0.18, 15 s ✓ | $0.21, 21 s ✓, tools unused | $0.17, 26 s ✓ (local_ask failed†; answered from local_search) |
| Summarize every tool in `tools/` | $0.20, 13 s ✓ | $0.18, 16 s ✓, tools unused | $0.16, 31 s ✓ |
| How `battery-threshold` restores the previous limit | $0.14, 8 s ✓ | $0.13, 17 s ✓, tools unused | $0.15, 48 s ✓ |
| llama.cpp: Anthropic → internal format mapping | $0.20, 14 s ✓ | – | not finished‡ |
| llama.cpp: architecture of `tools/server` | $0.25, 28 s ✓ | – | not run‡ |

† Overflowed the 64K context; fixed since (see the bug notes below).
‡ First-time `local_search` over the whole llama.cpp tree had no cap and ran for
~an hour. Indexing is capped per call now.

The log question from the shell with the fixed `local_ask`, first 3,000 lines
(~135K tokens): **6 min**, and the answer named the symptom (pool exhausted) but
**missed the trigger** (`max_connections: 50 -> 5` on line 2406, which was in its
input) and gave the wrong start time. Opus with grep got all three right in 15 s.

### Findings

- **No saving on reading.** Opus already reads little: it greps and reads the
  matching ranges. Most of each run's cost is the fixed system prompt and tool
  definitions (~20K tokens per turn, served from cache), which delegation can't
  remove.
- **Claude doesn't choose the tools.** Claude Code defers MCP tool definitions
  behind ToolSearch, so in B they were never loaded. `"alwaysLoad": true` on the
  server would change that, but given the results there's no reason to.
- **The local model is a weak reader of noisy input.** ~370 tokens/s end to end
  for map-reduce over a long log, and it missed an INFO-level cause.
- **Token density varies 2×.** Code is ~3.5 characters per token; a log full of
  timestamps is 1.65 (Qwen's tokenizer splits digits). `local_ask` measures each
  file with `/tokenize` rather than assuming a ratio.
- **Conclusion:** keep `local_ask` / `local_search` as optional tools (no
  `CLAUDE.md` nudge, no `alwaysLoad`). The hybrid effort moves to output: having
  the local model *write* well-specified code with tests, Opus reviewing.
