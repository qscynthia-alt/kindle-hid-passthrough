# PW5 Joy-Con stable manual workflow

This folder records the configuration that was verified on a Kindle Paperwhite 5
running firmware 5.18.6 and KOReader 2026.07.1 on 2026-09-04. It is a manual,
fail-safe setup: there is no HID autostart and no Kindle Button Mapper (KBM).

## Safety scope

- Do not install an Upstart Preston/autostart service for this build.
- Do not modify the Kindle boot chain, root filesystem, or system startup files.
- A missing page-turner is acceptable; a risky retry is not.
- Fresh API creation enters the MediaTek WMT Bluetooth path. On this test device,
  creating it before stock Bluetooth was genuinely initialized repeatedly caused
  the same kernel NULL dereference and watchdog reboot.
- Never repeat API creation after an error, missing dialog, unexpected reboot, or
  UI anomaly. Save diagnostics first.
- Entering USB drive mode removes `/mnt/us` from the Kindle runtime. It terminates
  the resident API and can race with Kindle framework applications. Safely eject,
  then allow the Library to settle before opening Kindle Tools.

## Included components

- Core commit `a4175a9`: avoids the extra `/dev/stpbt` availability open/close.
- `kindle-tools-unified/`: the verified one-action-per-launch Kindle Tools UI.
- `joycon-page-turner.koplugin/`: KOReader joystick input support.

The core change reduces one hazardous probe, but it does **not** fix the underlying
PW5 WMT kernel bug. A real transport open can still trigger it in an unwarmed state.

## Device configuration

Keep device-specific addresses and pairing keys on the Kindle; do not commit them.
`/mnt/us/kindle_hid_passthrough/devices.conf` must contain exactly two active lines:

```text
AA:BB:CC:DD:EE:FF ble KeyKey Mini BLE1
11:22:33:44:55:66 classic Joy-Con (R)
```

Replace the sample addresses with the addresses discovered for your devices. Set
`connect_timeout = 120` in `config.ini`. Preserve the existing `cache/` directory
and `pairing_keys.json` when upgrading UI or plugin files.

The included backend is published without the tested hardware addresses. Its legacy
one-shot pairing action therefore contains a sample `JOY_ADDR`; replace that sample
only if pairing a new Joy-Con. The verified post-pairing startup path reads addresses
from `devices.conf` and needs no source edit.

## File placement

This is an overlay for an already registered `com.local.kindtools` WAF installation.
Back up each destination before replacing it.

```text
kindle-tools-unified/launch.sh
  -> /mnt/us/Kindle_Tools/launch.sh
kindle-tools-unified/run.sh
  -> /mnt/us/Kindle_Tools/menu/run.sh
kindle-tools-unified/index.html
  -> /mnt/us/Kindle_Tools/v2-safe-restore/create-api-step/index.html
kindle-tools-unified/request.sh
  -> /mnt/us/Kindle_Tools/v2-safe-restore/create-api-step/request.sh
kindle-tools-unified/server.sh
  -> /mnt/us/Kindle_Tools/v2-safe-restore/create-api-step/server.sh
kindle-tools-unified/Kindle_Tools.sh
  -> /mnt/us/documents/Kindle_Tools.sh
joycon-page-turner.koplugin/
  -> /mnt/us/koreader/plugins/joycon-page-turner.koplugin/
```

`launch.sh` contains a hash gate for the exact previously installed WAF page. For
a different base installation, do not weaken the gate blindly: verify the installed
page, save it, and deliberately update `EXPECTED_OLD`.

After copying, wait for writes to finish, safely eject the Kindle, and wait until
the Library is responsive. Do not launch the HID binary while USB storage is mounted.

## Creating the API after a reboot or USB session

1. From Kindle Quick Settings, set Bluetooth to Off and wait for it to settle.
2. Set Bluetooth to On and wait until it is fully On. If it returns to Off, try only
   one more time after 30 seconds. If that also fails, stop; use one normal menu
   Restart, never a forced reboot.
3. After a normal restart, Bluetooth may already be On. Wait about 30 seconds.
4. Wake the Joy-Con so its LEDs blink.
5. Open Kindle Tools.
6. Select `1. Arm saved-device API`.
7. Within 60 seconds select `2. Create API for saved devices`, once only.
8. Wait for the result. On success, use `Status`; it should identify the connected
   saved device. Do not use Create again while port 8321 is resident.
9. Open a book in KOReader.

If API creation fails, produces no result, reboots the device, or leaves Bluetooth
abnormal, stop. Do not retry in the same runtime state.

## Verified page controls

For the tested right Joy-Con descriptor:

- physical A (`BtnA`) -> previous page
- physical X (`BtnB`) -> next page
- physical B (`BtnC`) -> no page action
- physical Y (`BtnX`) -> no page action

The plugin only opens an already-created KOReader joystick input node. It does not
start Bluetooth, create the HID API, install KBM, or modify system startup.

## Normal daily use

- Keep the API resident and avoid USB drive mode.
- Wake the Joy-Con, confirm `Joy-Con connected` in Kindle Tools only when needed,
  then enter KOReader.
- Use Start/Stop only against an already-present API. These actions are different
  from fresh API creation.
- Do not repeatedly leave KOReader merely to inspect status. The next development
  step is to expose safe status/control actions inside KOReader itself.

## Recovery and evidence

- If the Kindle UI remains responsive, do not force reboot.
- Save the Kindle Tools incident snapshot after any unexpected behavior.
- Preserve `/proc/last_kmsg` and `/sys/fs/pstore` after an automatic reboot.
- A `KPPMainAppV2` report produced exactly while entering/leaving USB mode can be a
  user-storage race; distinguish it from a kernel Oops by checking continuous uptime
  and current-boot kernel messages.
- Restore backed-up user-storage files if the UI/plugin overlay is damaged. No boot
  or rootfs rollback should be necessary because this setup changes neither.
