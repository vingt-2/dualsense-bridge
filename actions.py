"""What a controller shortcut can do.

To add an action: subclass Action, give it an id (stored in settings.json, so never change
it) and a name, implement summary/editor/run, and add it to ACTIONS. The shortcut window
and the tray pick it up from there.
"""
import tkinter as tk

import audio_default
import cursor
import display
import keyboard
import ui
from ui import MUTED, label


class ActionError(Exception):
    """A shortcut couldn't run; the message is shown to the user."""


class Action:
    id = ""
    name = ""
    hold = False   # True: start, then repeat on each further press, then end (see shortcuts.py)

    def summary(self, params):
        """One line describing what this shortcut does with these params."""
        return self.name

    def editor(self, parent, params):
        """Build this action's settings into the Tk frame `parent` (params is None for a new
        shortcut). Returns a function that gives the edited params, or raises ActionError."""
        return lambda: {}

    def run(self, params):
        """Do it (on a worker thread). Returns a short message for the notification."""
        raise NotImplementedError

    def repeat(self, params):
        """Held actions: the shortcut was pressed again while held."""

    def end(self, params):
        """Held actions: the shortcut was let go."""


class _AudioOutputAction(Action):
    # The bridge doesn't carry the controller's speaker audio yet, so its sound device is silent.
    HIDDEN = ("DualSense Wireless Controller",)

    def _outputs(self):
        try:
            return [d for d in audio_default.list_outputs() if not any(h in d[1] for h in self.HIDDEN)]
        except OSError as e:
            print(f"[audio] could not list playback devices: {e}")
            return []

    def _device_rows(self, params):
        """[(endpoint id, name, label)]: the devices plugged in, plus chosen ones that aren't."""
        chosen = dict(map(tuple, params["devices"])) if params else {}
        outputs = self._outputs()
        present = {i for i, _ in outputs}
        return ([(i, n, n) for i, n in outputs]
                + [(i, n, f"{n} (not connected)") for i, n in chosen.items() if i not in present])

    def _hint(self, parent, text):
        label(parent, text, size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(408 * ui.scale)).pack(fill="x", pady=(int(4 * ui.scale), 0))

    def run(self, params):
        """Switch to the next connected device of params["devices"] after the current one."""
        try:
            outputs = dict(audio_default.list_outputs())
            ids = [i for i, _ in params["devices"] if i in outputs]
            if not ids:
                raise ActionError("None of this shortcut's playback devices are connected.")
            current = audio_default.get_defaults().get((audio_default.E_RENDER, 0))
            target = ids[(ids.index(current) + 1) % len(ids)] if current in ids else ids[0]
            audio_default.set_output(target)
        except OSError as e:
            raise ActionError(f"Could not switch the audio output: {e}")
        return f"Audio output: {outputs[target]}"


class SwitchAudioOutput(_AudioOutputAction):
    id = "audio_output"
    name = "Switch audio output"

    def summary(self, params):
        return f"Switch audio output to {params['devices'][0][1]}"

    def editor(self, parent, params):
        rows = self._device_rows(params)
        choice = tk.StringVar(value=params["devices"][0][0] if params else "")
        for endpoint_id, _, text in rows:
            ui.radio(parent, text, choice, endpoint_id).pack(fill="x")
        if not rows:
            label(parent, "No playback devices found.", color=MUTED, anchor="w").pack(fill="x")
        self._hint(parent, "To go back and forth between several devices, use "
                           "\"Cycle audio outputs\" instead.")

        def get():
            for endpoint_id, name, _ in rows:
                if endpoint_id == choice.get():
                    return {"devices": [[endpoint_id, name]]}
            raise ActionError("Choose a playback device.")
        return get


class CycleAudioOutputs(_AudioOutputAction):
    id = "audio_cycle"
    name = "Cycle audio outputs"

    def summary(self, params):
        return "Cycle audio output through " + ", ".join(n for _, n in params["devices"])

    def editor(self, parent, params):
        rows = self._device_rows(params)
        chosen = {i for i, _ in params["devices"]} if params else set()
        ticks = []
        for endpoint_id, name, text in rows:
            var = tk.BooleanVar(value=endpoint_id in chosen)
            ui.checkbox(parent, text, var).pack(fill="x")
            ticks.append((endpoint_id, name, var))
        if not rows:
            label(parent, "No playback devices found.", color=MUTED, anchor="w").pack(fill="x")
        self._hint(parent, "Each press switches to the next ticked device, in this order.")

        def get():
            devices = [[i, n] for i, n, var in ticks if var.get()]
            if len(devices) < 2:
                raise ActionError("Tick at least two playback devices to cycle through.")
            return {"devices": devices}
        return get


