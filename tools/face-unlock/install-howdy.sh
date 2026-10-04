#!/usr/bin/env bash
# Build and install Howdy (face authentication) self-contained under /opt/howdy.
#
#   ./install-howdy.sh build          # as your user: fetch + compile everything
#   sudo ./install-howdy.sh install   # as root: install from the build dir only
#   sudo ./install-howdy.sh uninstall [--purge]
#
# The install step never downloads anything; it only copies what `build` produced.
set -euo pipefail

HOWDY_REPO="https://github.com/boltgolt/howdy.git"
HOWDY_COMMIT="d3ab99382f88f043d15f15c1450ab69433892a1c" # master, 2025-06-22 (3.0.0 beta)
PY_PACKAGES=(dlib==20.0.1 opencv-python-headless==4.14.0.94 numpy==2.5.3)
MODELS_URL="https://github.com/davisking/dlib-models/raw/master"
declare -A MODEL_SHA256=(
    [dlib_face_recognition_resnet_model_v1]=abb1f61041e434465855ce81c2bd546e830d28bcbed8d27ffbe5bb408b11553a
    [mmod_human_face_detector]=db9e9e40f092c118d5eb3e643935b216838170793559515541c56a2b50d9fc84
    [shape_predictor_5_face_landmarks]=6e787bbebf5c9efdb793f6cd1f023230c4413306605f24f299f12869f95aa472
)
# IR camera on the Legion 7 15ASH11 (by-path: stable, contains no serial number)
IR_DEVICE="/dev/v4l/by-path/pci-0000:c3:00.4-usb-0:1:1.2-video-index0"

PREFIX="/opt/howdy"
VENV="$PREFIX/venv"
CONFIG_DIR="/etc/howdy"
PAM_DIR="/usr/lib64/security"
BUILD_DEPS=(meson ninja-build gcc-c++ cmake bzip2 pam-devel inih-devel libevdev-devel python3-devel)

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

log() { printf '==> %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

build_dir_for() {
    local home
    home=$(getent passwd "$1" | cut -d: -f6)
    echo "${FACE_UNLOCK_BUILD_DIR:-$home/.cache/legiontools7a/face-unlock}"
}

cmd_build() {
    [[ $EUID -ne 0 ]] || die "run 'build' as your normal user, not root"
    local missing=()
    for pkg in "${BUILD_DEPS[@]}"; do rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg"); done
    ((${#missing[@]} == 0)) || die "missing packages: sudo dnf install ${missing[*]}"

    local dir
    dir=$(build_dir_for "$USER")
    mkdir -p "$dir"
    cd "$dir"

    log "Fetching Howdy $HOWDY_COMMIT"
    [[ -d howdy/.git ]] || git clone --quiet "$HOWDY_REPO" howdy
    git -C howdy fetch --quiet origin "$HOWDY_COMMIT"
    git -C howdy checkout --quiet --detach "$HOWDY_COMMIT"

    log "Building Python wheels (dlib compiles from source; takes a few minutes)"
    [[ -x buildvenv/bin/pip ]] || python3 -m venv buildvenv
    CMAKE_BUILD_PARALLEL_LEVEL=$(nproc) buildvenv/bin/pip wheel --quiet --disable-pip-version-check \
        --wheel-dir wheels "${PY_PACKAGES[@]}"

    log "Downloading and verifying dlib models"
    mkdir -p dlib-data
    for model in "${!MODEL_SHA256[@]}"; do
        local bz="dlib-data/$model.dat.bz2"
        [[ -f $bz ]] || curl -sSfL --retry 5 -o "$bz" "$MODELS_URL/$model.dat.bz2"
        echo "${MODEL_SHA256[$model]}  $bz" | sha256sum --quiet -c - ||
            die "checksum mismatch for $bz (delete it and re-run build)"
        bzip2 -dkf "$bz"
    done
    log "Build complete: $dir"
    log "Next: sudo $0 install"
}

cmd_install() {
    [[ $EUID -eq 0 ]] || die "run 'install' with sudo"
    [[ -n ${SUDO_USER:-} ]] || die "run via sudo so the build directory can be located"
    local dir
    dir=$(build_dir_for "$SUDO_USER")
    [[ -d $dir/wheels && -d $dir/howdy && -d $dir/dlib-data ]] ||
        die "no build found in $dir; run '$0 build' first"

    log "Creating $VENV from local wheels"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-index --find-links "$dir/wheels" "${PY_PACKAGES[@]}"

    log "Configuring and compiling Howdy as $SUDO_USER"
    local wipe=()
    [[ -f $dir/build/build.ninja ]] && wipe=(--wipe)
    runuser -u "$SUDO_USER" -- meson setup "${wipe[@]}" "$dir/build" "$dir/howdy" \
        --prefix="$PREFIX" \
        -Dpython_path="$VENV/bin/python" \
        -Dconfig_dir="$CONFIG_DIR" \
        -Ddlib_data_dir="$PREFIX/share/dlib-data" \
        -Dpam_dir="$PAM_DIR" >/dev/null
    runuser -u "$SUDO_USER" -- meson compile -C "$dir/build" >/dev/null

    local fresh_config=1
    [[ -f $CONFIG_DIR/config.ini ]] && fresh_config=0
    [[ $fresh_config -eq 1 ]] || cp -a "$CONFIG_DIR/config.ini" "$dir/config.ini.keep"

    log "Installing to $PREFIX and $PAM_DIR/pam_howdy.so"
    meson install -C "$dir/build" --quiet
    install -m 644 "$dir"/dlib-data/*.dat "$PREFIX/share/dlib-data/"
    ln -sfn "$PREFIX/bin/howdy" /usr/local/bin/howdy

    if [[ $fresh_config -eq 1 ]]; then
        log "Writing initial config (IR camera: $IR_DEVICE)"
        sed -i -e "s|^device_path = .*|device_path = $IR_DEVICE|" \
            -e "s|^detection_notice = .*|detection_notice = true|" \
            "$CONFIG_DIR/config.ini"
    else
        log "Keeping existing $CONFIG_DIR/config.ini"
        cp -a "$dir/config.ini.keep" "$CONFIG_DIR/config.ini"
    fi

    restorecon -RF "$PREFIX" "$CONFIG_DIR" "$PAM_DIR/pam_howdy.so" /usr/local/bin/howdy
    log "Installed. Next: sudo howdy add   (then enable PAM with face_unlock.py)"
}

cmd_uninstall() {
    [[ $EUID -eq 0 ]] || die "run 'uninstall' with sudo"
    local here
    here=$(dirname "$(readlink -f "$0")")
    log "Removing pam_howdy from PAM services"
    python3 "$here/face_unlock.py" disable
    rm -rf "$PREFIX" "$PAM_DIR/pam_howdy.so" /usr/local/bin/howdy
    if [[ ${1:-} == --purge ]]; then
        rm -rf "$CONFIG_DIR" /var/log/howdy
        log "Removed $CONFIG_DIR (config and face models)"
    else
        log "Kept $CONFIG_DIR (config and face models); use --purge to remove"
    fi
}

case "${1:-}" in
build) cmd_build ;;
install) cmd_install ;;
uninstall) cmd_uninstall "${2:-}" ;;
-h | --help) usage 0 ;;
*) usage 1 ;;
esac
