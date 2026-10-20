#!/usr/bin/env bash
# Download pinned GGUF models listed in models.tsv and verify their SHA-256.
#
#   ./download-models.sh [TIER]   (default: 1; "all" for every tier)
#
# Files go to $LLM_MODELS_DIR (default /srv/llm/models)/<repo>/<file>.
# Downloads resume if interrupted; verified files are skipped on re-runs.
set -euo pipefail

HERE=$(dirname "$(readlink -f "$0")")
MANIFEST="$HERE/models.tsv"
MODELS_DIR="${LLM_MODELS_DIR:-/srv/llm/models}"
TIER="${1:-1}"
ATTEMPTS=20     # per file; each attempt resumes the partial download
STALL_SECONDS=60 # abort an attempt that moves < 100 KB/s for this long

log() { printf '==> %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# Download $1 to $2, resuming. curl's own --retry skips some errors (e.g. 92,
# HTTP/2 stream reset) and a stalled transfer otherwise never ends, so retry here.
fetch() {
    local url=$1 out=$2 attempt
    for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
        curl -fL -C - --speed-limit 102400 --speed-time "$STALL_SECONDS" -o "$out" "$url" && return 0
        log "attempt $attempt/$ATTEMPTS failed; resuming in 10 s"
        sleep 10
    done
    return 1
}

case "$TIER" in -h | --help)
    sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac
[[ -d $MODELS_DIR && -w $MODELS_DIR ]] || die "$MODELS_DIR missing or not writable"

needed=0
while IFS=$'\t' read -r tier _ repo file bytes _; do
    [[ $tier == \#* || -z $tier ]] && continue
    [[ $TIER == all || $tier == "$TIER" ]] || continue
    [[ -f $MODELS_DIR/$repo/$file ]] || needed=$((needed + bytes))
done <"$MANIFEST"
avail=$(df --output=avail -B1 "$MODELS_DIR" | tail -1)
((needed < avail)) || die "need $((needed / 1000000000)) GB, only $((avail / 1000000000)) GB free"

while IFS=$'\t' read -r tier role repo file bytes sha; do
    [[ $tier == \#* || -z $tier ]] && continue
    [[ $TIER == all || $tier == "$TIER" ]] || continue
    dest="$MODELS_DIR/$repo/$file"
    mkdir -p "$(dirname "$dest")"
    if [[ -f $dest && -f $dest.verified ]]; then
        log "ok      $role  $file"
        continue
    fi
    log "fetch   $role  $file ($((bytes / 1000000)) MB)"
    fetch "https://huggingface.co/$repo/resolve/main/$file" "$dest.part" ||
        die "giving up on $file after $ATTEMPTS attempts"
    [[ $(stat -c %s "$dest.part") == "$bytes" ]] || die "size mismatch for $file"
    echo "$sha  $dest.part" | sha256sum --quiet -c - || die "checksum mismatch for $file"
    mv "$dest.part" "$dest"
    touch "$dest.verified"
done <"$MANIFEST"
log "All tier-$TIER models present in $MODELS_DIR"
