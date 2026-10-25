"""Shortcuts window: controller shortcuts and what they do (see actions.py).

Lives on the shared UI thread (see ui.py).
"""
import tkinter as tk
from dataclasses import dataclass, field

import ui
from actions import ACTIONS, ActionError
from shortcuts import describe
from ui import GRID, MUTED, PANEL, SURFACE, TEXT, TEXT_2, label

ERROR = "#d03b3b"


@dataclass(eq=False)
class Shortcut:
    buttons: list
    presses: int = 1
    action: str = "audio_output"
    params: dict = field(default_factory=dict)

    def to_json(self):
        return {"buttons": sorted(self.buttons), "presses": self.presses,
                "action": self.action, "params": self.params}

    @classmethod
    def from_json(cls, d):
        action, params = d.get("action", "audio_output"), dict(d.get("params", {}))
        if action == "audio_output" and len(params.get("devices", [])) > 1:
            action = "audio_cycle"   # "Switch" took several devices before "Cycle" existed
        return cls(list(d.get("buttons", [])), int(d.get("presses", 1)), action, params)

    @property
    def hold(self):
        action = ACTIONS.get(self.action)
        return bool(action and action.hold)

    def summary(self):
        action = ACTIONS.get(self.action)
        return action.summary(self.params) if action else f"Unknown action ({self.action})"


_window = None


def show(app):
    """app: the tray app (shortcuts, shortcut_notify, save_shortcuts, shortcut_engine, bt)."""
    ui.call(lambda: _show(app))


def _show(app):
    global _window
    if _window is not None and _window.root.winfo_exists():
        ui.bring_to_front(_window.root)
    else:
        _window = ShortcutWindow(app)


