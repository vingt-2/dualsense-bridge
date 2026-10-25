"""Speaker and microphone audio over Bluetooth, and the paced stream of media reports.

Speaker: the wired DualSense plays USB audio channels 1-2. Over Bluetooth the controller
takes Opus instead: stereo, 48 kHz, CELT, constant 200 bytes per 10 ms frame (what a PS5
sends). It plays one frame per media report, and media reports go out every 10.667 ms
(32 haptics frames at 3 kHz), so each frame carries 512 input samples squeezed into 480:
the controller plays them at its own pace. Report 0x36 carries haptics and one Opus
frame; without speaker audio, the plain haptics report 0x32 is sent (see haptics.py).

Microphone: once enabled (bit 0 of the media config packet), the controller streams Opus
frames back in input report 0x31 with type 2 in the low nibble of byte 1 (gamepad state
is type 0/1): a counter byte and 71 bytes of Opus.

Protocol from LinuxAudio4Dualsense5 (GeorgLegato) and dualwave (jpbjesus), which build on
SAxense (egormanga) and Nielk1's PS5 captures.
"""
import array
import struct
import threading
import time
import zlib
from collections import deque

import opus
from haptics import BT_HAPTICS_SIZE, FRAMES_PER_REPORT

PERIOD = FRAMES_PER_REPORT / 3000          # 10.667 ms per media report
SPEAKER_IN = 512                           # 48 kHz frames per report period
SPEAKER_OUT = 480                          # frames per 10 ms Opus frame
OPUS_FRAME = 200
BT_SPEAKER_SIZE = 398                      # report 0x36, as the Bluetooth descriptor declares
BT_SPEAKER2_SIZE = 547                     # report 0x39
MIC_TYPE = 2
SPEAKER_BUFFERS = (0x40, 0x40, 0x40, 0x40)  # config bytes 1-4 in report 0x36
# Report 0x39 as a PS5 sends it (Nielk1's capture, via dualwave): config 91 06 <flags> 51 1F
# 62 28 <frame counter>, two Opus frames (0x15), two haptics blocks. The last config byte is
# the speaker's buffer depth: measured with the controller's mic listening to its speaker
# over a link that stalls ~35 times per 5 s (up to 45 ms), 0x14 gave ~140 dropouts in 5 s,
# the PS5's 0x28 a few, 0x3C none for ~15 ms more delay, 0x50 none for ~40 ms.
PS5_CONFIG = (0x51, 0x1F, 0x62, 0x3C)


def _seal(body, size):
    body = bytes(body).ljust(size - 4, b"\0")
    return body + struct.pack("<I", zlib.crc32(b"\xa2" + body) & 0xFFFFFFFF)


def config_packet(depth, counter, mic, buffers=SPEAKER_BUFFERS):
    """Sub-packet 0x11: stream config. Bit 0 of the first byte streams the microphone back,
    bit 1 keeps gamepad input coming; the 5th buffer byte is the haptics buffer depth."""
    return bytes([0x80 | 0x11, 7, 0xFE | mic, *buffers, depth, counter & 0xFF])


def speaker_report(haptics, frame, seq, counter, depth, mic=False, headset=False,
                   buffers=SPEAKER_BUFFERS):
    """Report 0x36: config, 64 bytes of haptics, one Opus frame for the speaker (0x13)
    or, in stereo, the headphone jack (0x16)."""
    body = bytearray([0x36, (seq & 0x0F) << 4])
    body += config_packet(depth, counter, mic, buffers)
    body += bytes([0x80 | 0x12, len(haptics)]) + haptics
    body += bytes([0x80 | (0x16 if headset else 0x13), OPUS_FRAME]) + frame
    return _seal(body, BT_SPEAKER_SIZE)


def speaker_report_pair(haptics, frames, seq, counter, mic=False, headset=False,
                        config=PS5_CONFIG):
    """Report 0x39: two Opus frames and two haptics blocks (21.3 ms), as a PS5 sends it.
    Packet ids with bit 6 set carry two payloads of the given length."""
    body = bytearray([0x39, (seq & 0x0F) << 4])
    body += bytes([0x80 | 0x11, 6, 0x7E | mic, *config, counter & 0xFF])
    body += bytes([0xC0 | (0x16 if headset else 0x15), OPUS_FRAME]) + frames[0] + frames[1]
    body += bytes([0xC0 | 0x12, 64]) + haptics[0].ljust(64, b"\0") + haptics[1].ljust(64, b"\0")
    return _seal(body, BT_SPEAKER2_SIZE)


