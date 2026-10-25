"""Controller shortcuts: a combination of buttons held together, pressed one or more times.

"L1 + R1 + Options" fires when the last of the three goes down while the others are held.
"PS ×3" fires on the third press of PS, each press within PRESS_GAP of the one before.
When the same buttons have shortcuts for different press counts (PS ×1 and PS ×3), the
smaller one waits PRESS_GAP to see whether more presses follow.

Some actions are held, like Alt while Alt+Tab-ing through windows (shortcut.hold): after
the shortcut fires ("start"), each further press of its buttons fires "repeat" (whatever its
press count), and "end" comes once its buttons are let go. So with L1 + R1 you hold L1, tap
R1, and let go of L1 to finish. A one-button shortcut has nothing to hold, so it ends
HOLD_IDLE after the last press instead.

Games still see the buttons, so combinations a game doesn't use (or PS pressed several
times) work best.
"""
import queue
import threading
import time

PRESS_GAP = 0.4      # s allowed between presses of a multi-press shortcut
RECORD_IDLE = 0.8    # s without presses that ends a recording
HOLD_IDLE = 0.8      # s after the last press that ends a one-button held shortcut
HOLD_MAX = 15.0      # s after which a held shortcut ends anyway (never leave Alt stuck)

# Display order. Names match the controller's labels.
BUTTONS = ["L1", "R1", "L2", "R2", "L3", "R3",
           "Create", "Options", "PS", "Touchpad", "Mute",
           "Up", "Down", "Left", "Right", "Square", "Cross", "Circle", "Triangle"]

_FACE = {4: "Square", 5: "Cross", 6: "Circle", 7: "Triangle"}
_SHOULDER = {0: "L1", 1: "R1", 2: "L2", 3: "R2", 4: "Create", 5: "Options", 6: "L3", 7: "R3"}
_SYSTEM = {0: "PS", 1: "Touchpad", 2: "Mute"}
_DPAD = {"Up": (7, 0, 1), "Right": (1, 2, 3), "Down": (3, 4, 5), "Left": (5, 6, 7)}


def decode(b0, b1, b2):
    """The three button bytes of an input report -> frozenset of button names."""
    held = {name for bit, name in _FACE.items() if b0 >> bit & 1}
    held |= {name for bit, name in _SHOULDER.items() if b1 >> bit & 1}
    held |= {name for bit, name in _SYSTEM.items() if b2 >> bit & 1}
    hat = b0 & 0x0F   # 0 = up, clockwise; 8 = released
    held |= {name for name, hats in _DPAD.items() if hat in hats}
    return frozenset(held)


def describe(buttons, presses=1):
    combo = " + ".join(sorted(buttons, key=BUTTONS.index))
    if presses == 1:
        return combo
    return f"({combo}) ×{presses}" if len(buttons) > 1 else f"{combo} ×{presses}"


