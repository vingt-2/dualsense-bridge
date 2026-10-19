# DualSense Bridge

Use a **PS5 DualSense over Bluetooth** on Windows and have games treat it as a **wired** controller,
including **HD haptics, adaptive triggers**, rumble, lightbar and LEDs.

Many PC games only enable the DualSense's special features when the controller is plugged in with a cable.
DualSense Bridge sits in the notification tray and presents your Bluetooth controller to Windows as a
virtual USB DualSense that looks like the real wired one, so those games enable everything.

| Feature | Over Bluetooth with DualSense Bridge |
|---|---|
| Buttons, sticks, touchpad, motion sensors | ✅ |
| Rumble | ✅ (works with old and new controller firmware) |
| Adaptive triggers | ✅ |
| Lightbar, player LEDs, mic LED | ✅ |
| HD haptics (e.g. Assassin's Creed Mirage) | ✅ ~50 ms delay by default, adjustable |
| Controller speaker and microphone | ❌ not bridged |

Tested with Assassin's Creed Mirage on Windows 11. Not affiliated with or endorsed by Sony.

## Install

1. Pair your DualSense with Windows over Bluetooth (hold **PS + Create** until the lightbar flashes,
   then add it in *Settings → Bluetooth & devices*).
2. Download **`DualSenseBridge-Setup-<version>.exe`** from the [latest release](https://github.com/vingt-2/dualsense-bridge/releases/latest) and run it.
3. Setup also installs two free drivers if you don't have them, by downloading their latest official
   release and opening their own installer:
   - [**usbip-win2**](https://github.com/vadimgrn/usbip-win2): provides the virtual USB port the
     emulated controller is plugged into (Microsoft-signed drivers).
   - [**HidHide**](https://github.com/nefarius/HidHide): hides the Bluetooth controller from games so
     they only see the wired one.
4. Restart if asked to, then turn on your controller.

> The installer and app are not code-signed, so Windows SmartScreen may warn about them.
> Choose *More info → Run anyway*, or build them yourself from source (see below).

## Use

DualSense Bridge runs in the notification tray (it asks for admin rights, which it needs to attach
the virtual controller and to configure HidHide). The icon shows the state:

- **Gray**: waiting for the controller to connect over Bluetooth
- **Amber**: connecting
- **Blue**: active, games see a wired DualSense

Right-click the icon for:

- **Haptics delay**: how much haptics data the controller buffers. *Balanced (~40 ms)* is the default;
  *Low* or *Lowest* feel more immediate but may stutter when the PC or Bluetooth is busy.
- **Hide Bluetooth controller from games** (HidHide), on by default.
- **Keep my default speaker and mic**: the virtual controller adds a "DualSense Wireless Controller"
  sound device, and Windows tends to make new sound devices the default. This puts yours back.
- **Start with Windows**, **Open log**, **Quit**.

### Troubleshooting

- **The game shows the controller twice, or as a generic controller**: make sure
  *Hide Bluetooth controller from games* is ticked, and try turning **Steam Input** off for that game.
- **Something else is wrong**: open the log (tray menu → *Open log*, or
  `%LOCALAPPDATA%\DualSenseBridge\bridge.log`) and include it in an
  [issue](https://github.com/vingt-2/dualsense-bridge/issues).

## How it works

```
DualSense ──Bluetooth──> DualSense Bridge ──USB/IP (127.0.0.1:3240)──> usbip-win2 virtual USB port ──> Windows & games
                         (HidHide hides the Bluetooth controller itself from games)
```

The bridge is a small USB/IP server that emulates a wired DualSense: the same USB descriptors
(including the audio interfaces used for haptics), the same HID reports, and the same feature
reports, captured from a real controller (see `reference/`). It translates between the wired and
Bluetooth report formats in both directions:

| Wired (USB) | Bluetooth |
|---|---|
| input `0x01` (64 B) | input `0x31` (78 B), payload offset +1 |
| output `0x02` (48 B): rumble, adaptive triggers, lightbar, player LEDs, mic LED | output `0x31` (78 B) + seq + CRC32 (seed `0xA2`) |
| feature GET (`0x05`, `0x09`, `0x20`, …) | same id, CRC (seed `0xA3`) blanked |
| feature SET (e.g. `0x08` from Steam) | acknowledged, **not** forwarded: it disconnects the controller over Bluetooth |
| haptics: USB audio, 4ch 48 kHz int16, channels 3–4 | output `0x32` (142 B): 32 frames of 3 kHz int8 stereo |

### Findings

These cost some debugging and are documented here in case they help others:

- **Haptics report layout** comes from [SAxense](https://github.com/egormanga/SAxense) (Linux).
  On Windows the report must be **142 bytes** (the size the controller's Bluetooth descriptor
  declares; Windows sends exactly that), not SAxense's 141, or the CRC lands in the wrong place.
- **Byte 5 of the `0x11` control sub-packet is the controller's haptics buffer depth**, roughly in ms.
  SAxense sends `0xFF`, which measured ~200 ms from write to vibration; `0x20` gives ~50 ms end to end,
  `0x08` ~25 ms, and `0x04` plays nothing. (Lead from [AnyPS5 #2056](https://github.com/boykopovar/AnyPS5/issues/2056).)
  Latencies were measured with the controller's own IMU, which picks up its vibration.
- Reports must be **32 frames**: shorter ones look fine in short bursts but make the controller pulse
  in a continuous stream.
- Reports must arrive **evenly paced**, which on Python 3.10 needs `timeBeginPeriod(1)` (the default
  15.6 ms Windows timer made a 5 ms sleep take 15.6 ms).
- **Restart the report counter at 0** after each pause; a continuing counter made every other effect
  play stretched to ~2x.
- After each effect, **keep sending silence for ~250 ms**. Without it short effects (menu clicks)
  never play, or play and then keep vibrating until the next effect.
- Treat the game's ±1 noise floor as silence (and convert int16 → int8 truncating toward zero),
  or the haptics stream never stops.
- The audio endpoints use **`bInterval=1`**. The real controller's `4` makes usbip-win2 never start
  the audio stream.
- **Rumble on newer firmware** (update version ≥ `0x0215`) needs `valid_flag2` bit 2 plus
  `valid_flag0` bit 1 (haptics select); older firmware uses `valid_flag0` bits 0–1. The bridge reads
  the firmware version and rewrites the flags, whichever style the game sent.

## Build from source

Requires Windows 10/11 x64, Python 3.10+ and [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`winget install JRSoftware.InnoSetup`).

```powershell
.\build.ps1 -Version 1.0.0
```

This runs the self-test (`tests\test_usbip_device.py`, needs no controller or drivers), builds
`dist\DualSenseBridge.exe` with PyInstaller and the installer `dist\DualSenseBridge-Setup-1.0.0.exe`.

To run from source instead: `pip install -r requirements.txt`, install usbip-win2 and HidHide, then
`python tray_app.py` from an elevated terminal.

| File | Purpose |
|---|---|
| `tray_app.py` | Tray app: attaches/detaches the virtual controller, HidHide, settings |
| `bridge.py` | Bluetooth side: reads/writes the real controller with hidapi |
| `dualsense_usb.py` | The emulated wired DualSense: descriptors, report translation |
| `usb_audio.py` | Its USB audio function (haptics in, silent mic out) |
| `haptics.py` | USB haptics audio → Bluetooth haptics reports, pacing |
| `usbip_server.py` | Minimal USB/IP server |
| `audio_default.py` | Restores Windows' default sound devices |
| `installer/` | Inno Setup script and the dependency downloader |

## Credits

- [SAxense](https://github.com/egormanga/SAxense) by Sdore: DualSense Bluetooth haptics research.
- [usbip-win2](https://github.com/vadimgrn/usbip-win2) and [HidHide](https://github.com/nefarius/HidHide),
  which do the driver-level work.
- [hidapi](https://github.com/trezor/cython-hidapi), [pystray](https://github.com/moses-palmer/pystray),
  [Pillow](https://python-pillow.org/), [PyInstaller](https://pyinstaller.org/).
- The Linux `hid-playstation` driver, for the DualSense report layouts and rumble flags.

## License

[MIT](LICENSE)
