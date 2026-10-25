"""Send key presses to Windows as if typed on a keyboard (SendInput).

The bridge runs elevated, so the keys also reach elevated windows and games.
"""
import ctypes
import time
from ctypes import wintypes as wt

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

user32 = ctypes.WinDLL("user32", use_last_error=True)

VK_TAB = 0x09
VK_MENU = 0x12   # Alt


class _KeybdInput(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):   # the largest union member; sets INPUT's size
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KeybdInput), ("mi", _MouseInput)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _send(*events):
    """events: (virtual key, down?)"""
    inputs = (_Input * len(events))()
    for inp, (vk, down) in zip(inputs, events):
        inp.type = INPUT_KEYBOARD
        inp.ki = _KeybdInput(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0, 0)
    if user32.SendInput(len(events), inputs, ctypes.sizeof(_Input)) != len(events):
        raise OSError(ctypes.get_last_error() or "SendInput was blocked")


_down = set()   # keys we hold down right now


def key_down(vk):
    _send((vk, True))
    _down.add(vk)


def key_up(vk):
    _down.discard(vk)
    _send((vk, False))


def release_all():
    """Let go of any key still held down (e.g. Alt, if the bridge quits mid-switch)."""
    for vk in list(_down):
        key_up(vk)


def press(*vks, hold=0.03):
    """Press a key combination: keys go down in order, then up in reverse."""
    _send(*[(vk, True) for vk in vks])
    time.sleep(hold)   # some games poll the keyboard and miss instant taps
    _send(*[(vk, False) for vk in reversed(vks)])
