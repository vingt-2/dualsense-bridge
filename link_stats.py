"""Bluetooth link quality, measured from when the controller's input reports arrive.

A DualSense on a good Bluetooth link sends an input report about every 1-2 ms (~600 Hz).
A weak link (distance, a body or desk in the way, a USB 3 port or a Wi-Fi router next to
the Bluetooth adapter) shows up as gaps: Bluetooth retransmits what didn't get through,
so reports arrive late or not at all. Haptics and rumble travel over the same link, so
gaps longer than the controller's haptics buffer are felt as stutter.
"""
import collections
import threading
import time

DROPOUT = 0.010   # a gap longer than this (s) counts as a dropout
HISTORY = 120     # seconds of per-second history kept


class Second:
    __slots__ = ("start", "reports", "dropouts", "worst", "disconnected")

    def __init__(self, start):
        self.start = start
        self.reports = 0
        self.dropouts = 0
        self.worst = 0.0          # longest gap between reports (s)
        self.disconnected = False


class LinkStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            now = time.perf_counter()
            self.since = now
            self.history = collections.deque(maxlen=HISTORY)
            self.current = Second(now)
            self.last = None             # arrival time of the previous report
            self.connected = False
            self.disconnects = 0
            self.total = Second(now)     # totals since reset

    def _roll(self, now):
        while now - self.current.start >= 1.0:
            self.history.append(self.current)
            self.current = Second(self.current.start + 1.0)
            if not self.connected:
                self.current.disconnected = True

    def input_report(self):
        now = time.perf_counter()
        with self._lock:
            self._roll(now)
            for s in (self.current, self.total):
                s.reports += 1
            if self.last is not None:
                gap = now - self.last
                for s in (self.current, self.total):
                    s.worst = max(s.worst, gap)
                    if gap > DROPOUT:
                        s.dropouts += 1
            self.last = now

    def connection(self, up):
        with self._lock:
            self._roll(time.perf_counter())
            if self.connected and not up:
                self.disconnects += 1
                self.current.disconnected = True
            self.connected = up
            self.last = None   # the time spent disconnected isn't a gap

    def snapshot(self):
        """(seconds of history, oldest first; totals since reset; seconds since reset)"""
        with self._lock:
            self._roll(time.perf_counter())
            return list(self.history), self.total, time.perf_counter() - self.since


def rating(seconds):
    """'Excellent' / 'Good' / 'Fair' / 'Poor' for a run of Second records, or None."""
    seconds = [s for s in seconds if s.reports]
    if not seconds:
        return None
    if any(s.disconnected for s in seconds):
        return "Poor"
    worst = max(s.worst for s in seconds) * 1000
    rate = sum(s.reports for s in seconds) / len(seconds)
    if worst < 20 and rate > 400:
        return "Excellent"
    if worst < 40 and rate > 250:
        return "Good"
    if worst < 80:
        return "Fair"
    return "Poor"
