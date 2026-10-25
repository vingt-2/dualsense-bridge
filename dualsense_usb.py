"""Emulated wired (USB) DualSense: descriptors and USB<->Bluetooth report translation."""
import json
import os
import struct
import threading
import time
import zlib
from collections import deque

from haptics import HapticsConverter
from media import MediaStreamer, MicStream, SpeakerStream, speaker_setup_output
from usb_audio import AudioFunction
from usbip_server import EPIPE

VID, PID = 0x054C, 0x0CE6

# Feature reports of a wired DualSense, in descriptor order:
# (report id, payload size excluding id, vendor usage) - taken from a real controller.
FEATURES = [
    (0x05, 40, 0x33), (0x08, 47, 0x34), (0x09, 19, 0x24), (0x0A, 26, 0x25),
    (0x0B, 41, 0x41), (0x0C, 41, 0x42), (0x20, 63, 0x26), (0x21, 4, 0x27),
    (0x22, 63, 0x40), (0x80, 63, 0x28), (0x81, 63, 0x29), (0x82, 9, 0x2A),
    (0x83, 63, 0x2B), (0x84, 63, 0x2C), (0x85, 2, 0x2D), (0xA0, 1, 0x2E),
    (0xE0, 63, 0x2F), (0xF0, 63, 0x30), (0xF1, 63, 0x31), (0xF2, 15, 0x32),
    (0xF4, 63, 0x35), (0xF5, 3, 0x36),
]
FEATURE_SIZE = {rid: n + 1 for rid, n, _ in FEATURES}

USB_INPUT_SIZE = 64    # report 0x01
USB_OUTPUT_SIZE = 48   # report 0x02
BT_REPORT_SIZE = 78    # report 0x31, in and out


# ---------------------------------------------------------------------------
# HID report descriptor (same report layout as the real wired controller)
# ---------------------------------------------------------------------------
def _i(tag, value=None, size=1):
    if value is None:
        return bytes([tag])
    fmt = {1: "<B", 2: "<H", 4: "<I"}[size]
    return bytes([tag | {1: 1, 2: 2, 4: 3}[size]]) + struct.pack(fmt, value)


USAGE_PAGE, USAGE, USAGE_MIN, USAGE_MAX = 0x04, 0x08, 0x18, 0x28
LOG_MIN, LOG_MAX, PHYS_MIN, PHYS_MAX, UNIT = 0x14, 0x24, 0x34, 0x44, 0x64
REPORT_SIZE, REPORT_ID, REPORT_COUNT = 0x74, 0x84, 0x94
INPUT, OUTPUT, FEATURE, COLLECTION = 0x80, 0x90, 0xB0, 0xA0


def _report_descriptor():
    d = _i(USAGE_PAGE, 0x01) + _i(USAGE, 0x05) + _i(COLLECTION, 0x01)
    d += _i(REPORT_ID, 0x01)
    # LX, LY, RX, RY, L2, R2
    for u in (0x30, 0x31, 0x32, 0x35, 0x33, 0x34):
        d += _i(USAGE, u)
    d += _i(LOG_MIN, 0) + _i(LOG_MAX, 255, 2) + _i(REPORT_SIZE, 8) + _i(REPORT_COUNT, 6) + _i(INPUT, 0x02)
    # sequence counter
    d += _i(USAGE_PAGE, 0xFF00, 2) + _i(USAGE, 0x20) + _i(REPORT_COUNT, 1) + _i(INPUT, 0x02)
    # d-pad
    d += _i(USAGE_PAGE, 0x01) + _i(USAGE, 0x39) + _i(LOG_MIN, 0) + _i(LOG_MAX, 7)
    d += _i(PHYS_MIN, 0) + _i(PHYS_MAX, 315, 2) + _i(UNIT, 0x14)
    d += _i(REPORT_SIZE, 4) + _i(REPORT_COUNT, 1) + _i(INPUT, 0x42)
    d += _i(UNIT, 0) + _i(PHYS_MAX, 0)
    # 15 buttons
    d += _i(USAGE_PAGE, 0x09) + _i(USAGE_MIN, 1) + _i(USAGE_MAX, 15) + _i(LOG_MIN, 0) + _i(LOG_MAX, 1)
    d += _i(REPORT_SIZE, 1) + _i(REPORT_COUNT, 15) + _i(INPUT, 0x02)
    # vendor bits, then touch / IMU / battery block
    d += _i(USAGE_PAGE, 0xFF00, 2) + _i(USAGE, 0x21) + _i(REPORT_COUNT, 13) + _i(INPUT, 0x02)
    d += _i(USAGE, 0x22) + _i(LOG_MIN, 0) + _i(LOG_MAX, 255, 2) + _i(REPORT_SIZE, 8)
    d += _i(REPORT_COUNT, 52) + _i(INPUT, 0x02)
    # output report 0x02 (rumble, triggers, LEDs)
    d += _i(REPORT_ID, 0x02) + _i(USAGE, 0x23) + _i(REPORT_COUNT, 47) + _i(OUTPUT, 0x02)
    for rid, size, usage in FEATURES:
        d += _i(REPORT_ID, rid) + _i(USAGE, usage) + _i(REPORT_COUNT, size) + _i(FEATURE, 0x02)
    return d + bytes([0xC0])


