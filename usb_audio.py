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
MIC_FRAMES = 48          # per 1 ms packet: 48 frames * 2ch * 16-bit = 192 bytes
SPEAKER_UNIT, MIC_UNIT = 2, 5   # feature units (volume, mute) in the descriptor

# Audio class request codes
SET_CUR, GET_CUR, GET_MIN, GET_MAX, GET_RES = 0x01, 0x81, 0x82, 0x83, 0x84
MUTE_CONTROL, VOLUME_CONTROL = 0x01, 0x02
VOLUME_RANGE = {GET_MIN: -0x6000, GET_MAX: 0x0000, GET_RES: 0x0100}  # 1/256 dB units


class AudioFunction:
    def __init__(self, on_haptics=None, on_speaker=None, mic=None):
        self.on_haptics = on_haptics          # callback(list of (ch3, ch4) int16 frames)
        self.on_speaker = on_speaker          # callback(array('h') of (ch1, ch2) frames, gain)
        self.mic = mic                        # media.MicStream, or None for silence
        self._controls = {}                   # (entity, selector, channel) -> bytes
        self._sched = []                      # heap of (due, n, urb, payload)
        self._cancelled = set()
        self._cv = threading.Condition()
        self._clock = {}                      # ep -> time the next packet may complete
        self._n = itertools.count()
        self._server = None
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
            payload = self._mic_packets(len(urb.iso))
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

    def gain(self, unit):
        """0..1 from a feature unit's mute and master volume (1/256 dB) as Windows set
        them: it leaves applying them to the device."""
        if self._controls.get((unit, MUTE_CONTROL, 0), bytes(1))[:1] != bytes(1):
            return 0.0
        vol = self._controls.get((unit, VOLUME_CONTROL, 0))
        return 10 ** (struct.unpack("<h", vol[:2])[0] / 256 / 20) if vol and len(vol) >= 2 else 1.0

    # -- microphone -------------------------------------------------------------
    def _mic_packets(self, n):
        if self.mic is None:
            return [bytes(MIC_FRAMES * 4)] * n
        mono = self.mic.read(MIC_FRAMES * n)
        gain = self.gain(MIC_UNIT)
        if gain < 0.999:
            mono = array.array("h", (int(v * gain) for v in mono))
        stereo = array.array("h", bytes(len(mono) * 4))
        stereo[0::2] = mono
        stereo[1::2] = mono
        size = MIC_FRAMES * 2
        return [stereo[i * size:(i + 1) * size].tobytes() for i in range(n)]

    # -- speaker and haptics ----------------------------------------------------
    def _inspect_haptics(self, urb):
        samples = array.array("h")
        samples.frombytes(urb.data[: len(urb.data) // 8 * 8])
        if self.on_speaker and samples:
            speaker = array.array("h", bytes(len(samples)))   # half as many: 2 of 4 channels
            speaker[0::2] = samples[0::4]                 # channels 1 and 2
            speaker[1::2] = samples[1::4]
            self.on_speaker(speaker, self.gain(SPEAKER_UNIT))
        frames = list(zip(samples[2::4], samples[3::4]))  # channels 3 and 4
        if frames and self.on_haptics:
            self.on_haptics(frames)
