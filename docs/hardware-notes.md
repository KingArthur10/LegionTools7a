# Hardware notes

Findings about the Legion 7a / Strix Halo under Fedora: sysfs paths, driver
behaviour, kernel-version quirks. Record the kernel version alongside each note.

<!-- Example:
## Platform profile (kernel 7.2.8)
`/sys/firmware/acpi/platform_profile_choices` → `low-power balanced performance`
-->

## Battery charge thresholds (kernel 7.2.8, BIOS UACN26WW, 2026-10-03)

**No arbitrary percentage threshold exists in firmware.** Checked the decompiled
DSDT + 33 SSDTs and the decoded WMI BMOF:

- `BAT0/charge_types` (`Fast` / `Standard` / `Long_Life`) is the only kernel
  interface; there is no `charge_control_end_threshold`. Writing needs root.
- ideapad `SBMC` sets single EC bits: `BTSM` (conservation, args 3/5), `QCHO`
  (rapid charge, 7/8), `CDMB` (0/1), `ESMC` (9/0x10). The ~80% cap lives in EC
  firmware; no ACPI field holds it.
- `LENOVO_OTHER_METHOD` (WMI `DC2A8805…`) battery features `0x03010001` → EC bit
  `EACS`, `0x03010002` → EC bit `ETCS`; both are 0/1 only. Their exact meaning
  is undocumented (kernel patch author: "sets a charge threshold of 80% and lowers
  the charge speed a bit").
- `LENOVO_REPORT_DBDC_DATA` (`129108C7…`) has a read-only "charge threshold 0–100"
  array paired with current/power limits. It looks like a discharge power table,
  not a stop threshold.
- UPower 1.91.4 reports `ChargeThresholdSupported=true` (maps to `Long_Life`),
  currently disabled. KDE's "battery protection" toggle drives it.
- Tools used: `iasl` (acpica-tools), `bmf2mof` from github.com/pali/bmfdec.
- **Hold test (verified):** `Long_Life` above 80% stops charging but does not
  discharge. At 83% on AC, power went 34 W → 0 W in ~75 s and capacity/energy
  stayed flat for 10 min. This is what `tools/battery-threshold` `hold` relies on.
