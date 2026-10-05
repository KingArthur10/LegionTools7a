# face-unlock

## Purpose

Windows Hello-style face authentication on the Legion 7 15ASH11 using
[Howdy](https://github.com/boltgolt/howdy) and the built-in IR camera, for:

| Prompt | PAM service | Behaviour |
| ------ | ----------- | --------- |
| Terminal `sudo` | `sudo` | Face check starts immediately |
| KDE lock screen | `kde` | Press a key / click, then the face check runs |
| Graphical admin prompts | `polkit-1` | Face check starts when the dialog opens |

Your **password always works as a fallback**: no face, a dark image, a 4 s timeout,
an SSH session or a closed lid all fall through to the normal password prompt.

**The login screen deliberately still uses your password.** KDE Wallet is
encrypted with your login password and unlocked by capturing it at login. A face
login provides no password, so the wallet would stay locked. Typing the password
once at boot keeps the wallet working; face unlock covers everything after that.

## Requirements

- IR camera at `/dev/v4l/by-path/pci-0000:c3:00.4-usb-0:1:1.2-video-index0`. The
  emitter works out of the box (frames alternate lit/unlit; Howdy skips the dark ones).
  `linux-enable-ir-emitter` is **not** needed.
- Build packages: `sudo dnf install meson ninja-build gcc-c++ cmake bzip2 pam-devel
  inih-devel libevdev-devel python3-devel openblas-devel`
- Root for installing and for changing PAM.

### Why not the COPR package?

The `principis/howdy-beta` COPR can't be installed on Fedora 44: `python3-dlib` doesn't
exist, and its `python3-pyv4l2`/`python3-keyboard` builds target Python 3.13.
`install-howdy.sh` instead builds Howdy (pinned commit) with its own Python venv:

| Path | Contents |
| ---- | -------- |
| `/opt/howdy/` | Howdy, CLI, dlib models, and `venv/` (dlib 20.0.1, OpenCV 4.14, NumPy 2.5) |
| `/etc/howdy/` | `config.ini` and face models (`models/<user>.dat`) |
| `/usr/lib64/security/pam_howdy.so` | PAM module |
| `/usr/local/bin/howdy` | Symlink to the CLI |

## Usage

```bash
# 1. Build as your user (downloads + compiles; dlib takes a few minutes the first time)
./install-howdy.sh build
# 2. Install (copies the build only; downloads nothing)
sudo ./install-howdy.sh install
# 3. Enroll your face (repeat with/without glasses, different labels)
sudo howdy add
# 4. Enable per PAM service (diff shown; --dry-run to preview)
sudo python3 face_unlock.py enable              # all: sudo kde polkit-1
sudo python3 face_unlock.py enable sudo         # or one at a time
python3 face_unlock.py                          # status
```

```
usage: face-unlock [-h] [-v] {status,enable,disable} ...

  status    show which services use face authentication (default)
  enable    add face authentication to services (default: all)
  disable   remove it from services (default: all)

enable/disable: [SERVICE ...] [--dry-run]   SERVICE: sudo, kde, polkit-1
```

`face_unlock.py` runs with plain `python3` (standard library only), so it works
under `sudo` without `uv`.

### Uninstall

```bash
sudo ./install-howdy.sh uninstall           # removes PAM rules, /opt/howdy, module
sudo ./install-howdy.sh uninstall --purge   # also removes /etc/howdy (face models)
```

## Safety notes

- **Keep a root shell open** (`sudo -i` in another terminal) when enabling PAM
  services, and test with `sudo -k; sudo true` before closing it.
- Each rule is `-auth sufficient pam_howdy.so`, placed first in the auth stack.
  The `-` makes PAM skip it silently if the module is removed.
- Edits are idempotent and atomic; the original file is saved once to
  `/var/lib/legiontools7a/face-unlock/backup/`. Vendor-only services (`polkit-1`
  lives in `/usr/lib/pam.d`) get an override copy in `/etc/pam.d`, which
  `disable` deletes again; the vendor file is never edited.
- SELinux stays enforcing; no policy module was needed (verified 2026-10-04, no AVC
  denials for sudo, kscreenlocker or polkit-agent-helper).
- Face recognition is **weaker than a password**: the IR camera defeats printed
  photos, but Howdy has no dedicated liveness detection. Disable it for sensitive
  setups with `sudo python3 face_unlock.py disable`.
- Face models are world-readable (`0644`) because the lock screen runs the check
  as your user. They contain face embeddings, not images.
