"""What a controller shortcut can do.

To add an action: subclass Action, give it an id (stored in settings.json, so never change
it) and a name, implement summary/editor/run, and add it to ACTIONS. The shortcut window
and the tray pick it up from there.
"""
import tkinter as tk

import audio_default
import ui
from ui import MUTED, label


class ActionError(Exception):
    """A shortcut couldn't run; the message is shown to the user."""


class Action:
    id = ""
    name = ""

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


class SwitchAudioOutput(Action):
    id = "audio_output"
    name = "Switch audio output"
    # The bridge doesn't carry the controller's speaker audio yet, so its sound device is silent.
    HIDDEN = ("DualSense Wireless Controller",)

    def summary(self, params):
        names = [name for _, name in params["devices"]]
        if len(names) == 1:
            return f"Switch audio output to {names[0]}"
        return "Cycle audio output through " + ", ".join(names)

    def _outputs(self):
        try:
            return [d for d in audio_default.list_outputs() if not any(h in d[1] for h in self.HIDDEN)]
        except OSError as e:
            print(f"[audio] could not list playback devices: {e}")
            return []

    def editor(self, parent, params):
        s = ui.scale
        chosen = dict(map(tuple, params["devices"])) if params else {}
        outputs = self._outputs()
        present = {i for i, _ in outputs}
        rows = []
        for endpoint_id, name in outputs + [(i, n) for i, n in chosen.items() if i not in present]:
            var = tk.BooleanVar(value=endpoint_id in chosen)
            ui.checkbox(parent, name if endpoint_id in present else f"{name} (not connected)",
                        var).pack(fill="x")
            rows.append((endpoint_id, name, var))
        if not rows:
            label(parent, "No playback devices found.", color=MUTED, anchor="w").pack(fill="x")
        label(parent, "Tick one device to always switch to it, or several to go to the next one "
                      "each time.", size=9, color=MUTED, anchor="w", justify="left",
              wraplength=int(408 * s)).pack(fill="x", pady=(int(4 * s), 0))

        def get():
            devices = [[i, n] for i, n, var in rows if var.get()]
            if not devices:
                raise ActionError("Tick at least one playback device.")
            return {"devices": devices}
        return get

    def run(self, params):
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


ACTIONS = {a.id: a for a in [SwitchAudioOutput()]}