class ShortcutWindow:
    def __init__(self, app):
        self.app = app
        s = ui.scale
        pad = int(16 * s)
        self.root = root = ui.window("Shortcuts")

        label(root, "Controller shortcuts", size=14, bold=True,
              anchor="w").pack(fill="x", padx=pad, pady=(pad, 0))
        label(root, "Press a button combination, or press a button several times, to make "
                    "something happen on the PC.", color=TEXT_2, anchor="w", justify="left",
              wraplength=int(488 * s)).pack(fill="x", padx=pad, pady=(int(4 * s), 0))

        self.list = tk.Frame(root, bg=SURFACE)
        self.list.pack(fill="x", padx=pad, pady=(int(12 * s), 0))

        bottom = tk.Frame(root, bg=SURFACE)
        bottom.pack(fill="x", padx=pad, pady=(int(12 * s), 0))
        ui.button(bottom, "Add shortcut…", lambda: Editor(self, None), primary=True).pack(side="left")
        self.notify = tk.BooleanVar(value=app.shortcut_notify)
        ui.checkbox(bottom, "Show a notification when one runs", self.notify,
                    command=self._save).pack(side="right")

        label(root, "Games see these button presses too, so pick something a game won't "
                    "mind, like PS pressed 2 or 3 times, or a combination you don't use in "
                    "games.", size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(488 * s)).pack(fill="x", padx=pad, pady=(int(12 * s), 0))
        label(root, "Steam has its own PS-button shortcuts too (\"chords\", like PS + Options), "
                    "which run as well while Steam is open. If one clashes with a shortcut here, "
                    "remove it in Steam: Settings → Controller → Guide Button Chord Layout → "
                    "Edit. For example, Steam's window switching doesn't let you pick a window "
                    "with the d-pad, but \"Scroll through windows\" here does.",
              size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(488 * s)).pack(fill="x", padx=pad, pady=(int(8 * s), pad))
        self._refresh()
        ui.bring_to_front(root)

    def _refresh(self):
        s = ui.scale
        for w in self.list.winfo_children():
            w.destroy()
        if not self.app.shortcuts:
            label(self.list, "No shortcuts yet.", color=MUTED, anchor="w").pack(fill="x")
        for i, sc in enumerate(self.app.shortcuts):
            row = tk.Frame(self.list, bg=PANEL, padx=int(12 * s), pady=int(8 * s))
            row.pack(fill="x", pady=(0 if i == 0 else int(6 * s), 0))
            text = tk.Frame(row, bg=PANEL)
            text.pack(side="left", fill="x", expand=True)
            label(text, describe(sc.buttons, sc.presses), size=11, bold=True, anchor="w").pack(fill="x")
            label(text, sc.summary(), color=TEXT_2, anchor="w", justify="left",
                  wraplength=int(340 * s)).pack(fill="x")
            ui.button(row, "Delete", lambda sc=sc: self._delete(sc), bg=GRID).pack(side="right")
            ui.button(row, "Edit…", lambda sc=sc: Editor(self, sc), bg=GRID).pack(
                side="right", padx=(0, int(6 * s)))

    def _delete(self, sc):
        self.app.shortcuts.remove(sc)
        self._save()
        self._refresh()

    def _save(self):
        self.app.save_shortcuts(self.app.shortcuts, self.notify.get())

    def replace(self, old, new):
        shortcuts = self.app.shortcuts
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

        # the shortcut
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

        # what it does
        what = tk.Frame(root, bg=SURFACE)
        what.pack(fill="x", padx=pad, pady=(int(16 * s), 0))
        label(what, "Action", color=TEXT_2, anchor="w").pack(side="left")
        names = {a.name: a.id for a in ACTIONS.values()}
        current = ACTIONS.get(shortcut.action) if shortcut else None
        self.action = tk.StringVar(value=(current or next(iter(ACTIONS.values()))).name)
        menu = tk.OptionMenu(what, self.action, *names, command=lambda _: self._show_action())
        menu.configure(bg=PANEL, fg=TEXT, activebackground=GRID, activeforeground=TEXT,
                       relief="flat", highlightthickness=0, font=("Segoe UI", 10))
        menu["menu"].configure(bg=PANEL, fg=TEXT, activebackground=ui.ACCENT, font=("Segoe UI", 10))
        menu.pack(side="left", padx=int(8 * s))
        self.action_ids = names
        self.action_frame = tk.Frame(root, bg=SURFACE)
        self.action_frame.pack(fill="x", padx=pad, pady=(int(6 * s), 0))

        self.error = label(root, color=ERROR, anchor="w", justify="left", wraplength=int(408 * s))
        self.error.pack(fill="x", padx=pad, pady=(int(8 * s), 0))
        actions = tk.Frame(root, bg=SURFACE)
        actions.pack(fill="x", padx=pad, pady=(int(4 * s), pad))
        ui.button(actions, "Save", self._save, primary=True).pack(side="right")
        ui.button(actions, "Cancel", self._close).pack(side="right", padx=(0, int(6 * s)))

        self._show_combo()
        self._show_action()
        ui.bring_to_front(root)
        root.grab_set()
        if not self.buttons:
            self._record()

    def _show_combo(self):
        if self.buttons:
            self.combo.configure(text=describe(self.buttons, self.presses.get()), fg=TEXT)
        else:
            self.combo.configure(text="Not set", fg=MUTED)

    def _show_action(self):
        for w in self.action_frame.winfo_children():
            w.destroy()
        action_id = self.action_ids[self.action.get()]
        params = self.shortcut.params if self.shortcut and self.shortcut.action == action_id else None
        self.get_params = ACTIONS[action_id].editor(self.action_frame, params)

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
        if not self.buttons:
            return self.error.configure(text="Record a shortcut first.")
        try:
            params = self.get_params()
        except ActionError as e:
            return self.error.configure(text=str(e))
        presses = self.presses.get()
        for other in self.parent.app.shortcuts:
            if other is not self.shortcut and set(other.buttons) == set(self.buttons) \
                    and other.presses == presses:
                return self.error.configure(text="Another shortcut already uses "
                                                 f"{describe(self.buttons, presses)}.")
        action_id = self.action_ids[self.action.get()]
        self.parent.replace(self.shortcut, Shortcut(self.buttons, presses, action_id, params))
        self._close()

    def _close(self):
        self.engine.cancel_record()
        self.root.grab_release()
        self.root.destroy()
