"""USB Audio Class 1.0 function of the wired DualSense (interfaces 0-2).

Interface 1 / EP 0x01 OUT: 4ch 48 kHz 16-bit. Channels 1-2 are the speaker,
channels 3-4 drive the haptic actuators. Interface 2 / EP 0x82 IN: stereo mic.

Isochronous URBs are completed at real-time pace (1 packet = 1 ms frame) so the
Windows audio engine sees a correctly clocked device.
"""
import array
import heapq
import itertools
import struct
import threading
import time

from usbip_server import EPIPE

SPEAKER_EP, MIC_EP = 0x01, 0x02
MIC_PACKET = bytes(192)  # 48 samples * 2ch * 16-bit of silence per ms

# Audio class request codes
SET_CUR, GET_CUR, GET_MIN, GET_MAX, GET_RES = 0x01, 0x81, 0x82, 0x83, 0x84
MUTE_CONTROL, VOLUME_CONTROL = 0x01, 0x02
VOLUME_RANGE = {GET_MIN: -0x6000, GET_MAX: 0x0000, GET_RES: 0x0100}  # 1/256 dB units


class AudioFunction:
    def __init__(self, on_haptics=None):
        self.on_haptics = on_haptics          # callback(list of (ch3, ch4) int16 frames)
        self._controls = {}                   # (entity, selector, channel) -> bytes
        self._sched = []                      # heap of (due, n, urb, payload)
        self._cancelled = set()
        self._cv = threading.Condition()
        self._clock = {}                      # ep -> time the next packet may complete
        self._n = itertools.count()
        self._server = None
        self._haptic_peak = 0
        self._last_report = 0.0
        threading.Thread(target=self._timer, daemon=True).start()

    def attached(self, server):
        self._server = server

    def detached(self):
        with self._cv:
            self._sched.clear()
            self._clock.clear()
        self._server = None

    # -- control requests (interface 0 entities, or the iso endpoints) -------
    def control(self, rtype, req, value, index, length, data):
        selector, channel, entity = value >> 8, value & 0xFF, index >> 8
        key = (entity, selector, channel)
        if rtype & 0x80:  # GET_*
            if req == GET_CUR:
                if key in self._controls:
                    return 0, self._controls[key]
                if rtype & 0x1F == 2:  # endpoint: sampling frequency
                    return 0, struct.pack("<I", 48000)[:3]
                return 0, (b"\x00" if selector == MUTE_CONTROL else struct.pack("<h", 0))
            if req in VOLUME_RANGE and selector == VOLUME_CONTROL:
                return 0, struct.pack("<h", VOLUME_RANGE[req])
            return EPIPE, b""
        if req == SET_CUR:
            self._controls[key] = bytes(data)
            return 0, b""
        return EPIPE, b""

    # -- isochronous endpoints ------------------------------------------------
    def submit(self, urb):
        if urb.ep == SPEAKER_EP and not urb.dir_in:
            self._inspect_haptics(urb)
            payload = None
        elif urb.ep == MIC_EP and urb.dir_in:
            payload = [MIC_PACKET] * len(urb.iso)
        else:
            return EPIPE, b""
        with self._cv:
            now = time.perf_counter()
            start = max(now, self._clock.get(urb.ep, 0.0))
            due = start + 0.001 * max(1, len(urb.iso))
            self._clock[urb.ep] = due
            heapq.heappush(self._sched, (due, next(self._n), urb, payload))
            self._cv.notify()
        return None  # completed later by the timer

    def cancel(self, seq):
        with self._cv:
            for i, (_, _, urb, _) in enumerate(self._sched):
                if urb.seq == seq:
                    self._sched.pop(i)
                    heapq.heapify(self._sched)
                    return True
        return False

    def _timer(self):
        while True:
            with self._cv:
                while not self._sched:
                    self._cv.wait()
                due, _, urb, payload = self._sched[0]
                wait = due - time.perf_counter()
                if wait > 0:
                    self._cv.wait(min(wait, 0.01))
                    continue
                heapq.heappop(self._sched)
            if self._server:
                self._server.complete(urb, 0, payload or [])

    # -- haptics --------------------------------------------------------------
    def _inspect_haptics(self, urb):
        samples = array.array("h")
        samples.frombytes(urb.data[: len(urb.data) // 8 * 8])
        frames = list(zip(samples[2::4], samples[3::4]))  # channels 3 and 4
        if frames:
            peak = max(max(abs(a), abs(b)) for a, b in frames)
            self._haptic_peak = max(self._haptic_peak, peak)
            if self.on_haptics:
                self.on_haptics(frames)
        now = time.monotonic()
        if now - self._last_report > 5:
            if self._haptic_peak > 0:
                print(f"[audio] game is sending haptics (peak {self._haptic_peak})")
            self._haptic_peak, self._last_report = 0, now
