"""Hide the mouse cursor everywhere, by swapping every system cursor for a blank one.

ShowCursor() only affects our own windows, so this is the way that works in all apps and
games (except those that draw their own cursor). An invisible cursor still hovers over
things, so it can also be parked at the edge of the screen. restore() puts the user's
cursors back; it runs at startup too, in case the bridge ever exits with them hidden.
"""
import ctypes
import threading
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.CreateCursor.restype = wt.HANDLE
user32.SetSystemCursor.argtypes = (wt.HANDLE, wt.DWORD)

SPI_SETCURSORS = 0x57
# OCR_NORMAL, IBEAM, WAIT, CROSS, UP, SIZENWSE, SIZENESW, SIZEWE, SIZENS, SIZEALL, NO,
# HAND, APPSTARTING, HELP, PIN, PERSON
CURSOR_IDS = (32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648,
              32649, 32650, 32651, 32671, 32672)
MOVE_THRESHOLD = 4   # px the mouse must move to show the cursor again

_lock = threading.Lock()
_hidden = False
_parked_at = None


def _blank_cursor():
    w, h = user32.GetSystemMetrics(13), user32.GetSystemMetrics(14)   # SM_CXCURSOR, SM_CYCURSOR
    size = (w + 7) // 8 * h
    and_mask = (ctypes.c_ubyte * size)(*([0xFF] * size))   # keep the screen as is...
    xor_mask = (ctypes.c_ubyte * size)()                    # ...and draw nothing
    return user32.CreateCursor(None, 0, 0, w, h, and_mask, xor_mask)


def _cursor_pos():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def is_hidden():
    return _hidden


def hide(park=True, show_on_move=True):
    global _hidden, _parked_at
    with _lock:
        if _hidden:
            return
        for cid in CURSOR_IDS:
            # SetSystemCursor takes ownership of (and destroys) the cursor, so one each.
            user32.SetSystemCursor(_blank_cursor(), cid)
        if park:
            # right edge, halfway down the main screen: away from corners and the taskbar
            user32.SetCursorPos(user32.GetSystemMetrics(0) - 1, user32.GetSystemMetrics(1) // 2)
        _hidden = True
        _parked_at = _cursor_pos()
    if show_on_move:
        threading.Thread(target=_watch_mouse, daemon=True).start()


def restore():
    """Show the cursor: reload the user's cursors."""
    global _hidden
    with _lock:
        user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
        _hidden = False


def toggle(park=True, show_on_move=True):
    """Returns True if the cursor is now hidden."""
    if _hidden:
        restore()
        return False
    hide(park, show_on_move)
    return True


def _watch_mouse():
    while _hidden:
        time.sleep(0.1)
        x, y = _cursor_pos()
        if _parked_at and max(abs(x - _parked_at[0]), abs(y - _parked_at[1])) > MOVE_THRESHOLD:
            restore()