def audio_control_report(seq, mic):
    """Report 0x32 holding only a config packet: turns the microphone stream on or off
    (and keeps gamepad input on) without touching haptics."""
    return _seal(bytes([0x32, (seq & 0x0F) << 4, 0x80 | 0x11, 1, 0x02 | mic]), BT_HAPTICS_SIZE)


def speaker_setup_output(headset=False):
    """USB output report 0x02 that routes the controller's audio and sets its volumes
    (fields from Linux hid-playstation; offsets are within the 47-byte payload + 1)."""
    r = bytearray(48)
    r[0] = 0x02
    r[1] = 0x80 | 0x20 | 0x10   # valid: audio control, speaker volume, headphone volume
    r[2] = 0x80                 # valid: audio control 2
    if headset:
        r[5], r[6], r[8] = 0x7F, 0x00, 0x00     # headphones up, speaker off, path L/R jack
    else:
        r[5], r[6], r[8] = 0x00, 0x64, 0x30     # speaker volume max, path 3: speaker
    r[38] = 0x02                # audio control 2: speaker preamp gain, as Linux sets it
    return bytes(r)


class SpeakerStream:
    """USB speaker audio (channels 1-2) -> Opus frames for the controller."""
    IDLE_AFTER = 2.0        # s of silence before the speaker stream stops
    PREBUFFER = 2           # report periods buffered before the first frame
    MAX_BUFFER = 6          # beyond this, drop the oldest (keeps latency bounded)

    def __init__(self, on_start=None, on_stop=None):
        self.on_start, self.on_stop = on_start, on_stop
        self.wake = lambda: None
        self.mono = True    # the speaker plays one channel: mix both into it
        self._buf = array.array("h")
        self._lock = threading.Lock()
        self._active = False
        self._started = False
        self._last_loud = 0.0
        self._encoder = None
        self.stats = {}

    def _reset_stats(self):
        self.stats = {"frames": 0, "underruns": 0, "dropped_ms": 0.0, "late": 0, "max_late_ms": 0.0,
                      "max_buffer_ms": 0.0}

    def summary(self):
        st = self.stats
        return (f"{st['frames']} frames, {st['underruns']} underruns, {st['dropped_ms']:.0f} ms dropped, "
                f"{st['late']} late reports (worst {st['max_late_ms']:.1f} ms), "
                f"buffer up to {st['max_buffer_ms']:.0f} ms")

    def feed(self, samples, gain):
        """samples: array('h') of interleaved (ch1, ch2) frames; gain: 0..1 from the
        device's volume/mute controls (Windows leaves volume to the device)."""
        now = time.monotonic()
        loud = max(samples, default=0) > 1 or min(samples, default=0) < -1
        with self._lock:
            if loud:
                self._last_loud = now
                if not self._active:
                    self._active, self._started = True, False
                    self._buf = array.array("h")
                    self._reset_stats()
                    start = True
                else:
                    start = False
            else:
                start = False
            if not self._active:
                return
            if gain < 0.999:
                samples = array.array("h", (int(v * gain) for v in samples))
            self._buf.extend(samples)
            self.stats["max_buffer_ms"] = max(self.stats["max_buffer_ms"], len(self._buf) / 96)
            excess = len(self._buf) - self.MAX_BUFFER * SPEAKER_IN * 2
            if excess > 0:
                del self._buf[:excess]
                self.stats["dropped_ms"] += excess / 96
        if start and self.on_start:
            self.on_start()
        self.wake()

    def ready(self):
        with self._lock:
            need = (1 if self._started else self.PREBUFFER) * SPEAKER_IN * 2
            return self._active and len(self._buf) >= need

    def poll_idle(self):
        """Stop the speaker stream after IDLE_AFTER of silence."""
        with self._lock:
            stop = self._active and time.monotonic() - self._last_loud > self.IDLE_AFTER
            if stop:
                self._active = False
        if stop and self.on_stop:
            self.on_stop()

    def next_frame(self):
        """The next 200-byte Opus frame, or None when there's no speaker audio."""
        self.poll_idle()
        with self._lock:
            if not self._active:
                return None
            if len(self._buf) < (1 if self._started else self.PREBUFFER) * SPEAKER_IN * 2:
                if self._started:
                    self.stats["underruns"] += 1
                return None
            block = self._buf[:SPEAKER_IN * 2]
            del self._buf[:SPEAKER_IN * 2]
            self._started = True
            self.stats["frames"] += 1
        if self._encoder is None:
            self._encoder = opus.Encoder(OPUS_FRAME)
        return self._encoder.encode(self._squeeze(block))

    def _squeeze(self, block):
        """512 stereo frames -> 480, linear interpolation; mixed to mono for the speaker."""
        out = array.array("h", bytes(SPEAKER_OUT * 4))
        step = (SPEAKER_IN - 1) / (SPEAKER_OUT - 1)
        for i in range(SPEAKER_OUT):
            pos = i * step
            j = int(pos)
            f = pos - j
            k = min(j + 1, SPEAKER_IN - 1)
            l = block[2 * j] + (block[2 * k] - block[2 * j]) * f
            r = block[2 * j + 1] + (block[2 * k + 1] - block[2 * j + 1]) * f
            if self.mono:
                l = r = (l + r) / 2
            out[2 * i], out[2 * i + 1] = int(l), int(r)
        return out


