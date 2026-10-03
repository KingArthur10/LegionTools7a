# System specifications

Reference for the machine these tools target. Collected **2026-10-03** on kernel
**7.2.8-200.fc44**. Identifiers that are unique to this unit (serial numbers, MAC
addresses, UUIDs) are deliberately omitted.

> Kernel, firmware, and Mesa updates change paths and behaviour on Strix Halo.
> Re-check anything here on the live system before relying on it, and update this
> file (with the new kernel version) when something changes.

## Machine

| Item | Value |
| ---- | ----- |
| Model | Lenovo Legion 7 15ASH11 (machine type `83V9`) |
| Board | `LNVNB161216` |
| BIOS | Lenovo `UACN26WW` (2026-06-29) |
| Secure Boot | Enabled; TPM2 present |
| Dual boot | Windows on the second NVMe (BitLocker) — never touch `nvme0n1` |

## CPU

| Item | Value |
| ---- | ----- |
| Model | AMD Ryzen AI MAX+ 392 w/ Radeon 8060S (Strix Halo) |
| Cores / threads | 12 / 24 (2 CCDs, 32 MiB L3 each, 64 MiB total) |
| Cache | L1d 48 KiB/core, L1i 32 KiB/core, L2 1 MiB/core |
| Clock range | ~0.6 – 5.06 GHz |
| ISA highlights | AVX-512 (incl. BF16, VNNI), AVX-VNNI |
| Scaling driver | `amd-pstate-epp` (mode `active`) |
| Governor / EPP | `performance` / `performance` (at time of capture) |
| Temp sensor | `k10temp` (`Tctl`) |

## Memory

| Item | Value |
| ---- | ----- |
| Installed | 64 GiB unified LPDDR5X (62 GiB visible to the OS) |
| BIOS VRAM carve-out | 512 MiB |
| GTT (GPU-addressable system RAM) | 52 GiB, set via kernel arg `ttm.pages_limit=13631488` (4 KiB pages) |
| Swap | 8 GiB zram |

## GPU

| Item | Value |
| ---- | ----- |
| Device | Radeon 8060S (Strix Halo), PCI `1002:1586`, `gfx1151`, at `0000:c3:00.0` |
| Kernel driver | `amdgpu` → `/sys/class/drm/card1` |
| Vulkan | RADV `STRIX_HALO`, Mesa 26.2.3, Vulkan 1.4 |
| ROCm | `rocm-runtime` 7.1.1 and `hipcc` installed; `rocminfo`, `rocm-smi` and `amdsmi` **not** installed |
| DPM | `power_dpm_force_performance_level` = `auto`; shader clock 672–2900 MHz |
| Sensors | `amdgpu` hwmon: edge temp, PPT power (whole APU package), sclk, voltages |

## NPU

| Item | Value |
| ---- | ----- |
| Device | AMD XDNA 2 "NPU Strix Halo", PCI `1022:17f0`, at `0000:c4:00.1` |
| Kernel driver | `amdxdna` → `/dev/accel/accel0` |
| Firmware | 1.1.2.65 |
| Userspace | XRT / `xrt-plugin-amdxdna` **not** installed |

## Display

| Item | Value |
| ---- | ----- |
| Internal panel | `eDP-1`, 2560×1600 @ 165 Hz |
| Outputs | 1× HDMI, 7× DP connectors (USB4/USB-C alt-mode) on `card1` |
| Session | KDE Plasma 6.7.5 on Wayland |

## Power, thermal & platform

| Item | Value |
| ---- | ----- |
| Platform profile handlers | Two handlers: `platform-profile-0` = `amd-pmf` (low-power / balanced / performance), `platform-profile-1` = `lenovo-wmi-gamezone` (adds max-power and custom) |
| Legacy `/sys/firmware/acpi/platform_profile` | Showed `custom` while the two handlers disagreed (`performance` vs `balanced`) |
| Profile daemon | `tuned` + `tuned-ppd` (replaces `power-profiles-daemon`, which is not installed) |
| Lenovo firmware attributes | `/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes/` |
| Power limits (W, current [min–max]) | SPL `ppt_pl1_spl` 55 [40–125], sPPT `ppt_pl2_sppt` 70 [55–145], fPPT `ppt_pl3_fppt` 81 [66–165] |
| CPU temp limit (°C) | `cpu_temp` 100 [85–100] |
| Fans | `lenovo_wmi_other` hwmon `fan1_input` gives real RPM (range 1700–5700); `yogafan` hwmon (`ideapad_laptop`) reads 0 RPM on both fans |
| Lenovo drivers loaded | `lenovo_wmi_gamezone`, `lenovo_wmi_other`, `lenovo_wmi_capdata`, `lenovo_wmi_events`, `lenovo_wmi_hotkey_utilities`, `ideapad_laptop` |
| AMD platform drivers | `amd_pmf`, `amd_pmc`, `amd_sfh` |
| Battery | `BAT0`, Li-poly, 84 Wh design; `charge_types`: `Fast` / `Standard` / **`Long_Life`** (active) |
| AC / USB-C PD | `ADP0`, plus three `ucsi-source-psy-USBC000:00x` supplies |

## Storage

| Device | Model | Size | Use |
| ------ | ----- | ---- | --- |
| `nvme1n1` | Samsung PM9A1-family `MZVL21T0HCLR` | 1 TB | Fedora: EFI (vfat), `/boot` (ext4), btrfs (`root` + `home` subvolumes, `compress=zstd:1`) |
| `nvme0n1` | Samsung PM9C1a `MZAL81T0HFLB` (DRAM-less) | 1 TB | Windows (BitLocker) — do not modify |

## Other hardware

| Item | Device / driver |
| ---- | --------------- |
| Wi-Fi | MediaTek MT7925 (Wi-Fi 7, 2×2) — `mt7925e` |
| Bluetooth | MediaTek (USB `0e8d:e025`) |
| Audio | Realtek ALC287 codec (`snd_hda_intel`), AMD ACP audio coprocessor (`snd_pci_ps`), PipeWire 1.6.9 |
| Keyboard / RGB controller | ITE 8258 (USB `048d:c115`) |
| Touchpad | Synaptics `SYNA2BA6:00 06CB:CF00` (I²C HID) |
| Camera | Luxvisions Integrated Camera (USB `30c9:011c`) |
| Card reader | Realtek RTS525A — `rtsx_pci` |
| USB | 4× xHCI controllers, 2× USB4 host routers (`thunderbolt`) |

## Software

| Item | Value |
| ---- | ----- |
| OS | Fedora Linux 44 (KDE Plasma Desktop Edition) |
| Kernel | 7.2.8-200.fc44 (fallback 6.19.10-300.fc44 installed) |
| Kernel cmdline | `rhgb quiet ttm.pages_limit=13631488 rootflags=subvol=root` |
| SELinux | Enforcing |
| Firmware packages | `linux-firmware`, `amd-gpu-firmware`, `amd-ucode-firmware` 20260916 |
| systemd | 259.9 |
| Python | 3.14.7 (system) |
| Tooling present | `lm_sensors` 3.6.0, `fwupd` 2.1.8, `tuned` 2.28.0 |
