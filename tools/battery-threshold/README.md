# battery-threshold

## Purpose

View and change the battery charge mode on the Legion 7 15ASH11, and hold the
battery at **any limit from 80% to 100%**, to the percent.

The firmware itself only supports one fixed stop threshold: `Long_Life`
("conservation mode") stops charging at ~80%. There is no percentage register in
the ACPI tables or WMI interfaces (see `docs/hardware-notes.md`). `hold` works
around this: it charges in `Standard` mode until the target is reached, then
switches to `Long_Life`, which stops charging **without** discharging back to 80%.
Charging resumes once the battery drops `--hysteresis` points below the target.

**Verified 2026-10-03** (kernel 7.2.8): charged to 83% in `Standard`, switched to
`Long_Life` on AC. Charge power fell to 0 W within ~75 s and the battery held at
83% (energy unchanged) for the full 10-minute observation.

Limits below 80% are not possible through any known firmware interface.

## Requirements

- Kernel exposing `/sys/class/power_supply/BAT0/charge_types` (provided by
  `ideapad_laptop`; tested on 7.2.8-200.fc44).
- Python ≥ 3.12, standard library only.
- **Root** for `mode` and `hold` (not for `status` or `--dry-run`).

## Usage

```
usage: battery-threshold [-h] [-v] [--battery BATTERY] {status,mode,hold} ...

  status   show battery state and charge mode (default)
  mode     set the firmware charge mode: standard | long-life | fast
  hold     hold charge at a target percentage (80-100)

hold options:
  --target N        stop charging at N%
  --hysteresis N    resume charging once N points below target (default: 2)
  --interval S      seconds between checks (default: 60)
  --once            check once and exit
  --dry-run         log decisions, change nothing
```

## Examples

```bash
./battery_threshold.py                                 # status
./battery_threshold.py hold --target 90 --once --dry-run
sudo ./battery_threshold.py mode long-life             # firmware 80% cap
sudo ./battery_threshold.py mode standard              # charge to 100%
```

### Run as a service

```bash
sudo install -m 755 battery_threshold.py /usr/local/bin/battery-threshold
sudo install -m 644 battery-threshold.service /etc/systemd/system/
echo 'TARGET=85' | sudo tee /etc/default/battery-threshold
sudo systemctl daemon-reload
sudo systemctl enable --now battery-threshold
journalctl -u battery-threshold -f
```

Change the target by editing `/etc/default/battery-threshold` and running
`sudo systemctl restart battery-threshold`. Re-run the `install` line after
pulling updates to the script.

To uninstall: `sudo systemctl disable --now battery-threshold`, remove the two
installed files, then choose a mode with `mode long-life` or `mode standard`.

## Safety notes

- Only writes to `charge_types`, a standard kernel interface; no EC/ACPI pokes.
- The mode is only written when it needs to change, not on every check.
- When the service stops it leaves the current mode as-is. If it stopped while
  in `Standard`, the battery will charge to 100%.
- **Don't combine with another charge limiter.** Turn off KDE's battery
  protection (System Settings → Power Management) or UPower's charge threshold,
  and TLP's `STOP_CHARGE_THRESH_BAT0`. They write the same file and will fight.
- `Fast` (rapid charge) is not used by `hold`; it can't be combined with `Long_Life`.
