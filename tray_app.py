"""DualSense Bridge tray app.

Runs the BT -> wired bridge in the background and handles the plumbing:
  * attaches the virtual wired controller (usbip-win2) when the DualSense connects,
    and detaches it when the DualSense disconnects
  * hides the Bluetooth controller from games with HidHide while running
  * optional start-with-Windows (scheduled task, runs elevated)
"""
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import pystray
from PIL import Image, ImageDraw

import audio_default
import cursor
import keyboard
import quality_window
import shortcut_window
import updater
from bridge import BluetoothDualSense
from dualsense_usb import WiredDualSense
from actions import ACTIONS, ActionError
from shortcut_window import Shortcut
from shortcuts import ShortcutEngine, decode
from usbip_server import UsbIpServer

APP = "DualSense Bridge"
TASK = "DualSenseBridge"
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", "."), "DualSenseBridge")
LOG_PATH = os.path.join(DATA_DIR, "bridge.log")
USBIP_URL = "https://github.com/vadimgrn/usbip-win2/releases"
HIDHIDE_URL = "https://github.com/nefarius/HidHide/releases"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
QUIT_EVENT = "Local\\DualSenseBridgeQuit"

# Controller haptics buffer (byte 5 of the BT haptics control packet, ~ms). Smaller is
# more responsive but has less slack for Bluetooth/PC hiccups. Delays measured via IMU.
HAPTICS_DEPTHS = [
    ("Lowest (~16 ms, may stutter)", 0x08),
    ("Low (~30 ms)", 0x10),
    ("Balanced (~40 ms)", 0x20),
    ("Safe (~70 ms)", 0x40),
]


def _load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_settings(update):
    settings = _load_settings()
    settings.update(update)
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError as e:
        print(f"[tray] could not save settings: {e}")


def _find_tool(name, *dirs):
    found = shutil.which(name)
    if found:
        return found
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _run(*cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[tray] {cmd[0]} failed: {e}")
        return 1, ""
    out = (r.stdout + r.stderr).strip()
    print(f"[tray] {os.path.basename(cmd[0])} {' '.join(cmd[1:])} -> {r.returncode} {out}")
    return r.returncode, out


def _hid_instance_id(hid_path):
    r"""\\?\HID#{...}_VID&..#c&29..&0000#{guid}  ->  HID\{...}_VID&..\C&29..&0000"""
    p = hid_path.decode() if isinstance(hid_path, bytes) else hid_path
    return "\\".join(p.replace("\\\\?\\", "").split("#")[:3]).upper()


def _make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 16, 58, 44), radius=12, fill=color)    # body
    d.ellipse((4, 26, 26, 58), fill=color)                         # left grip
    d.ellipse((38, 26, 60, 58), fill=color)                        # right grip
    d.ellipse((16, 24, 26, 34), fill=(255, 255, 255, 230))         # sticks
    d.ellipse((38, 24, 48, 34), fill=(255, 255, 255, 230))
    return img


ICONS = {
    "waiting": _make_icon((140, 140, 140, 255)),
    "connected": _make_icon((230, 170, 30, 255)),
    "wired": _make_icon((40, 140, 255, 255)),
}


# Shortcuts for a fresh install (until the user saves their own). The cycles use whatever
# playback devices and displays are connected when they run.
DEFAULT_SHORTCUTS = [
    {"buttons": ["PS"], "presses": 1, "action": "hide_cursor",
     "params": {"park": True, "show_on_move": True}},
    {"buttons": ["Create", "PS"], "presses": 1, "action": "window_switcher", "params": {}},
    {"buttons": ["Options", "PS"], "presses": 1, "action": "audio_cycle",
     "params": {"all": True, "devices": []}},
    {"buttons": ["Create", "Options"], "presses": 1, "action": "display_cycle",
     "params": {"all": True, "displays": []}, "hold_time": 1.0},
]