REPORT_DESC = _report_descriptor()

DEVICE_DESC = struct.pack(
    "<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0, 0, 0, 64, VID, PID, 0x0100, 1, 2, 0, 1)

# Interfaces 0-2: USB Audio 1.0 (speaker + haptics out, mic in), copied from a real
# wired DualSense except the iso endpoints' bInterval: the real 4 makes usbip-win2
# never start the stream, the spec-standard 1 works. See usb_audio.py.
_AUDIO_IFACES = bytes.fromhex(
    "090400000001010000"                      # if 0: audio control
    "0a240100014900020102"                    # AC header, interfaces 1,2
    "0c2402010101060433000000"                # input terminal 1: USB stream, 4ch
    "0c2406020101030000000000"                # feature unit 2: mute+volume
    "092403030103040200"                      # output terminal 3: speaker
    "0c2402040204030203000000"                # input terminal 4: mic
    "092406050401030000"                      # feature unit 5
    "092403060101010500"                      # output terminal 6: USB stream
    "090401000001020000"                      # if 1 alt 0: no bandwidth
    "090401010101020000"                      # if 1 alt 1: speaker/haptics
    "07240101010100"
    "0b2402010402100180bb00"                  # 4ch, 16-bit, 48 kHz
    "090501098801010000"                      # EP 0x01 iso OUT, 392 bytes
    "07250100000000"
    "090402000001020000"                      # if 2 alt 0
    "090402010101020000"                      # if 2 alt 1: mic
    "07240106010100"
    "0b2402010202100180bb00"                  # 2ch, 16-bit, 48 kHz
    "09058205c400010000"                      # EP 0x82 iso IN, 196 bytes
    "07250100000000"
)
HID_DESC = struct.pack("<BBHBBBH", 9, 0x21, 0x0111, 0, 1, 0x22, len(REPORT_DESC))
_hid_iface = struct.pack("<BBBBBBBBB", 9, 4, 3, 0, 2, 0x03, 0, 0, 0)
_ep_in = struct.pack("<BBBBHB", 7, 5, 0x84, 0x03, 64, 6)
_ep_out = struct.pack("<BBBBHB", 7, 5, 0x03, 0x03, 64, 6)
_cfg_body = _AUDIO_IFACES + _hid_iface + HID_DESC + _ep_in + _ep_out
CONFIG_DESC = struct.pack("<BBHBBBBB", 9, 2, 9 + len(_cfg_body), 4, 1, 0, 0xC0, 250) + _cfg_body
HID_INTERFACE = 3

STRINGS = {1: "Sony Interactive Entertainment", 2: "DualSense Wireless Controller"}


def _string_desc(idx):
    if idx == 0:
        return bytes([4, 3, 0x09, 0x04])
    if idx not in STRINGS:
        return None
    s = STRINGS[idx].encode("utf-16-le")
    return bytes([2 + len(s), 3]) + s


# ---------------------------------------------------------------------------
# Bluetooth CRC helpers (CRC32 over a one-byte seed + report)
# ---------------------------------------------------------------------------
def _bt_crc(seed, data):
    return struct.pack("<I", zlib.crc32(bytes([seed]) + data) & 0xFFFFFFFF)


# Rumble flags in the output payload. Classic rumble always needs HAPTICS_SELECT
# (valid_flag0 bit 1); the enable bit is valid_flag0 bit 0 on firmware older than
# update version 2.21, and valid_flag2 bit 2 on newer firmware.
VALID_FLAG0, VALID_FLAG2 = 0, 38
HAPTICS_SELECT, RUMBLE_V1_BIT, RUMBLE_V2_BIT = 0x02, 0x01, 0x04
VIBRATION_V2_MIN_VERSION = 0x0215


def firmware_uses_vibration_v2(fw_report):
    """fw_report: feature report 0x20. The update version lives at byte 44."""
    if not fw_report or len(fw_report) < 46:
        return True  # unknown: assume current firmware
    return struct.unpack_from("<H", fw_report, 44)[0] >= VIBRATION_V2_MIN_VERSION


def normalize_rumble(payload, vibration_v2):
    """Rewrite the rumble flag so it matches this controller's firmware,
    whichever style the game used."""
    p = bytearray(payload)
    if p[VALID_FLAG0] & RUMBLE_V1_BIT or p[VALID_FLAG2] & RUMBLE_V2_BIT:
        p[VALID_FLAG0] |= HAPTICS_SELECT
        if vibration_v2:
            p[VALID_FLAG0] &= ~RUMBLE_V1_BIT & 0xFF
            p[VALID_FLAG2] |= RUMBLE_V2_BIT
        else:
            p[VALID_FLAG0] |= RUMBLE_V1_BIT
            p[VALID_FLAG2] &= ~RUMBLE_V2_BIT & 0xFF
    return bytes(p)


def usb_output_to_bt(usb, seq, vibration_v2=True):
    """USB output report 0x02 (48 bytes) -> BT output report 0x31 (78 bytes)."""
    payload = bytes(usb[1:USB_OUTPUT_SIZE]).ljust(47, b"\0")
    payload = normalize_rumble(payload, vibration_v2)
    body = bytes([0x31, (seq & 0x0F) << 4, 0x10]) + payload
    body = body.ljust(BT_REPORT_SIZE - 4, b"\0")
    return body + _bt_crc(0xA2, body)


def bt_input_to_usb(bt):
    """BT input report 0x31 -> USB input report 0x01 (64 bytes)."""
    return bytes([0x01]) + bytes(bt[2:65])


# ---------------------------------------------------------------------------
# The emulated device
# ---------------------------------------------------------------------------
class WiredDualSense:
    busid = "1-1"

    def __init__(self, bt):
        self.bt = bt                    # BluetoothDualSense
        self._lock = threading.Lock()
        self._pending_in = deque()      # interrupt IN URBs waiting for a report
        self._latest = bytes([0x01]) + bytes(63)
        self._fresh = False
        self._server = None
        self._seen = set()              # host requests already logged once
        self.speaker = SpeakerStream(on_start=self._speaker_started,
                                     on_stop=self._speaker_stopped)
        self.media = MediaStreamer(bt.write_media, self.speaker)
        self.haptics = HapticsConverter(self.media.put)
        self.mic = MicStream()
        bt.on_mic = self.mic.packet
        self.audio = AudioFunction(on_haptics=self.haptics.feed, on_speaker=self.speaker.feed,
                                   mic=self.mic)
        bt.on_input = self._on_bt_input

    def _speaker_stopped(self):
        history, total, _ = self.bt.link.snapshot()
        recent = [x for x in history if x.start >= self._speaker_since]
        worst = max((x.worst for x in recent), default=0) * 1000
        dropouts = total.dropouts - self._speaker_dropouts
        print(f"[audio] speaker stream stopped: {self.speaker.summary()}; Bluetooth meanwhile: "
              f"{dropouts} input gaps over 10 ms, longest {worst:.0f} ms")

    def _speaker_started(self):
        _, total, _ = self.bt.link.snapshot()
        self._speaker_dropouts, self._speaker_since = total.dropouts, time.perf_counter()
        self._load_debug()
        print(f"[audio] speaker stream started ({'headphones' if self.bt.headset else 'speaker'}, "
              f"report 0x{self.bt.speaker_format:02x})")
        self.bt.write_usb_output(speaker_setup_output(self.bt.headset))

    def _load_debug(self):
        """Development knobs, read each time a speaker stream starts so they can be tried
        without restarting: %LOCALAPPDATA%\\DualSenseBridge\\debug.json, e.g.
        {"speaker_format": 57, "speaker_buffers": [64, 64, 64, 64], "ps5_config": [81, 31, 98, 40]}"""
        path = os.path.join(os.environ.get("LOCALAPPDATA", "."), "DualSenseBridge", "debug.json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.bt.speaker_format = d.get("speaker_format", self.bt.speaker_format)
        self.bt.speaker_buffers = tuple(d.get("speaker_buffers", self.bt.speaker_buffers))
        self.bt.ps5_config = tuple(d.get("ps5_config", self.bt.ps5_config))
        self.mic.PREBUFFER = d.get("mic_prebuffer", type(self.mic).PREBUFFER)
        print(f"[audio] debug settings: {d}")

    def _log_once(self, key, msg):
        if key not in self._seen:
            self._seen.add(key)
            print(f"[usb] {msg}")

    # -- usbip_server device interface ------------------------------------
    def info(self):
        return dict(vid=VID, pid=PID, bcd=0x0100, cls=0, subcls=0, proto=0)

    def interfaces(self):
        return [(0x01, 0x01, 0), (0x01, 0x02, 0), (0x01, 0x02, 0), (0x03, 0, 0)]

    def attached(self, server):
        self._server = server
        self._seen.clear()
        self.audio.attached(server)

    def detached(self):
        with self._lock:
            self._pending_in.clear()
        self.audio.detached()
        self._server = None

    def cancel(self, seq):
        with self._lock:
            for urb in self._pending_in:
                if urb.seq == seq:
                    self._pending_in.remove(urb)
                    return True
        return self.audio.cancel(seq)

    def submit(self, urb, server):
        if urb.ep == 0:
            return self._control(urb)
        if urb.iso:
            self._log_once(("iso", urb.ep), f"audio stream started on endpoint {urb.ep} "
                                            f"({'mic' if urb.dir_in else 'speaker/haptics'})")
            return self.audio.submit(urb)
        if urb.ep == 4 and urb.dir_in:
            with self._lock:
                if self._fresh:
                    self._fresh = False
                    return 0, self._latest
                self._pending_in.append(urb)
            return None
        if urb.ep == 3 and not urb.dir_in:
            self._output(urb.data)
            return 0, b""
        return EPIPE, b""

    # -- input path -------------------------------------------------------
    def _on_bt_input(self, usb_report):
        with self._lock:
            self._latest = usb_report
            urb = self._pending_in.popleft() if self._pending_in else None
            self._fresh = urb is None
        if urb and self._server:
            self._server.complete(urb, 0, usb_report)

    # -- output path ------------------------------------------------------
    def _output(self, data):
        if data and data[0] == 0x02:
            self._log_once("output", f"host sent first output report: {bytes(data[:12]).hex()}")
            self.bt.write_usb_output(data)

    # -- control endpoint -------------------------------------------------
    def _control(self, urb):
        rtype, req, value, index, length = struct.unpack("<BBHHH", urb.setup)
        dtype, didx = value >> 8, value & 0xFF

        if rtype == 0x80 and req == 0x06:  # GET_DESCRIPTOR (device)
            desc = {1: DEVICE_DESC, 2: CONFIG_DESC}.get(dtype)
            if dtype == 3:
                desc = _string_desc(didx)
            return (0, desc) if desc else (EPIPE, b"")
        if rtype == 0x81 and req == 0x06 and index == HID_INTERFACE:  # GET_DESCRIPTOR (HID)
            desc = {0x21: HID_DESC, 0x22: REPORT_DESC}.get(dtype)
            return (0, desc) if desc else (EPIPE, b"")
        if rtype == 0x80 and req == 0x00:  # GET_STATUS
            return 0, b"\0\0"
        if rtype == 0x80 and req == 0x08:  # GET_CONFIGURATION
            return 0, b"\x01"
        if rtype in (0x00, 0x01, 0x02) and req in (0x01, 0x03, 0x09, 0x0B):
            if req == 0x0B:  # SET_INTERFACE
                self._log_once(("alt", index, value), f"host selected interface {index} alt {value}")
                if index == 2:   # the mic stream: Windows opens it when an app records
                    print(f"[usb] mic interface alt {value} ({self.mic.summary()})")
                    self.mic.reset()
                    self.bt.set_mic(value == 1)
            return 0, b""  # CLEAR/SET_FEATURE, SET_CONFIGURATION, SET_INTERFACE

        # Audio class: interface 0 entities, or the iso endpoints
        if (rtype & 0x7F == 0x21 and index & 0xFF == 0) or rtype & 0x7F == 0x22:
            return self.audio.control(rtype, req, value, index, length, urb.data)

        if rtype == 0x21 and req in (0x0A, 0x0B):  # HID SET_IDLE / SET_PROTOCOL
            return 0, b""
        if rtype == 0x21 and req == 0x09:  # HID SET_REPORT
            if dtype == 2:
                self._output(urb.data)
                return 0, b""
            if dtype == 3:
                # Feature SETs configure the USB side of the controller (e.g. 0x08 from
                # Steam); forwarding them over Bluetooth makes the controller drop the
                # connection, so acknowledge them without passing them on.
                self._log_once(("setf", didx), f"host set feature 0x{didx:02x} (not forwarded): "
                                               f"{bytes(urb.data[:16]).hex()}")
                return 0, b""
        if rtype == 0xA1 and req == 0x01:  # HID GET_REPORT
            if dtype == 1:
                return 0, self._latest
            if dtype == 3 and didx in FEATURE_SIZE:
                data = self.bt.get_feature(didx, FEATURE_SIZE[didx])
                self._log_once(("getf", didx), f"host read feature 0x{didx:02x}"
                                               + ("" if data else " (failed, stalled)"))
                return (0, data) if data else (EPIPE, b"")

        print(f"[usb] unhandled control {urb.setup.hex()}")
        return EPIPE, b""