class ShortcutEngine:
    def __init__(self, on_fire):
        # on_fire(shortcut, phase): phase is "start", or "repeat"/"end" for held shortcuts.
        # Shortcuts are any objects with .buttons and .presses (and optionally .hold).
        # Called in order, on one worker thread: actions can take a moment (audio
        # switching), and a held action's start/repeat/end must not overtake each other.
        self.on_fire = on_fire
        self._jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()
        self._session = None         # held shortcut in progress: [shortcut, combo, timer]
        self.shortcuts = []
        self._held = frozenset()
        self._lock = threading.Lock()
        self._counts = {}            # combo -> (presses so far, time of last press)
        self._timers = {}            # combo -> pending threading.Timer
        self._recording = None       # (on_done, chords, current chord, timer)

    def set_shortcuts(self, shortcuts):
        with self._lock:
            self.shortcuts = list(shortcuts)
            self._reset()
            self._end_session()

    def _reset(self):
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        self._counts.clear()

    # -- input ----------------------------------------------------------------
    def update(self, held):
        """Called with the buttons held now, whenever they change."""
        with self._lock:
            prev, self._held = self._held, held
            if self._recording:
                self._record_update(prev, held)
                return
            if self._session:
                self._session_update(prev, held)
                return
            combos = {frozenset(s.buttons) for s in self.shortcuts}
            for combo in combos:
                if combo and combo <= held and not combo <= prev:
                    self._pressed(combo)

    def _pressed(self, combo):
        now = time.monotonic()
        count, last = self._counts.get(combo, (0, 0.0))
        count = count + 1 if now - last <= PRESS_GAP else 1
        if combo in self._timers:
            self._timers.pop(combo).cancel()
        counts = {s.presses: s for s in self.shortcuts if frozenset(s.buttons) == combo}
        if count >= max(counts):
            self._counts.pop(combo, None)
            if count in counts:
                self._fire(counts[count])
            return
        self._counts[combo] = (count, now)
        if count in counts:   # a longer shortcut on the same buttons may still follow
            t = threading.Timer(PRESS_GAP, self._timeout, args=(combo, count, counts[count]))
            t.daemon = True
            self._timers[combo] = t
            t.start()

    def _timeout(self, combo, count, shortcut):
        with self._lock:
            if self._counts.get(combo, (0,))[0] != count:
                return   # another press came in time
            self._counts.pop(combo, None)
            self._timers.pop(combo, None)
            self._fire(shortcut)

    def _fire(self, shortcut, phase="start"):
        self._jobs.put((shortcut, phase))
        if phase == "start" and getattr(shortcut, "hold", False):
            combo = frozenset(shortcut.buttons)
            self._session = [shortcut, combo, None]
            self._arm_session_timer(HOLD_MAX)
            if not combo & self._held:   # fired after its buttons were let go (press-count wait)
                self._arm_session_timer(HOLD_IDLE)

    def _worker(self):
        while True:
            shortcut, phase = self._jobs.get()
            try:
                self.on_fire(shortcut, phase)
            except Exception as e:  # keep the worker alive whatever an action does
                print(f"[shortcut] {type(e).__name__}: {e}")

    # -- held shortcuts -----------------------------------------------------------
    def _session_update(self, prev, held):
        shortcut, combo, _ = self._session
        if combo <= held and not combo <= prev:
            self._fire(shortcut, "repeat")
            self._arm_session_timer(HOLD_MAX)
        elif not combo & held and combo & prev:     # all of its buttons let go
            if len(combo) > 1:
                self._end_session()
            else:
                self._arm_session_timer(HOLD_IDLE)

    def _arm_session_timer(self, delay):
        session = self._session
        if session[2]:
            session[2].cancel()
        session[2] = threading.Timer(delay, self._session_timeout, args=(session,))
        session[2].daemon = True
        session[2].start()

    def _session_timeout(self, session):
        with self._lock:
            if self._session is session:
                self._end_session()

    def _end_session(self):
        if self._session:
            shortcut, _, timer = self._session
            if timer:
                timer.cancel()
            self._session = None
            self._jobs.put((shortcut, "end"))

    # -- recording --------------------------------------------------------------
    def record(self, on_done):
        """Capture the next shortcut pressed on the controller; on_done(buttons, presses)
        is called from another thread. While recording, shortcuts don't fire."""
        with self._lock:
            self.cancel_record_locked()
            self._reset()
            self._recording = [on_done, [], set(), None]

    def cancel_record(self):
        with self._lock:
            self.cancel_record_locked()

    def cancel_record_locked(self):
        if self._recording and self._recording[3]:
            self._recording[3].cancel()
        self._recording = None

    def _record_update(self, prev, held):
        rec = self._recording
        if rec[3]:
            rec[3].cancel()
            rec[3] = None
        rec[2] |= held          # the chord: everything held together this press
        if held or not rec[2]:
            return
        rec[1].append(frozenset(rec[2]))   # all released: one press done
        rec[2] = set()
        rec[3] = threading.Timer(RECORD_IDLE, self._record_done)
        rec[3].daemon = True
        rec[3].start()

    def _record_done(self):
        with self._lock:
            rec, self._recording = self._recording, None
        if not rec:
            return
        chords = rec[1]
        # Repeating the same chord records a multi-press shortcut.
        presses = len(chords) if all(c == chords[-1] for c in chords) else 1
        rec[0](chords[-1], min(presses, 5))
