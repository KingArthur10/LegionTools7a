#!/usr/bin/env bash
# Install the local LLM endpoint as a system service (llama-swap + llama.cpp Vulkan).
#
#   ./install-server.sh prepare          # as your user: fetch + verify llama-swap
#   sudo ./install-server.sh install     # as root: install from prepared files only
#   sudo ./install-server.sh uninstall   # remove service and binaries (keeps models)
#
# Requires `./build-llama-cpp.sh vulkan` first. Serves http://127.0.0.1:8080.
set -euo pipefail

LLAMA_SWAP_VERSION="262"
LLAMA_SWAP_SHA256="871b3ed7891f8ce057428f5a34d38298e06c36c6848c1136e05308eb4303af50" # linux_amd64 tarball
HERE=$(dirname "$(readlink -f "$0")")
UNIT=/etc/systemd/system/llama-swap.service

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}
log() { printf '==> %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

cache_for() {
    local home
    home=$(getent passwd "$1" | cut -d: -f6)
    echo "$home/.cache/legiontools7a"
}

cmd_prepare() {
    [[ $EUID -ne 0 ]] || die "run 'prepare' as your normal user"
    local cache dir tarball base
    cache=$(cache_for "$USER")
    [[ -x $cache/llama.cpp/build-vulkan/bin/llama-server ]] ||
        die "no Vulkan build; run ./build-llama-cpp.sh vulkan first"
    dir="$cache/llama-swap"
    mkdir -p "$dir"
    base="https://github.com/mostlygeek/llama-swap/releases/download/v$LLAMA_SWAP_VERSION"
    tarball="llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz"
    log "Fetching llama-swap v$LLAMA_SWAP_VERSION"
    curl -sSfL -o "$dir/$tarball" "$base/$tarball"
    echo "$LLAMA_SWAP_SHA256  $dir/$tarball" | sha256sum --quiet -c - ||
        die "llama-swap checksum mismatch"
    tar -xzf "$dir/$tarball" -C "$dir" llama-swap
    "$dir/llama-swap" -config "$HERE/llama-swap.yaml" -validate
    log "Prepared. Next: sudo $0 install"
}

cmd_install() {
    [[ $EUID -eq 0 ]] || die "run 'install' with sudo"
    [[ -n ${SUDO_USER:-} ]] || die "run via sudo so the build directory can be located"
    local cache
    cache=$(cache_for "$SUDO_USER")
    [[ -x $cache/llama-swap/llama-swap ]] || die "run '$0 prepare' first"
    [[ -d /srv/llm/models ]] || die "/srv/llm/models missing"

    log "Creating system user 'llm'"
    install -D -m 644 /dev/stdin /etc/sysusers.d/llm.conf <<'EOF'
u llm - "Local LLM server" /var/lib/llm
m llm render
m llm video
EOF
    systemd-sysusers /etc/sysusers.d/llm.conf

    log "Installing llama.cpp (Vulkan) to /opt/llama.cpp"
    rm -rf /opt/llama.cpp
    cmake --install "$cache/llama.cpp/build-vulkan" --prefix /opt/llama.cpp >/dev/null
    log "Installing llama-swap to /opt/llama-swap"
    install -D -m 755 "$cache/llama-swap/llama-swap" /opt/llama-swap/llama-swap

    if [[ -f /etc/llama-swap/config.yaml ]] &&
        ! cmp -s "$HERE/llama-swap.yaml" /etc/llama-swap/config.yaml; then
        cp -a /etc/llama-swap/config.yaml "/etc/llama-swap/config.yaml.bak.$(date +%s)"
        log "Backed up existing /etc/llama-swap/config.yaml"
    fi
    install -D -m 644 "$HERE/llama-swap.yaml" /etc/llama-swap/config.yaml
    install -m 644 "$HERE/llama-swap.service" "$UNIT"
    restorecon -RF /opt/llama.cpp /opt/llama-swap /etc/llama-swap "$UNIT"

    systemctl daemon-reload
    systemctl enable llama-swap >/dev/null 2>&1
    systemctl restart llama-swap
    log "Waiting for http://127.0.0.1:8080"
    for _ in $(seq 30); do
        if curl -sf http://127.0.0.1:8080/v1/models >/dev/null; then
            log "llama-swap is up: $(curl -s http://127.0.0.1:8080/v1/models |
                python3 -c 'import json,sys;print(", ".join(m["id"] for m in json.load(sys.stdin)["data"]))')"
            return 0
        fi
        sleep 1
    done
    die "llama-swap did not start; see: journalctl -u llama-swap -n 50"
}

cmd_uninstall() {
    [[ $EUID -eq 0 ]] || die "run 'uninstall' with sudo"
    systemctl disable --now llama-swap 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf /opt/llama.cpp /opt/llama-swap /etc/llama-swap
    log "Removed service and binaries. Kept: user 'llm', /srv/llm/models, /var/lib/llm"
}

case "${1:-}" in
prepare) cmd_prepare ;;
install) cmd_install ;;
uninstall) cmd_uninstall ;;
-h | --help) usage 0 ;;
*) usage 1 ;;
esac
