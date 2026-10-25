"""Self-test of controller shortcut detection:  python tests\\test_shortcuts.py"""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import shortcuts
from shortcuts import PRESS_GAP, ShortcutEngine, decode, describe


def main():
    assert decode(0x08, 0, 0) == frozenset()
    assert decode(0x28, 0x03, 0x01) == {"Cross", "L1", "R1", "PS"}
    assert decode(0x01, 0, 0) == {"Up", "Right"}            # d-pad diagonal
    assert describe(["R1", "L1", "Options"]) == "L1 + R1 + Options"
    assert describe(["PS"], 3) == "PS ×3"

    fired = []
    engine = ShortcutEngine(fired.append)
    engine._fire = fired.append                              # synchronous for the test
    combo = SimpleNamespace(buttons=["L1", "R1", "Options"], presses=1)
    ps1 = SimpleNamespace(buttons=["PS"], presses=1)
    ps3 = SimpleNamespace(buttons=["PS"], presses=3)
    engine.set_shortcuts([combo, ps1, ps3])

    def press(*names):
        engine.update(frozenset(names))

    # a combination fires when its last button goes down, once
    press("L1"); press("L1", "R1"); press("L1", "R1", "Options"); press("L1", "R1", "Options", "Cross")
    press()
    assert fired == [combo], fired

    # PS x3 fires on the third press, without PS x1 firing on the way
    fired.clear()
    for _ in range(3):
        press("PS"); press()
    assert fired == [ps3], fired

    # a single PS press fires PS x1 once no second press follows
    fired.clear()
    time.sleep(PRESS_GAP + 0.1)
    press("PS"); press()
    assert fired == []
    time.sleep(PRESS_GAP + 0.2)
    assert fired == [ps1], fired

    # two presses match nothing
    fired.clear()
    press("PS"); press(); press("PS"); press()
    time.sleep(PRESS_GAP + 0.2)
    assert fired == [], fired

    # recording: the same chord repeated becomes a multi-press shortcut
    recorded = []
    engine.record(lambda b, p: recorded.append((b, p)))
    for _ in range(2):
        press("L1"); press("L1", "Triangle"); press()
    time.sleep(shortcuts.RECORD_IDLE + 0.2)
    assert recorded == [(frozenset({"L1", "Triangle"}), 2)], recorded
    assert fired == []

    print("OK: shortcut decoding, combinations, multi-press and recording all pass")


if __name__ == "__main__":
    main()