class TrayApp:
    def __init__(self, server, bt):
        self.server, self.bt = server, bt
        self.usbip = _find_tool("usbip.exe", r"C:\Program Files\USBip")
        self.hidhide = _find_tool(
            "HidHideCLI.exe", r"C:\Program Files\Nefarius Software Solutions\HidHide\x64")
        self.hide_enabled = self.hidhide is not None
        self.keep_audio = True
        self.check_updates = _load_settings().get("check_updates", True)
        self.update = None                 # updater.Release newer than this build, if any
        self._updating = False
        # Like a PS5: headphones in the controller get all audio (its speaker only plays
        # what's sent to the controller, e.g. a game's speaker effects).
        self.headset_output = _load_settings().get("headset_default_output", True)
        self._headset_saved = None         # (render defaults before, controller endpoint)
        self._playback_chosen = threading.Event()
        server.device.on_headset = lambda plugged: threading.Thread(
            target=self._on_headset, args=(plugged,), daemon=True).start()
        # Bluetooth controllers (HID instance ids) seen before; hidden at startup so a
        # game that is already running never sees the controller when it reconnects.
        self.known_ids = set(_load_settings().get("controller_ids", []))
        self.hidden_ids = set()
        self._hide_lock = threading.Lock()
        bt.before_open = lambda path: self._hide_ids({_hid_instance_id(path)})
        settings = _load_settings()
        if "audio_shortcuts" in settings and "shortcuts" not in settings:   # from 1.3.0
            settings["shortcuts"] = [
                {"buttons": d["buttons"], "presses": d["presses"], "action": "audio_output",
                 "params": {"devices": d["devices"]}} for d in settings["audio_shortcuts"]]
            settings["shortcut_notify"] = settings.get("audio_shortcut_notify", True)
            _save_settings({k: settings[k] for k in ("shortcuts", "shortcut_notify")})
        self.shortcuts = [Shortcut.from_json(d) for d in settings.get("shortcuts", DEFAULT_SHORTCUTS)]
        self.shortcut_notify = settings.get("shortcut_notify", True)
        self.shortcut_engine = ShortcutEngine(self._run_shortcut)
        self.shortcut_engine.set_shortcuts(self.shortcuts)
        bt.on_buttons = lambda *b: self.shortcut_engine.update(decode(*b))
        self.port = None
        self.state = "waiting"
        self._startup = None
        self._lock = threading.Lock()
        bt.on_state = lambda up: threading.Thread(target=self._on_bt_state, args=(up,), daemon=True).start()

        self.icon = pystray.Icon(TASK, ICONS["waiting"], APP, menu=pystray.Menu(
            pystray.MenuItem(lambda _: self._status_text(), None, enabled=False),
            pystray.MenuItem(lambda _: f"Update to {self.update.version}…" if self.update else "",
                             self._install_update, visible=lambda _: self.update is not None,
                             enabled=lambda _: not self._updating),
            pystray.MenuItem("Connection quality…", lambda: quality_window.show(self.bt)),
            pystray.MenuItem("Turn off controller", self._power_off,
                             enabled=lambda _: self.state != "waiting"),
            pystray.MenuItem("Shortcuts…", lambda: shortcut_window.show(self)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Hide Bluetooth controller from games", self._toggle_hide,
                             checked=lambda _: self.hide_enabled,
                             enabled=lambda _: self.hidhide is not None),
            pystray.MenuItem("Haptics delay", pystray.Menu(*[
                pystray.MenuItem(label, self._depth_setter(depth), radio=True,
                                 checked=lambda _, d=depth: self.bt.haptics_depth == d)
                for label, depth in HAPTICS_DEPTHS])),
            pystray.MenuItem("Keep my default speaker and mic", self._toggle_keep_audio,
                             checked=lambda _: self.keep_audio),
            pystray.MenuItem("Route all PC audio to the controller's headphone jack when plugged in",
                             self._toggle_headset_output,
                             checked=lambda _: self.headset_output),
            pystray.MenuItem("Start with Windows", self._toggle_startup,
                             checked=lambda _: self._startup_enabled()),
            pystray.MenuItem("Open log", lambda: os.startfile(LOG_PATH)),
            pystray.MenuItem("Updates", pystray.Menu(
                pystray.MenuItem("Check for updates automatically", self._toggle_check_updates,
                                 checked=lambda _: self.check_updates),
                pystray.MenuItem("Check now", lambda: threading.Thread(
                    target=self._check_for_update, args=(True,), daemon=True).start()),
                pystray.MenuItem(lambda _: f"Version {updater.VERSION or 'from source'}", None,
                                 enabled=False))),
            pystray.MenuItem("Install usbip-win2…", lambda: webbrowser.open(USBIP_URL),
                             visible=lambda _: self.usbip is None),
            pystray.MenuItem("Install HidHide…", lambda: webbrowser.open(HIDHIDE_URL),
                             visible=lambda _: self.hidhide is None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        ))

    # -- status -------------------------------------------------------------
    def _status_text(self):
        if self.usbip is None:
            return "usbip-win2 not installed"
        return {"waiting": "Waiting for DualSense (Bluetooth)…",
                "connected": "Connected – attaching…",
                "wired": "Active – showing as wired controller"}[self.state]

    def _set_state(self, state):
        self.state = state
        self.icon.icon = ICONS[state]
        self.icon.title = f"{APP}: {self._status_text()}"
        self.icon.update_menu()

    # -- controller connect / disconnect ------------------------------------
    def _on_bt_state(self, up):
        with self._lock:
            if up:
                self._set_state("connected")
                defaults = self._audio_defaults()
                if self._attach():
                    if defaults:
                        self._playback_chosen.clear()
                        threading.Thread(target=self._guard_audio, args=(defaults,), daemon=True).start()
                    self._set_state("wired")
                    self.icon.notify("DualSense is now showing as a wired controller.", APP)
            else:
                # The controller stays hidden while the bridge runs: un-hiding it here
                # let a running game grab it on reconnect, and the game's setup
                # (feature 0x08 over Bluetooth) knocked it off again, in a loop.
                self.shortcut_engine.update(frozenset())   # let go of any held shortcut
                self._detach()
                self._set_state("waiting")
                threading.Thread(target=self._restore_headset_output, daemon=True).start()

    def _attach(self):
        if self.usbip is None:
            self.icon.notify("usbip-win2 is not installed – right-click the tray icon to get it.", APP)
            return False
        if self.port is not None:
            return True
        code, out = _run(self.usbip, "attach", "-r", "127.0.0.1", "-b", self.server.device.busid)
        if code != 0:
            self.icon.notify("Could not attach the virtual controller – see the log.", APP)
            return False
        m = re.search(r"port\s+(\d+)", out, re.I)
        self.port = int(m.group(1)) if m else -1
        return True

    def _detach(self):
        if self.port is None or self.usbip is None:
            return
        if self.port > 0:
            _run(self.usbip, "detach", "-p", str(self.port))
        else:
            _run(self.usbip, "detach", "--all")
        self.port = None

    def _power_off(self):
        if not self.bt.power_off():
            self.icon.notify("Could not turn off the controller – see the log.", APP)

    # -- controller shortcuts ---------------------------------------------------
    def save_shortcuts(self, shortcuts, notify):
        self.shortcuts, self.shortcut_notify = shortcuts, notify
        self.shortcut_engine.set_shortcuts(shortcuts)
        _save_settings({"shortcuts": [sc.to_json() for sc in shortcuts], "shortcut_notify": notify})

    def _run_shortcut(self, shortcut, phase):
        name = shortcut.trigger()
        action = ACTIONS.get(shortcut.action)
        try:
            if action is None:
                raise ActionError(f"Unknown action {shortcut.action!r}; is DualSense Bridge up to date?")
            if phase == "repeat":
                return action.repeat(shortcut.params)
            if phase == "end":
                return action.end(shortcut.params)
            message = action.run(shortcut.params)
        except ActionError as e:
            print(f"[shortcut] {name}: {e}")
            self.icon.notify(str(e), APP)
            return
        print(f"[shortcut] {name}: {message or action.name}")
        if self.shortcut_notify and message:
            self.icon.notify(message, APP)

    # -- default audio devices --------------------------------------------------
    def _audio_defaults(self):
        if not self.keep_audio:
            return None
        try:
            return audio_default.get_defaults()
        except OSError as e:
            print(f"[audio] could not read default devices: {e}")
            return None

    def _guard_audio(self, defaults):
        try:
            audio_default.guard_defaults(defaults, playback_free=self._playback_chosen)
        except OSError as e:
            print(f"[audio] could not restore default devices: {e}")

    # -- controller headphones as the default output ------------------------------
    def _toggle_headset_output(self):
        self.headset_output = not self.headset_output
        _save_settings({"headset_default_output": self.headset_output})
        if self.headset_output:
            threading.Thread(target=self._on_headset, args=(self.bt.headset,), daemon=True).start()
        else:
            threading.Thread(target=self._restore_headset_output, daemon=True).start()

    def _controller_output(self, wait=0):
        """The virtual controller's playback endpoint id; it shows up a few seconds after
        the controller connects, so this can wait for it."""
        end = time.monotonic() + wait
        while True:
            for endpoint_id, name in audio_default.list_outputs():
                if "DualSense Wireless Controller" in name:
                    return endpoint_id
            if time.monotonic() >= end:
                return None
            time.sleep(0.5)

    def _on_headset(self, plugged):
        if not plugged:
            self._restore_headset_output()
            return
        if not self.headset_output or self._headset_saved:
            return
        try:
            endpoint = self._controller_output(wait=20)
            if endpoint is None:
                print("[audio] controller headphones: no controller sound device")
                return
            current = audio_default.get_defaults()
            saved = {role: current[(audio_default.E_RENDER, role)] for role in audio_default.ROLES
                     if (audio_default.E_RENDER, role) in current}
            self._playback_chosen.set()    # the attach guard must not switch it back
            self._headset_saved = (saved, endpoint)
            audio_default.set_output(endpoint)
        except OSError as e:
            print(f"[audio] could not switch to the controller headphones: {e}")
            return
        print("[audio] headphones plugged in: PC audio -> controller")
        self.icon.notify("Headphones plugged into the controller: PC audio plays there.", APP)

    def _restore_headset_output(self):
        """Put back the default output from before the headphones were plugged in, unless
        the user picked another one meanwhile."""
        if not self._headset_saved:
            return
        (saved, endpoint), self._headset_saved = self._headset_saved, None
        try:
            outputs = dict(audio_default.list_outputs())
            current = audio_default.get_defaults().get((audio_default.E_RENDER, 0))
            if current != endpoint and endpoint in outputs:
                return   # the user chose another output while the headphones were in
            for role, endpoint_id in saved.items():
                if endpoint_id in outputs:
                    audio_default.set_default(endpoint_id, role)
        except OSError as e:
            print(f"[audio] could not restore the default output: {e}")
            return
        print("[audio] headphones unplugged: PC audio back to the previous output")

    # -- HidHide --------------------------------------------------------------
    def _hide_ids(self, ids):
        """Hide these Bluetooth controllers from everything but the bridge."""
        if not (self.hide_enabled and self.hidhide):
            return
        with self._hide_lock:
            new = set(ids) - self.hidden_ids
            if not new:
                return
            if not self.hidden_ids:
                _run(self.hidhide, "--app-reg", sys.executable)
            for instance_id in new:
                _run(self.hidhide, "--dev-hide", instance_id)
            _run(self.hidhide, "--cloak-on")
            self.hidden_ids |= new
            if not new <= self.known_ids:
                self.known_ids |= new
                _save_settings({"controller_ids": sorted(self.known_ids)})

    def _unhide_all(self):
        if not self.hidhide:
            return
        with self._hide_lock:
            for instance_id in self.hidden_ids:
                _run(self.hidhide, "--dev-unhide", instance_id)
            self.hidden_ids.clear()

    def _depth_setter(self, depth):
        def set_depth():
            self.bt.haptics_depth = depth
            _save_settings({"haptics_depth": depth})
            print(f"[tray] haptics buffer depth set to 0x{depth:02x}")
        return set_depth

    def _toggle_keep_audio(self):
        self.keep_audio = not self.keep_audio

    def _toggle_hide(self):
        self.hide_enabled = not self.hide_enabled
        if self.hide_enabled:
            current = {_hid_instance_id(self.bt.path)} if self.bt.path else set()
            self._hide_ids(self.known_ids | current)
        else:
            self._unhide_all()

    # -- updates ------------------------------------------------------------------
    UPDATE_FIRST_CHECK = 60          # s after startup
    UPDATE_INTERVAL = 24 * 3600

    def _update_loop(self):
        time.sleep(self.UPDATE_FIRST_CHECK)
        while True:
            if self.check_updates:
                self._check_for_update()
            time.sleep(self.UPDATE_INTERVAL)

    def _toggle_check_updates(self):
        self.check_updates = not self.check_updates
        _save_settings({"check_updates": self.check_updates})

    def _check_for_update(self, manual=False):
        if updater.VERSION is None:
            if manual:
                self.icon.notify("Running from source: no version to compare.", APP)
            return
        try:
            release = updater.latest()
        except (OSError, ValueError) as e:
            print(f"[update] check failed: {e}")
            if manual:
                self.icon.notify("Could not check for updates – see the log.", APP)
            return
        if not updater.newer(release):
            print(f"[update] up to date ({updater.VERSION})")
            if manual:
                self.icon.notify(f"DualSense Bridge {updater.VERSION} is up to date.", APP)
            return
        self.update = release
        self.icon.update_menu()
        print(f"[update] {release.version} is available ({release.page})")
        # Tell once per version (or whenever asked), not at every daily check.
        if manual or _load_settings().get("update_notified") != release.version:
            _save_settings({"update_notified": release.version})
            self.icon.notify(f"DualSense Bridge {release.version} is available. "
                             "Right-click the tray icon to update.", APP)

    def _install_update(self):
        if self._updating or self.update is None:
            return
        self._updating = True
        self.icon.update_menu()
        threading.Thread(target=self._download_and_install, args=(self.update,), daemon=True).start()

    def _download_and_install(self, release):
        self.icon.notify(f"Downloading DualSense Bridge {release.version}…", APP)
        try:
            path = updater.download(release)
            print(f"[update] installing {release.version} from {path}")
            updater.install(path)      # the installer quits this app and starts the new one
        except OSError as e:
            print(f"[update] failed: {e}")
            self.icon.notify("The update failed – see the log. You can also download it "
                             "from GitHub.", APP)
            webbrowser.open(release.page)
            self._updating = False
            self.icon.update_menu()

    # -- start with Windows ---------------------------------------------------
    def _startup_enabled(self):
        if self._startup is None:
            # Not logged: it runs at every start and only answers whether the task exists.
            try:
                self._startup = subprocess.run(["schtasks", "/query", "/tn", TASK], capture_output=True,
                                               timeout=30, creationflags=NO_WINDOW).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                self._startup = False
        return self._startup

    def _toggle_startup(self):
        if self._startup_enabled():
            _run("schtasks", "/delete", "/tn", TASK, "/f")
        else:
            target = f'"{sys.executable}"' if getattr(sys, "frozen", False) \
                else f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            _run("schtasks", "/create", "/tn", TASK, "/tr", target,
                 "/sc", "onlogon", "/rl", "highest", "/f")
        self._startup = None
        self.icon.update_menu()

    # -- lifecycle ------------------------------------------------------------
    def _quit(self):
        with self._lock:
            self._detach()
        self._unhide_all()
        self._restore_headset_output()
        if cursor.is_hidden():
            cursor.restore()
        keyboard.release_all()
        self.icon.stop()
        os._exit(0)

    def _wait_for_quit_request(self):
        """`DualSenseBridge.exe --quit` (used by the uninstaller) signals this event."""
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = ctypes.c_void_p
        event = k32.CreateEventW(None, True, False, QUIT_EVENT)
        k32.WaitForSingleObject(ctypes.c_void_p(event), 0xFFFFFFFF)
        print("[tray] quit requested")
        self._quit()

    def run(self):
        def setup(icon):
            icon.visible = True
            if self.usbip is None:
                icon.notify("usbip-win2 is required – right-click the tray icon to install it.", APP)
            self._hide_ids(self.known_ids)   # before the controller can (re)connect
            cursor.restore()   # in case a previous run exited with the cursor hidden
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            threading.Thread(target=self.bt.run, daemon=True).start()
            threading.Thread(target=self._wait_for_quit_request, daemon=True).start()
            threading.Thread(target=self._update_loop, daemon=True).start()

        self.icon.run(setup=setup)


class _Log:
    """Timestamped log file (the windowed exe has no console)."""

    def __init__(self, path):
        self.f = open(path, "a", buffering=1, encoding="utf-8")

    def write(self, s):
        if s.strip():
            self.f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + s.rstrip("\n") + "\n")

    def flush(self):
        self.f.flush()


def request_quit(timeout=10):
    """Ask a running instance to quit cleanly (detach, un-hide) and wait for it."""
    k32 = ctypes.windll.kernel32
    k32.OpenEventW.restype = ctypes.c_void_p
    event = k32.OpenEventW(0x0002, False, QUIT_EVENT)  # EVENT_MODIFY_STATE
    if not event:
        return  # not running
    k32.SetEvent(ctypes.c_void_p(event))
    end = time.monotonic() + timeout
    while time.monotonic() < end:  # it has exited once its USB/IP port is free again
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 3240))
            return
        except OSError:
            time.sleep(0.2)
        finally:
            s.close()


def main():
    if "--quit" in sys.argv:
        request_quit()
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    sys.stdout = sys.stderr = _Log(LOG_PATH)

    bt = BluetoothDualSense()
    depth = _load_settings().get("haptics_depth")
    if depth in [d for _, d in HAPTICS_DEPTHS]:
        bt.haptics_depth = depth
    server = UsbIpServer(WiredDualSense(bt))
    try:
        server.bind()
    except OSError:
        ctypes.windll.user32.MessageBoxW(None, f"{APP} is already running.", APP, 0x40)
        return
    TrayApp(server, bt).run()


if __name__ == "__main__":
    main()