class MediaStreamer:
    """Sends media reports at exactly the rate they play: haptics blocks (see haptics.py)
    and, while there is speaker audio, Opus frames, one of each per report.

    Windows delivers audio in 10 ms chunks, so converted blocks come in clumps; measured
    with the IMU, clumped arrival sometimes makes the controller play a burst stretched to
    ~2x its length, while evenly paced reports play correctly.
    """
    MAX_QUEUE = 4                       # haptics blocks; beyond this, drop the oldest

    def __init__(self, send, speaker):
        self.send = send                # send(haptics block or None, Opus frame or None)
        self.speaker = speaker
        speaker.wake = self._wake
        self._q = deque()
        self._cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, block):
        with self._cv:
            self._q.append(block)
            while len(self._q) > self.MAX_QUEUE:
                self._q.popleft()
            self._cv.notify()

    def _wake(self):
        with self._cv:
            self._cv.notify()

    def _run(self):
        next_t = 0.0
        while True:
            with self._cv:
                idle = not self._q and not self.speaker.ready()
                while not self._q and not self.speaker.ready():
                    self._cv.wait(0.05)
                    self.speaker.poll_idle()    # notice when speaker audio has stopped
                block = self._q.popleft() if self._q else None
            frame = self.speaker.next_frame()
            if block is None and frame is None:
                continue
            now = time.perf_counter()
            if idle:                            # stream (re)starts: send right away
                next_t = max(next_t, now)
            if next_t > now:
                time.sleep(next_t - now)        # needs 1 ms timer resolution, see bridge.py
            elif frame is not None and now - next_t > 0.003:
                st = self.speaker.stats
                st["late"] += 1
                st["max_late_ms"] = max(st["max_late_ms"], (now - next_t) * 1000)
            self.send(block, frame)
            next_t += PERIOD                    # if we ran late, the next sleeps catch up


class MicStream:
    """Opus mic packets from the controller -> 48 kHz mono samples for the USB mic."""
    PREBUFFER = 2400        # samples (50 ms) buffered before playing out: rides over link stalls
    MAX_BUFFER = 9600       # 200 ms; beyond this, drop the oldest

    def __init__(self):
        self._buf = array.array("h")
        self._lock = threading.Lock()
        self._decoder = None
        self._last_counter = None
        self._playing = False
        self.packets = self.lost = self.underruns = 0

    def summary(self):
        return f"{self.packets} mic packets, {self.lost} lost, {self.underruns} underruns"

    def reset(self):
        with self._lock:
            self._buf = array.array("h")
            self._last_counter = None
            self._playing = False
            self.packets = self.lost = self.underruns = 0

    def packet(self, counter, data):
        if self._decoder is None:
            self._decoder = opus.Decoder(2)
        frames = []
        if self._last_counter is not None:
            lost = (counter - self._last_counter - 1) & 0xFF
            if 0 < lost < 5:
                frames += [self._decoder.decode(None) for _ in range(lost)]
                self.lost += lost
        self._last_counter = counter
        frames.append(self._decoder.decode(bytes(data)))
        mono = array.array("h")
        for pcm in frames:
            mono.extend((pcm[i] + pcm[i + 1]) // 2 for i in range(0, len(pcm) - 1, 2))
        with self._lock:
            self.packets += 1
            self._buf.extend(mono)
            excess = len(self._buf) - self.MAX_BUFFER
            if excess > 0:
                del self._buf[:excess]

    def read(self, n):
        """n mono samples (silence while there isn't enough)."""
        with self._lock:
            if not self._playing and len(self._buf) < self.PREBUFFER:
                return array.array("h", bytes(2 * n))
            self._playing = True
            out = self._buf[:n]
            del self._buf[:n]
            if len(out) < n:            # ran dry: wait for a cushion again
                self._playing = False
                self.underruns += 1
                out.extend(array.array("h", bytes(2 * (n - len(out)))))
            return out
