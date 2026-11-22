# Skyward on the OPPO A9x (PCEM00)

This fork ports [Skyward](../README.md) to the OPPO A9x: a MediaTek MT6771
phone (MT6771V/CT, MT6631 connectivity) built on the Android 9 era layout the
OPPO A9 shares, since the two phones are the same hardware inside.

## What the phone is

| | |
| --- | --- |
| Model | PCEM00, sold as OPPO A9x |
| SoC | MediaTek MT6771 (Helio P70) |
| Display | 1080×2340, 6.5 in, backlight on `/sys/class/leds/lcd-backlight` |
| Touch | MediaTek `touchpanel`, evdev protocol B |
| Layout | A-only, one recovery partition (64 MiB), one boot partition, **no super** |
| Bootloader message | at the start of `para`, not `misc` |
| Boot image | Android boot image header v2, gzip kernel and ramdisk, AVB hash footer |
| Firmware | vendor partition, Android 11 (SDK 30), keymaster@3.0-service.trustonic |
| Installed system | Android 16 (LineageOS 22/23 port), system-as-root erofs |

## How the image is built

The stock recovery partition image is kept in `stock/pcem00/recovery.img` (the
TWRP the phone came with, whose ramdisk supplies the recovery environment:
init, adbd, SELinux policy and libraries). `tools/pack_recovery.py` rebuilds
the v2 image around a new ramdisk and appends the unsigned AVB hash footer the
way `crates/skyward-pack` does for GKI images, because the bootloader here
reads no v3/v4 header and no LZ4 ramdisk.

`.github/workflows/build.yml` builds the recovery service and the system init
on GitHub Actions and packs `skyward-pcem00.img`, zipped as
`skyward-pcem00.zip`. Flash it whole:

```sh
adb reboot recovery            # or hold volume down and power
# from the recovery, as root:  dd if=/dev/zero of=... (see below)
fastboot flash recovery skyward-pcem00.img
```

With no fastboot at hand the recovery partition can also be written from a
root shell of the running system:

```sh
adb root
adb push skyward-pcem00.img /data/local/tmp/
adb shell "dd if=/data/local/tmp/skyward-pcem00.img of=/dev/block/by-name/recovery bs=1M"
adb reboot recovery
```

## What the port changes

* **ramdisk room** — LK reserves 0x2000000 bytes for the ramdisk at
  0x55000000 and resets the phone when a bigger one runs into its own memory
  at 0x57000000. The ramdisk is kept under that: the small DroidSans fonts
  instead of MiSansVF, TWRP's `twres`, `busybox`, `bash` and `nano` dropped,
  and the binaries stripped.
* **misc** — `bcb::misc()` falls back to `/dev/block/by-name/para`.
* **no slots** — the service reads `skyward_vab::current_slot()` as
  `Option<Slot>`; a phone without slots mounts `system`, `vendor`, `odm`
  (and `system_ext`, `product` where they exist) from `by-name` and reads the
  build properties from there. `/system` stays the recovery's ramdisk, the
  installed system goes to `/mnt/system`, and `vendor` is mounted where the
  ramdisk's `/vendor` link points (`/system/vendor`).
* **table** — `skyward-block`'s fstab reads every `fstab.*` the vendor and odm
  partitions carry, not only `fstab.qcom`, so MediaTek's `fstab.mt6771` is
  found.
* **display** — the phone has no DRM device, only `/dev/graphics/fb0`; a
  `FbSurface` backend draws the pages into the framebuffer's virtual screen
  and pans, and the backlight is found among the LEDs.
* **firmware fallback** — the 4.14 kernel has no
  `firmware_config/force_sysfs_fallback` switch, so writing it is not fatal.
* **no service directory** — a recovery has no framework `servicemanager`; the
  description publishing failure is logged and the programs start anyway.
* **WLAN** — MediaTek's chip is brought up by `wmt_loader`, `wmt_launcher` and
  `wlan_assistant` (listed as `[[service]]`s), powered on by writing
  `/dev/wmtWifi`, and joined by the vendor's own
  `/vendor/bin/hw/wpa_supplicant` through `skyward-net`'s control of it. No
  TWRP script is involved.
* **no super** — `snapshot_state()` is `None` on a phone without a super
  partition, so the home page does not warn about an update it cannot read.

## What does not work yet

* **Decrypting the data partition.** Skyward implements FBE itself, but it
  speaks the AIDL `android.hardware.security.keymint.IKeyMintDevice` service
  only. This phone's vendor is Android 11 with HIDL
  `android.hardware.keymaster@3.0-service.trustonic` (TWRP decrypted through
  its own script: gatekeeper@1.0, keymaster@3.0 and `keystore`), so no key
  service is found and the data partition stays closed. Supporting this
  vendor would mean adding a HIDL keymaster 3.0 client to the crypto stack.
* **A/B features** — backup and restore of the system through super, dual
  boot, slot switching and OTA merging do not apply: the phone has one slot
  and no super. Filling the pages remains possible; installing zips by
  script and flashing partition images still work.

## Testing on the phone

adb is a root shell inside Skyward (`ro.secure=0`, `ro.adb.secure=0`). The
device the port was developed on:

```sh
adb shell 'sendevent /dev/input/event2 3 47 0; sendevent /dev/input/event2 3 57 1; \
  sendevent /dev/input/event2 1 330 1; sendevent /dev/input/event2 3 53 540; \
  sendevent /dev/input/event2 3 54 600; sendevent /dev/input/event2 0 0 0; sleep 0.1; \
  sendevent /dev/input/event2 3 57 -1; sendevent /dev/input/event2 1 330 0; sendevent /dev/input/event2 0 0 0'
```

taps the middle of the screen; `/dev/graphics/fb0` can be read to look at
what is drawn.
