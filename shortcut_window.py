"""Audio shortcuts window: controller shortcuts that switch Windows' playback device.

Lives on the shared UI thread (see ui.py).
"""
import tkinter as tk
from dataclasses import dataclass, field

import audio_default
import ui
from shortcuts import describe
from ui import GRID, MUTED, PANEL, SURFACE, TEXT, TEXT_2, label

ERROR = "#d03b3b"
# The bridge doesn't carry the controller's speaker audio yet, so its sound device is silent.
HIDDEN_OUTPUTS = ("DualSense Wireless Controller",)


@dataclass
class AudioShortcut:
    buttons: list
    presses: int = 1
    devices: list = field(default_factory=list)   # [(endpoint id, name)], cycled in order

    def to_json(self):
        return {"buttons": sorted(self.buttons), "presses": self.presses,
                "devices": [list(d) for d in self.devices]}

    @classmethod
    def from_json(cls, d):
        return cls(list(d.get("buttons", [])), int(d.get("presses", 1)),
                   [tuple(x) for x in d.get("devices", [])])

    def target_text(self):
        names = [name for _, name in self.devices]
        if len(names) == 1:
            return f"Switches to {names[0]}"
        return "Cycles through " + ", ".join(names)


_window = None


def show(app):
    """app: the tray app (audio_shortcuts, audio_notify, save_audio_shortcuts, shortcut_engine, bt)."""
    ui.call(lambda: _show(app))


def _show(app):
    global _window
    if _window is not None and _window.root.winfo_exists():
        ui.bring_to_front(_window.root)
    else:
        _window = ShortcutWindow(app)


def _outputs():
    try:
        return [d for d in audio_default.list_outputs()
                if not any(h in d[1] for h in HIDDEN_OUTPUTS)]
    except OSError as e:
        print(f"[audio] could not list playback devices: {e}")
        return []


class ShortcutWindow:
    def __init__(self, app):
        self.app = app
        s = ui.scale
        self.pad = pad = int(16 * s)
        self.root = root = ui.window("Audio shortcuts")

        label(root, "Switch the audio output from your controller", size=14, bold=True,
              anchor="w").pack(fill="x", padx=pad, pady=(pad, 0))
        label(root, "Press a button combination, or press a button several times, to change "
                    "Windows' playback device.", color=TEXT_2, anchor="w", justify="left",
              wraplength=int(488 * s)).pack(fill="x", padx=pad, pady=(int(4 * s), 0))

        self.list = tk.Frame(root, bg=SURFACE)
        self.list.pack(fill="x", padx=pad, pady=(int(12 * s), 0))

        bottom = tk.Frame(root, bg=SURFACE)
        bottom.pack(fill="x", padx=pad, pady=(int(12 * s), 0))
        ui.button(bottom, "Add shortcut…", lambda: Editor(self, None), primary=True).pack(side="left")
        self.notify = tk.BooleanVar(value=app.audio_notify)
        ui.checkbox(bottom, "Show a notification when it switches", self.notify,
                    command=self._save).pack(side="right")

        label(root, "Games see these button presses too, so pick something a game won't "
                    "mind, like PS pressed 2 or 3 times, or a combination you don't use in "
                    "games.", size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(488 * s)).pack(fill="x", padx=pad, pady=(int(12 * s), pad))
        self._refresh()
        ui.bring_to_front(root)

    def _refresh(self):
        s = ui.scale
        for w in self.list.winfo_children():
            w.destroy()
        if not self.app.audio_shortcuts:
            label(self.list, "No shortcuts yet.", color=MUTED, anchor="w").pack(fill="x")
        for i, sc in enumerate(self.app.audio_shortcuts):
            row = tk.Frame(self.list, bg=PANEL, padx=int(12 * s), pady=int(8 * s))
            row.pack(fill="x", pady=(0 if i == 0 else int(6 * s), 0))
            text = tk.Frame(row, bg=PANEL)
            text.pack(side="left", fill="x", expand=True)
            label(text, describe(sc.buttons, sc.presses), size=11, bold=True, anchor="w").pack(fill="x")
            label(text, sc.target_text(), color=TEXT_2, anchor="w", justify="left",
                  wraplength=int(340 * s)).pack(fill="x")
            ui.button(row, "Delete", lambda i=i: self._delete(i), bg=GRID).pack(side="right")
            ui.button(row, "Edit…", lambda sc=sc: Editor(self, sc), bg=GRID).pack(
                side="right", padx=(0, int(6 * s)))

    def _delete(self, i):
        del self.app.audio_shortcuts[i]
        self._save()
        self._refresh()

    def _save(self):
        self.app.save_audio_shortcuts(self.app.audio_shortcuts, self.notify.get())

    def replace(self, old, new):
        shortcuts = self.app.audio_shortcuts
        if old in shortcuts:
            shortcuts[shortcuts.index(old)] = new
        else:
            shortcuts.append(new)
        self._save()
        self._refresh()


