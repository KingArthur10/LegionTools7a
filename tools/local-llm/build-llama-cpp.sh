#!/usr/bin/env bash
# Build llama.cpp for the Radeon 8060S (gfx1151) with two backends, side by side:
#   build-rocm/bin/    HIP/ROCm (system ROCm; run with ROCBLAS_USE_HIPBLASLT=1)
#   build-vulkan/bin/  Vulkan (Mesa RADV)
#
#   ./build-llama-cpp.sh [rocm|vulkan|all]    (default: all; run as your user)
#
# Output goes to $LLAMA_CPP_DIR (default ~/.cache/legiontools7a/llama.cpp).
set -euo pipefail

LLAMA_CPP_REPO="https://github.com/ggml-org/llama.cpp.git"
LLAMA_CPP_TAG="v0.5.0"
LLAMA_CPP_COMMIT="7fe450e19305b828c199d602c23a8337aaa1f03b" # 2026-09-23
GPU_TARGET="gfx1151"
DIR="${LLAMA_CPP_DIR:-$HOME/.cache/legiontools7a/llama.cpp}"

COMMON_DEPS=(cmake gcc-c++ ccache libcurl-devel)
ROCM_DEPS=(rocm-hip-devel hipblas-devel rocblas-devel hipblaslt-devel rocm-cmake)
VULKAN_DEPS=(vulkan-headers vulkan-loader-devel glslc spirv-headers-devel libshaderc-devel)

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

log() { printf '==> %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

require_pkgs() {
    local missing=()
    for pkg in "$@"; do rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg"); done
    ((${#missing[@]} == 0)) || die "missing packages: sudo dnf install ${missing[*]}"
}

fetch_source() {
    mkdir -p "$DIR"
    [[ -d $DIR/src/.git ]] || git clone --quiet "$LLAMA_CPP_REPO" "$DIR/src"
    git -C "$DIR/src" fetch --quiet --tags origin
    git -C "$DIR/src" checkout --quiet --detach "$LLAMA_CPP_COMMIT"
    log "llama.cpp $LLAMA_CPP_TAG ($LLAMA_CPP_COMMIT)"
}

# Options shared by both builds. GGML_NATIVE tunes CPU code for this Zen 5 host.
cmake_common=(
    -DCMAKE_BUILD_TYPE=Release
    -DGGML_NATIVE=ON
    -DLLAMA_CURL=ON
    -DLLAMA_BUILD_TESTS=OFF
)

build_rocm() {
    require_pkgs "${COMMON_DEPS[@]}" "${ROCM_DEPS[@]}"
    log "Configuring ROCm build for $GPU_TARGET"
    HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
        cmake -S "$DIR/src" -B "$DIR/build-rocm" "${cmake_common[@]}" \
        -DGGML_HIP=ON -DGPU_TARGETS="$GPU_TARGET" >/dev/null
    log "Compiling ROCm build"
    cmake --build "$DIR/build-rocm" --config Release -j "$(nproc)" >/dev/null
    "$DIR/build-rocm/bin/llama-server" --version 2>&1 | tail -2
}

build_vulkan() {
    require_pkgs "${COMMON_DEPS[@]}" "${VULKAN_DEPS[@]}"
    log "Configuring Vulkan build"
    cmake -S "$DIR/src" -B "$DIR/build-vulkan" "${cmake_common[@]}" -DGGML_VULKAN=ON >/dev/null
    log "Compiling Vulkan build"
    cmake --build "$DIR/build-vulkan" --config Release -j "$(nproc)" >/dev/null
    "$DIR/build-vulkan/bin/llama-server" --version 2>&1 | tail -2
}

[[ $EUID -ne 0 ]] || die "run as your normal user, not root"
case "${1:-all}" in
rocm) fetch_source && build_rocm ;;
vulkan) fetch_source && build_vulkan ;;
all) fetch_source && build_rocm && build_vulkan ;;
-h | --help) usage 0 ;;
*) usage 1 ;;
esac
log "Done. Binaries in $DIR/build-{rocm,vulkan}/bin"