class CyclePrimaryDisplay(Action):
    id = "display_cycle"
    name = "Cycle main display"

    def summary(self, params):
        return "Cycle the main display through " + ", ".join(n for _, n in params["displays"])

    def editor(self, parent, params):
        try:
            displays = display.list_displays()
        except OSError as e:
            print(f"[display] could not list displays: {e}")
            displays = []
        chosen = {i for i, _ in params["displays"]} if params else set()
        present = {d["id"] for d in displays}
        rows = [(d["id"], display.label(d), display.label(d)) for d in displays]
        if params:
            rows += [(i, n, f"{n} (not connected)") for i, n in params["displays"] if i not in present]
        ticks = []
        for display_id, name, text in rows:
            var = tk.BooleanVar(value=display_id in chosen)
            ui.checkbox(parent, text, var).pack(fill="x")
            ticks.append((display_id, name, var))
        if not rows:
            label(parent, "No displays found.", color=MUTED, anchor="w").pack(fill="x")
        label(parent, "Each press makes the next ticked display the main one, in this order. "
                      "The main display has the taskbar's clock and is where most games open.",
              size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(408 * ui.scale)).pack(fill="x", pady=(int(4 * ui.scale), 0))

        def get():
            chosen = [[i, n] for i, n, var in ticks if var.get()]
            if len(chosen) < 2:
                raise ActionError("Tick at least two displays to cycle through.")
            return {"displays": chosen}
        return get

    def run(self, params):
        try:
            displays = {d["id"]: d for d in display.list_displays()}
            ids = [i for i, _ in params["displays"] if i in displays]
            if not ids:
                raise ActionError("None of this shortcut's displays are connected.")
            current = next((i for i, d in displays.items() if d["primary"]), None)
            target = ids[(ids.index(current) + 1) % len(ids)] if current in ids else ids[0]
            display.set_primary(displays[target]["device"])
        except OSError as e:
            raise ActionError(f"Could not change the main display: {e}")
        return f"Main display: {display.label(displays[target])}"


class AltTab(Action):
    id = "alt_tab"
    name = "Switch window (Alt+Tab)"

    def summary(self, params):
        return "Switch to the previous window (Alt+Tab)"

    def editor(self, parent, params):
        label(parent, "Goes back to the window you used before, like pressing Alt+Tab once. "
                      "Use it again to come back.", size=9, color=MUTED, anchor="w",
              justify="left", wraplength=int(408 * ui.scale)).pack(fill="x")
        return lambda: {}

    def run(self, params):
        try:
            keyboard.press(keyboard.VK_MENU, keyboard.VK_TAB)
        except OSError as e:
            raise ActionError(f"Could not send Alt+Tab: {e}")
        return None   # the window change is feedback enough


class WindowSwitcher(Action):
    id = "window_switcher"
    name = "Scroll through windows (hold Alt+Tab)"
    hold = True

    def summary(self, params):
        return "Scroll through windows, like holding Alt and pressing Tab"

    def editor(self, parent, params):
        label(parent, "Opens the Alt+Tab switcher and moves to the next window on each press.\n"
                      "With a combination like L1 + R1: hold L1, tap R1 to move through the "
                      "windows, let go of L1 to switch.\n"
                      "With one button: tap it to move through the windows; it switches a "
                      "moment after your last tap.\n"
                      "While it's open you can also pick a window with the d-pad.", size=9, color=MUTED, anchor="w",
              justify="left", wraplength=int(408 * ui.scale)).pack(fill="x")
        return lambda: {}

    def run(self, params):
        try:
            keyboard.key_down(keyboard.VK_MENU)
            keyboard.press(keyboard.VK_TAB)
        except OSError as e:
            keyboard.release_all()
            raise ActionError(f"Could not open the window switcher: {e}")

    def repeat(self, params):
        keyboard.press(keyboard.VK_TAB)

    def end(self, params):
        keyboard.key_up(keyboard.VK_MENU)


class HideCursor(Action):
    id = "hide_cursor"
    name = "Hide mouse cursor"

    def summary(self, params):
        return "Hide or show the mouse cursor"

    def editor(self, parent, params):
        params = params or {"park": True, "show_on_move": True}
        park = tk.BooleanVar(value=params.get("park", True))
        show_on_move = tk.BooleanVar(value=params.get("show_on_move", True))
        ui.checkbox(parent, "Also move it to the edge of the screen", park).pack(fill="x")
        ui.checkbox(parent, "Show it again when the mouse moves", show_on_move).pack(fill="x")
        label(parent, "Press the shortcut again to show it. An invisible cursor still hovers "
                      "over things, so moving it out of the way stops it highlighting buttons "
                      "in game menus. Games that draw their own cursor aren't affected.",
              size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(408 * ui.scale)).pack(fill="x", pady=(int(4 * ui.scale), 0))
        return lambda: {"park": park.get(), "show_on_move": show_on_move.get()}

    def run(self, params):
        try:
            cursor.toggle(params.get("park", True), params.get("show_on_move", True))
        except OSError as e:
            raise ActionError(f"Could not hide the cursor: {e}")
        return None   # the cursor vanishing is feedback enough


ACTIONS = {a.id: a for a in [SwitchAudioOutput(), CycleAudioOutputs(), CyclePrimaryDisplay(), AltTab(), WindowSwitcher(), HideCursor()]}
