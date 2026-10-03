#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""View and control the battery charge mode on Lenovo Legion / IdeaPad laptops.

The firmware only offers a fixed ~80% stop threshold ("Long_Life"). The `hold`
command emulates any stop threshold from 80-100% by charging in "Standard" mode
until the target is reached, then switching to "Long_Life", which stops charging
without discharging back down to 80%.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("battery-threshold")

DEFAULT_SYSFS = Path("/sys/class/power_supply")
FIRMWARE_LIMIT = 80  # Long_Life stop threshold, fixed in the embedded controller
MODES = {"standard": "Standard", "long-life": "Long_Life", "fast": "Fast"}


class BatteryError(Exception):
    """A required sysfs interface is missing or unusable."""


@dataclass(frozen=True)
class Status:
    capacity: int
    state: str
    mode: str
    modes: tuple[str, ...]
    ac_online: bool | None


def parse_charge_types(text: str) -> tuple[str, tuple[str, ...]]:
    """Parse sysfs `charge_types` like 'Fast Standard [Long_Life]'."""
    active = None
    choices = []
    for token in text.split():
        if token.startswith("[") and token.endswith("]"):
            token = token[1:-1]
            active = token
        choices.append(token)
    if active is None:
        raise BatteryError(f"no active mode in charge_types: {text.strip()!r}")
    return active, tuple(choices)


class Battery:
    def __init__(self, sysfs: Path = DEFAULT_SYSFS, name: str = "BAT0") -> None:
        self.sysfs = sysfs
        self.path = sysfs / name
        if not (self.path / "charge_types").exists():
            raise BatteryError(
                f"{self.path}/charge_types not found; this kernel/driver does not "
                "expose battery charge modes (needs ideapad_laptop or lenovo-wmi-other)"
            )

    def _read(self, attr: str) -> str:
        return (self.path / attr).read_text().strip()

    def ac_online(self) -> bool | None:
        """True if any mains supply reports online; None if there is none."""
        found = None
        for supply in self.sysfs.iterdir():
            type_file = supply / "type"
            if type_file.exists() and type_file.read_text().strip() == "Mains":
                found = False
                if (supply / "online").read_text().strip() == "1":
                    return True
        return found

    def status(self) -> Status:
        mode, modes = parse_charge_types(self._read("charge_types"))
        return Status(
            capacity=int(self._read("capacity")),
            state=self._read("status"),
            mode=mode,
            modes=modes,
            ac_online=self.ac_online(),
        )

    def set_mode(self, mode: str, *, dry_run: bool = False) -> bool:
        """Set the charge mode. Returns True if a change was (or would be) made."""
        current = self.status()
        if mode not in current.modes:
            raise BatteryError(f"mode {mode!r} not supported; available: {current.modes}")
        if current.mode == mode:
            log.debug("mode already %s", mode)
            return False
        if dry_run:
            log.info("[dry-run] would change mode %s -> %s", current.mode, mode)
            return True
        (self.path / "charge_types").write_text(mode)
        log.info("changed mode %s -> %s", current.mode, mode)
        return True


def decide_mode(capacity: int, current: str, target: int, hysteresis: int) -> str:
    """Pick the charge mode needed to hold `capacity` at `target` percent.

    Charges in Standard until the target is reached, then switches to Long_Life.
    Charging resumes only once capacity falls `hysteresis` points below target,
    so the mode doesn't flap around a single percentage point.
    """
    if target <= FIRMWARE_LIMIT:
        return "Long_Life"
    if target >= 100:
        return "Standard"
    if capacity >= target:
        return "Long_Life"
    if capacity <= target - hysteresis:
        return "Standard"
    # Inside the hysteresis band: keep doing what we were doing.
    return current if current in ("Standard", "Long_Life") else "Long_Life"


def require_root(dry_run: bool) -> None:
    if not dry_run and os.geteuid() != 0:
        raise BatteryError("changing the charge mode requires root (try sudo, or --dry-run)")


def cmd_status(battery: Battery, _args: argparse.Namespace) -> int:
    s = battery.status()
    ac = {True: "online", False: "offline", None: "unknown"}[s.ac_online]
    print(f"capacity:  {s.capacity}%")
    print(f"state:     {s.state}")
    print(f"AC:        {ac}")
    print(f"mode:      {s.mode}")
    print(f"available: {' '.join(s.modes)}")
    return 0


def cmd_mode(battery: Battery, args: argparse.Namespace) -> int:
    require_root(args.dry_run)
    battery.set_mode(MODES[args.mode], dry_run=args.dry_run)
    return 0


def cmd_hold(battery: Battery, args: argparse.Namespace) -> int:
    require_root(args.dry_run)
    stop = False

    def _stop(signum: int, _frame: object) -> None:
        nonlocal stop
        log.info("received %s, exiting", signal.Signals(signum).name)
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("holding battery at %d%% (hysteresis %d)", args.target, args.hysteresis)
    while True:
        s = battery.status()
        wanted = decide_mode(s.capacity, s.mode, args.target, args.hysteresis)
        log.debug("capacity=%d%% state=%s mode=%s -> %s", s.capacity, s.state, s.mode, wanted)
        battery.set_mode(wanted, dry_run=args.dry_run)
        if args.once or stop:
            return 0
        # Sleep in short steps so SIGTERM is handled promptly.
        deadline = time.monotonic() + args.interval
        while not stop and time.monotonic() < deadline:
            time.sleep(1)
        if stop:
            return 0


def percent(value: str) -> int:
    n = int(value)
    if not FIRMWARE_LIMIT <= n <= 100:
        raise argparse.ArgumentTypeError(
            f"target must be {FIRMWARE_LIMIT}-100 (the firmware cannot stop charging "
            f"below {FIRMWARE_LIMIT}%)"
        )
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="battery-threshold",
        description="View and control battery charge mode; hold a custom 80-100% limit.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--battery", default="BAT0", help="battery name (default: BAT0)")
    parser.add_argument("--sysfs-root", type=Path, default=DEFAULT_SYSFS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show battery state and charge mode (default)")

    p_mode = sub.add_parser("mode", help="set the firmware charge mode")
    p_mode.add_argument("mode", choices=MODES, help="long-life caps at ~80%%")
    p_mode.add_argument("--dry-run", action="store_true", help="show what would change")

    p_hold = sub.add_parser("hold", help="hold charge at a target percentage (80-100)")
    p_hold.add_argument("--target", type=percent, required=True, help="stop charging at N%%")
    p_hold.add_argument(
        "--hysteresis",
        type=int,
        default=2,
        help="resume charging once N points below target (default: 2)",
    )
    p_hold.add_argument(
        "--interval", type=int, default=60, help="seconds between checks (default: 60)"
    )
    p_hold.add_argument("--once", action="store_true", help="check once and exit")
    p_hold.add_argument("--dry-run", action="store_true", help="log decisions, change nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    handlers = {"status": cmd_status, None: cmd_status, "mode": cmd_mode, "hold": cmd_hold}
    try:
        battery = Battery(args.sysfs_root, args.battery)
        return handlers[args.command](battery, args)
    except (BatteryError, OSError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
