#!/usr/bin/env bash
# Run Claude Code against the local llama-swap endpoint instead of Anthropic.
#
#   claude-local [--model NAME] [claude args...]   # start Claude Code on a local model
#   claude-local --env [--model NAME]              # print the exports instead
#   claude-local --list                            # list models on the endpoint
#
# NAME is a model from llama-swap.yaml (default: qwen3-coder). Every model role
# (main, Haiku/Sonnet/Opus aliases, subagents) uses it, because llama-swap keeps
# one chat model loaded and switching roles would reload it on every call.
# Env: LOCAL_LLM_URL (default http://127.0.0.1:8080), LOCAL_LLM_MODEL.
set -euo pipefail

url=${LOCAL_LLM_URL:-http://127.0.0.1:8080}
model=${LOCAL_LLM_MODEL:-qwen3-coder}
max_output=8192 # Claude Code reserves 32K by default: half of a 64K window

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}
die() {
    printf 'claude-local: %s\n' "$*" >&2
    exit 1
}

models() {
    local json
    json=$(curl -sf --max-time 5 "$url/v1/models") || return 1
    python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))' <<<"$json"
}

# Context size the server was started with. Loads the model if it isn't already.
context_size() {
    local json
    json=$(curl -sf --max-time 300 "$url/upstream/$1/props") || return 1
    python3 -c 'import json,sys; print(json.load(sys.stdin)["default_generation_settings"]["n_ctx"])' <<<"$json"
}

print_env=0
claude_args=()
while (($#)); do
    case $1 in
    --model)
        (($# >= 2)) || die "--model needs a value"
        model=$2
        shift 2
        ;;
    --model=*)
        model=${1#--model=}
        shift
        ;;
    --env)
        print_env=1
        shift
        ;;
    --list)
        models || die "no endpoint at $url (is llama-swap running? systemctl status llama-swap)"
        exit 0
        ;;
    -h | --help) usage 0 ;;
    *)
        claude_args+=("$1")
        shift
        ;;
    esac
done

available=$(models) || die "no endpoint at $url (is llama-swap running? systemctl status llama-swap)"
grep -qxF "$model" <<<"$available" ||
    die "model '$model' not served at $url; available: $(tr '\n' ' ' <<<"$available")"
[[ $model != embed ]] || die "'embed' is an embedding model, not a chat model"
printf 'claude-local: loading %s...\n' "$model" >&2
ctx=$(context_size "$model") || die "could not read context size for '$model' from $url"

vars=(
    ANTHROPIC_BASE_URL="$url"
    ANTHROPIC_AUTH_TOKEN=local
    ANTHROPIC_MODEL="$model"
    ANTHROPIC_DEFAULT_HAIKU_MODEL="$model"
    ANTHROPIC_DEFAULT_SONNET_MODEL="$model"
    ANTHROPIC_DEFAULT_OPUS_MODEL="$model"
    CLAUDE_CODE_SUBAGENT_MODEL="$model"
    # Without this Claude Code assumes 200K and never compacts before overflowing.
    CLAUDE_CODE_MAX_CONTEXT_TOKENS="$ctx"
    CLAUDE_CODE_MAX_OUTPUT_TOKENS="$max_output"
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    DISABLE_COST_WARNINGS=1
)

if ((print_env)); then
    echo "unset ANTHROPIC_API_KEY"
    for v in "${vars[@]}"; do printf 'export %s=%q\n' "${v%%=*}" "${v#*=}"; done
    exit 0
fi

printf 'claude-local: %s, %s-token context, via %s\n' "$model" "$ctx" "$url" >&2
exec env -u ANTHROPIC_API_KEY "${vars[@]}" claude "${claude_args[@]}"