class Editor:
    """Add or edit one shortcut."""

    def __init__(self, parent, shortcut):
        self.parent, self.shortcut = parent, shortcut
        self.engine = parent.app.shortcut_engine
        self.buttons = list(shortcut.buttons) if shortcut else []
        s = ui.scale
        pad = int(16 * s)
        self.root = root = ui.window("Add shortcut" if shortcut is None else "Edit shortcut", 440)
        root.transient(parent.root)
        root.protocol("WM_DELETE_WINDOW", self._close)

        label(root, "Shortcut", color=TEXT_2, anchor="w").pack(fill="x", padx=pad, pady=(pad, 0))
        rec = tk.Frame(root, bg=SURFACE)
        rec.pack(fill="x", padx=pad, pady=(int(4 * s), 0))
        self.combo = label(rec, size=14, bold=True, anchor="w")
        self.combo.pack(side="left", fill="x", expand=True)
        self.record_btn = ui.button(rec, "Record", self._record)
        self.record_btn.pack(side="right")
        self.record_note = label(root, color=MUTED, anchor="w", justify="left",
                                 wraplength=int(408 * s))
        self.record_note.pack(fill="x", padx=pad)

        presses = tk.Frame(root, bg=SURFACE)
        presses.pack(fill="x", padx=pad, pady=(int(8 * s), 0))
        label(presses, "Press it", anchor="w").pack(side="left")
        self.presses = tk.IntVar(value=shortcut.presses if shortcut else 1)
        tk.Spinbox(presses, from_=1, to=5, width=3, textvariable=self.presses, state="readonly",
                   readonlybackground=PANEL, fg=TEXT, buttonbackground=PANEL, relief="flat",
                   font=("Segoe UI", 10), command=self._show_combo).pack(side="left", padx=int(6 * s))
        label(presses, "time(s)", anchor="w").pack(side="left")

        label(root, "Switch to", color=TEXT_2, anchor="w").pack(fill="x", padx=pad, pady=(int(16 * s), 0))
        devices = tk.Frame(root, bg=SURFACE)
        devices.pack(fill="x", padx=pad)
        chosen = dict(shortcut.devices) if shortcut else {}
        outputs = _outputs()
        present = {i for i, _ in outputs}
        self.device_vars = []
        for endpoint_id, name in outputs + [(i, n) for i, n in chosen.items() if i not in present]:
            var = tk.BooleanVar(value=endpoint_id in chosen)
            text = name if endpoint_id in present else f"{name} (not connected)"
            ui.checkbox(devices, text, var).pack(fill="x")
            self.device_vars.append((endpoint_id, name, var))
        if not self.device_vars:
            label(devices, "No playback devices found.", color=MUTED, anchor="w").pack(fill="x")
        label(root, "Tick one device to always switch to it, or several to go to the next one "
                    "each time.", size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(408 * s)).pack(fill="x", padx=pad, pady=(int(4 * s), 0))

        self.error = label(root, color=ERROR, anchor="w", justify="left", wraplength=int(408 * s))
        self.error.pack(fill="x", padx=pad, pady=(int(8 * s), 0))
        actions = tk.Frame(root, bg=SURFACE)
        actions.pack(fill="x", padx=pad, pady=(int(4 * s), pad))
        ui.button(actions, "Save", self._save, primary=True).pack(side="right")
        ui.button(actions, "Cancel", self._close).pack(side="right", padx=(0, int(6 * s)))

        self._show_combo()
        ui.bring_to_front(root)
        root.grab_set()
        if not self.buttons:
            self._record()

    def _show_combo(self):
        if self.buttons:
            self.combo.configure(text=describe(self.buttons, self.presses.get()), fg=TEXT)
        else:
            self.combo.configure(text="Not set", fg=MUTED)

    # -- recording ------------------------------------------------------------
    def _record(self):
        if not self.parent.app.bt.dev:
            self.record_note.configure(text="Turn on the controller to record a shortcut.")
            return
        self.record_btn.configure(state="disabled")
        self.record_note.configure(text="Press the shortcut on your controller now. "
                                        "Repeat it to make it a multi-press shortcut.")
        self.engine.record(lambda buttons, presses: ui.call(lambda: self._recorded(buttons, presses)))

    def _recorded(self, buttons, presses):
        if not self.root.winfo_exists():
            return
        self.buttons = sorted(buttons)
        self.presses.set(presses)
        self.record_btn.configure(state="normal")
        note = ""
        if len(buttons) == 1 and presses == 1:
            note = "A single press of one button will also trigger whenever you use it in games."
        self.record_note.configure(text=note)
        self._show_combo()

    # -- save -----------------------------------------------------------------
    def _save(self):
        devices = [(i, n) for i, n, var in self.device_vars if var.get()]
        if not self.buttons:
            return self.error.configure(text="Record a shortcut first.")
        if not devices:
            return self.error.configure(text="Tick at least one playback device.")
        presses = self.presses.get()
        for other in self.parent.app.audio_shortcuts:
            if other is not self.shortcut and set(other.buttons) == set(self.buttons) \
                    and other.presses == presses:
                return self.error.configure(text="Another shortcut already uses "
                                                 f"{describe(self.buttons, presses)}.")
        self.parent.replace(self.shortcut, AudioShortcut(self.buttons, presses, devices))
        self._close()

    def _close(self):
        self.engine.cancel_record()
        self.root.grab_release()
        self.root.destroy()
