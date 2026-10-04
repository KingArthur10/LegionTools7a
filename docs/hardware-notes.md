# Hardware notes

Findings about the Legion 7a / Strix Halo under Fedora: sysfs paths, driver
behaviour, kernel-version quirks. Record the kernel version alongside each note.

<!-- Example:
## Platform profile (kernel 7.2.8)
`/sys/firmware/acpi/platform_profile_choices` → `low-power balanced performance`
-->

## IR camera / face unlock (kernel 7.2.8, 2026-10-04)

- The integrated camera (USB `30c9:011c`) has two UVC functions: RGB on
  `video0`/`video1`, IR on `video2`/`video3`. The IR stream is 640×360 8-bit
  greyscale (`GREY`, UVC GUID for D3DFMT_L8).
- Stable IR path: `/dev/v4l/by-path/pci-0000:c3:00.4-usb-0:1:1.2-video-index0`
  (the `by-id` name embeds the camera serial; don't commit it).
- The IR emitter works without `linux-enable-ir-emitter` (its `configure` reports
  "emitter is already working"). Frames alternate emitter on/off: mean brightness
  ~45 lit vs ~6 unlit, so a single grabbed frame can look black.
- dlib 20.0.1 (CPU, OpenBLAS) on this machine: CNN face detection ~0.95 s per
  frame; Howdy end-to-end match ~0.8 s.
- polkit 127 runs `polkit-agent-helper-1` setuid, but for an unconfined user it
  stays in `unconfined_t`, so Howdy needs no SELinux policy changes.
