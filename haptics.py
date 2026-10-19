"""Convert the wired DualSense's USB haptics audio into Bluetooth haptics reports.

USB: channels 3-4 of the 4ch / 48 kHz / int16 audio stream drive the two actuators.
Bluetooth: report 0x32 carries a sample packet of interleaved int8 stereo at 3 kHz.
Layout from SAxense by Sdore (https://github.com/egormanga/SAxense).
"""
import struct
import threading
import time
import zlib
from collections import deque

DECIMATE = 16                   # 48000 / 3000
# 32 frames per report, as SAxense does. Smaller reports start playing sooner in short
# bursts, but measured with the controller's IMU a continuous 16-frame stream makes
# the controller pulse every ~200 ms even when the samples are all zero; 32 is clean.
FRAMES_PER_REPORT = 32
# The controller's Bluetooth descriptor declares report 0x32 as 142 bytes and Windows
# sends exactly that, so the CRC must be in bytes 138-141. (SAxense writes 141 bytes,
# which works on Linux because hidraw sends the length as written.)
BT_HAPTICS_SIZE = 142


# Byte 5 of the 0x11 control sub-packet is the controller's haptics buffer depth,
# roughly in ms. SAxense sends 0xFF, which measured ~200 ms from write to vibration;
# measured with the IMU: 0x08 -> 16 ms, 0x10 -> 31 ms, 0x20 -> 39 ms, 0x40 -> 71 ms,
# all playing continuous tones unbroken; 0x04 plays nothing. A small buffer has less
# room for Bluetooth/PC timing hiccups, hence a user setting (tray menu).
DEFAULT_DEPTH = 0x20


def bt_haptics_report(samples, counter, seq=0, depth=DEFAULT_DEPTH):
    """samples: interleaved int8 stereo at 3 kHz (up to 64 bytes)."""
    body = bytearray(BT_HAPTICS_SIZE - 4)
    body[0] = 0x32
    body[1] = (seq & 0x0F) << 4                     # seq / tag
    # sub-packet 0x11 (sized): control block. Byte 0 enables haptics (0x10 there
    # silences it), byte 5 is the buffer depth, the last byte a running counter.
    body[2:11] = bytes([0x80 | 0x11, 7, 0xFE, 0, 0, 0, 0, depth, counter & 0xFF])
    # sub-packet 0x12 (sized): the samples
    body[11:13] = bytes([0x80 | 0x12, len(samples)])
    body[13:13 + len(samples)] = samples
    crc = zlib.crc32(b"\xa2" + bytes(body)) & 0xFFFFFFFF
    return bytes(body) + struct.pack("<I", crc)


class PacedSender:
    """Sends blocks at exactly the rate they play (one per FRAMES_PER_REPORT / 3 kHz).

    Windows delivers the audio in 10 ms chunks, so converted blocks come in clumps;
    measured with the IMU, clumped arrival sometimes makes the controller play a burst
    stretched to ~2x its length, while evenly paced reports play correctly.
    """
    PERIOD = FRAMES_PER_REPORT / 3000
    MAX_QUEUE = 4                       # beyond this, drop the oldest (clock drift)

    def __init__(self, send):
        self.send = send
        self._q = deque()
        self._cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, block):
        with self._cv:
            self._q.append(block)
            while len(self._q) > self.MAX_QUEUE:
                self._q.popleft()
            self._cv.notify()

    def _run(self):
        next_t = 0.0
        while True:
            with self._cv:
                idle = not self._q
                while not self._q:
                    self._cv.wait()
                block = self._q.popleft()
            now = time.perf_counter()
            if idle:                            # stream (re)starts: send right away
                next_t = max(next_t, now)
            if next_t > now:
                time.sleep(next_t - now)        # needs 1 ms timer resolution, see bridge.py
            self.send(block)
            next_t += self.PERIOD               # if we ran late, the next sleeps catch up


class HapticsConverter:
    """Feed 48 kHz (ch3, ch4) int16 frames; emits 3 kHz int8 stereo blocks via `emit`."""

    # After an effect, keep streaming silence this long before going quiet. The
    # controller only starts playing once ~80 ms of reports are queued, so a short
    # effect (a menu click) followed by nothing never plays - or plays and then keeps
    # vibrating until the next effect arrives (measured via IMU).
    SILENT_TAIL = int(0.25 * 3000 / FRAMES_PER_REPORT)

    def __init__(self, emit):
        self.emit = emit
        self._acc_l = self._acc_r = self._n = 0
        self._out = bytearray()
        self._silent_run = self.SILENT_TAIL

    def feed(self, frames):
        for l, r in frames:
            self._acc_l += l
            self._acc_r += r
            self._n += 1
            if self._n == DECIMATE:
                # int() truncates toward zero; floor division turned the game's ±1
                # noise floor into a constant -1 and the stream never went silent.
                self._out += struct.pack("bb", int(self._acc_l / (DECIMATE * 256)),
                                         int(self._acc_r / (DECIMATE * 256)))
                self._acc_l = self._acc_r = self._n = 0
                if len(self._out) == FRAMES_PER_REPORT * 2:
                    self._flush()

    def _flush(self):
        block, self._out = bytes(self._out), bytearray()
        # Anything within ±1 of zero is the game's noise floor: treat it as silence.
        if max(abs(v) for v in struct.unpack(f"{len(block)}b", block)) <= 1:
            block = bytes(len(block))
        # The audio engine streams silence whenever the device is open; after the
        # silent tail, stay quiet on Bluetooth until the next effect.
        if any(block):
            self._silent_run = 0
        elif self._silent_run >= self.SILENT_TAIL:
            return
        else:
            self._silent_run += 1
        self.emit(block)
