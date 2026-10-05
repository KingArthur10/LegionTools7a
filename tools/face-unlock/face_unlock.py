#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Enable or disable Howdy face authentication for individual PAM services.

Adds `-auth sufficient pam_howdy.so` as the first auth rule of each service, so
a recognised face authenticates immediately and anything else (no face, dark
image, timeout, SSH session, lid closed) falls through to the password prompt.
The leading `-` makes PAM skip the rule silently if the module is missing.

Vendor-only services (e.g. polkit-1 lives in /usr/lib/pam.d) are overridden by
copying them to /etc/pam.d; `disable` removes that copy again.
"""

from __future__ import annotations

import argparse
import difflib
import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger("face-unlock")

SERVICES = {
    "sudo": "terminal sudo",
    "kde": "KDE lock screen",
    "polkit-1": "graphical admin prompts (polkit)",
}
MARKER = "# Added by LegionTools7a tools/face-unlock: face first, password fallback"
HOWDY_LINE = "-auth       sufficient   pam_howdy.so"
PAM_MODULE = Path("/usr/lib64/security/pam_howdy.so")
BACKUP_DIR = Path("/var/lib/legiontools7a/face-unlock/backup")


class PamError(Exception):
    """A PAM file can't be safely changed."""


def _is_auth_rule(line: str) -> bool:
    words = line.split()
    return bool(words) and words[0] in ("auth", "-auth")


def has_howdy(text: str) -> bool:
    return any(
        "pam_howdy.so" in line and not line.lstrip().startswith("#") for line in text.splitlines()
    )


def add_howdy(text: str) -> str:
    """Insert the Howdy rule before the first auth rule. Idempotent."""
    if has_howdy(text):
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _is_auth_rule(line):
            return "".join([*lines[:i], MARKER + "\n", HOWDY_LINE + "\n", *lines[i:]])
    raise PamError("no auth rules found; refusing to guess where to add pam_howdy")


def remove_howdy(text: str) -> str:
    """Remove the Howdy rule and our marker comment. Idempotent."""
    kept = [
        line
        for line in text.splitlines(keepends=True)
        if line.rstrip("\n") != MARKER
        and not ("pam_howdy.so" in line and not line.lstrip().startswith("#"))
    ]
    return "".join(kept)


class PamService:
    def __init__(self, name: str, etc_dir: Path, vendor_dir: Path, backup_dir: Path) -> None:
        self.name = name
        self.path = etc_dir / name
        self.vendor = vendor_dir / name
        self.backup = backup_dir / f"{name}.orig"

    def current(self) -> tuple[str, bool]:
        """Return (text, from_vendor). PAM prefers /etc/pam.d over the vendor dir."""
        if self.path.exists():
            return self.path.read_text(), False
        if self.vendor.exists():
            return self.vendor.read_text(), True
        raise PamError(f"no PAM config for {self.name!r} in {self.path.parent} or vendor dir")

    def enabled(self) -> bool:
        try:
            return has_howdy(self.current()[0])
        except PamError:
            return False

    def enable(self, *, dry_run: bool) -> bool:
        text, from_vendor = self.current()
        new = add_howdy(text)
        if new == text:
            log.info("%s: already enabled", self.name)
            return False
        source = f"{self.vendor} (copied to {self.path})" if from_vendor else str(self.path)
        self._apply(text, new, source, dry_run=dry_run, backup=not from_vendor)
        return True

    def disable(self, *, dry_run: bool) -> bool:
        if not self.path.exists():
            log.info("%s: not enabled (no %s)", self.name, self.path)
            return False
        text = self.path.read_text()
        new = remove_howdy(text)
        if new == text:
            log.info("%s: not enabled", self.name)
            return False
        if self.vendor.exists() and new == self.vendor.read_text():
            # Our /etc copy only existed to add Howdy; drop it so the vendor file applies.
            show_diff(text, new, str(self.path))
            if dry_run:
                log.info("[dry-run] would remove %s (vendor file applies again)", self.path)
            else:
                self.path.unlink()
                log.info("%s: removed %s; vendor config applies again", self.name, self.path)
            return True
        self._apply(text, new, str(self.path), dry_run=dry_run, backup=False)
        return True

    def _apply(self, old: str, new: str, source: str, *, dry_run: bool, backup: bool) -> None:
        show_diff(old, new, source)
        if dry_run:
            log.info("[dry-run] %s: no changes written", self.name)
            return
        if backup and not self.backup.exists():
            self.backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.path, self.backup)
            log.info("%s: original saved to %s", self.name, self.backup)
        atomic_write(self.path, new)
        log.info("%s: updated %s", self.name, self.path)


def atomic_write(path: Path, text: str) -> None:
    """Replace `path` so PAM never sees a partially written file."""
    tmp = path.with_name(f".{path.name}.face-unlock.tmp")
    tmp.write_text(text)
    tmp.chmod(0o644)
    tmp.replace(path)


def show_diff(old: str, new: str, label: str) -> None:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), label, label
    )
    sys.stdout.writelines(diff)
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="face-unlock", description="Enable Howdy face authentication per PAM service."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--etc-dir", type=Path, default=Path("/etc/pam.d"), help=argparse.SUPPRESS)
    parser.add_argument(
        "--vendor-dir", type=Path, default=Path("/usr/lib/pam.d"), help=argparse.SUPPRESS
    )
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show which services use face authentication (default)")
    for name, verb in (("enable", "add face authentication to"), ("disable", "remove it from")):
        p = sub.add_parser(name, help=f"{verb} services (default: all)")
        p.add_argument(
            "services", nargs="*", metavar="SERVICE", help=f"one or more of: {', '.join(SERVICES)}"
        )
        p.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    unknown = set(getattr(args, "services", [])) - set(SERVICES)
    if unknown:
        parser.error(f"unknown service(s): {', '.join(sorted(unknown))}")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s"
    )

    def service(name: str) -> PamService:
        return PamService(name, args.etc_dir, args.vendor_dir, args.backup_dir)

    if args.command in (None, "status"):
        print(f"pam_howdy.so: {'installed' if PAM_MODULE.exists() else 'NOT installed'}")
        for name, desc in SERVICES.items():
            state = "face + password" if service(name).enabled() else "password only"
            print(f"{name:<9} {state:<16} {desc}")
        return 0

    if not args.dry_run and os.geteuid() != 0:
        log.error("changing PAM configuration requires root (try sudo, or --dry-run)")
        return 1
    if args.command == "enable" and not PAM_MODULE.exists():
        log.warning("%s not found; rules will be skipped until Howdy is installed", PAM_MODULE)

    try:
        for name in args.services or SERVICES:
            svc = service(name)
            if args.command == "enable":
                svc.enable(dry_run=args.dry_run)
            else:
                svc.disable(dry_run=args.dry_run)
    except (PamError, OSError) as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
