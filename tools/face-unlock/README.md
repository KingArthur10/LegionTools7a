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
| `/opt/howdy/` | Howdy, CLI, dlib models, and `venv/` (dlib 20.0.1, OpenCV 4.14, NumPy 2.5; rebuilt for Python 3.15 on Fedora 45, see [below](#after-a-fedora-or-python-upgrade)) |
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

## After a Fedora or Python upgrade

`/opt/howdy/venv` is tied to the Python minor version it was created with. A Fedora
release upgrade that bumps the system Python (Fedora 45: 3.14 → 3.15, 2026-11) removes
the old interpreter, and face authentication then fails with:

```
Attempting facial authentication
Traceback (most recent call last):
  File "/opt/howdy/lib64/howdy/compare.py", line 17, in <module>
    import dlib
ModuleNotFoundError: No module named 'dlib'. Did you mean: 'zlib'?
```

The packages are still installed, but in `venv/lib/python3.14/site-packages`; the
venv's `python3` symlink now resolves to 3.15, which looks in `python3.15/`. The
password fallback keeps working, so nothing locks you out. Face models and
`config.ini` in `/etc/howdy` are unaffected, and the C++ PAM module only stores the
path `/opt/howdy/venv/bin/python`, so neither Howdy nor PAM needs rebuilding — only
the venv:

```bash
sudo mv /opt/howdy/venv /opt/howdy/venv.py314.bak
sudo python3 -m venv /opt/howdy/venv
sudo /opt/howdy/venv/bin/pip install dlib==20.0.1 numpy opencv-python-headless==4.14.0.94
sudo restorecon -Rv /opt/howdy/venv      # lib_t on the .so files, as before
sudo -k && sudo true                     # should say "Attempting facial authentication"
sudo rm -rf /opt/howdy/venv.py314.bak    # once face unlock works
```

- dlib compiles from source (a few minutes); `cmake`, `gcc-c++` and `python3-devel`
  must be installed. NumPy and OpenCV ship wheels for new Pythons quickly (OpenCV's
  are `abi3`, so the pinned 4.14 works on 3.15).
- Pin OpenCV: unpinned pip installs **OpenCV 5.0**, which Howdy has not been tested
  with.
- To check a new Python before touching `/opt`, build the same packages in a scratch
  venv as your user and `import dlib, cv2, numpy`.
- `sudo howdy test` fails with `cv2.error: ... The function is not implemented.
  Rebuild the library with Windows, GTK+ 2.x or Cocoa support`. That is expected:
  `opencv-python-headless` has no GUI, and only `howdy test` opens a window. Test with
  `sudo -k && sudo true` or the lock screen instead.
- Don't use `./install-howdy.sh build` for this yet: the build directory
  (`~/.cache/legiontools7a/face-unlock`) keeps its old `buildvenv`, which is reused
  without a version check and breaks with the old interpreter gone. Delete
  `buildvenv/` and `wheels/` there first if you want to go through the installer.

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
