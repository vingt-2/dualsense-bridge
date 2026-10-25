"""Self-test of the emulated wired DualSense over USB/IP, with a fake Bluetooth side.

Acts as a USB/IP client (like usbip-win2 would) and checks descriptors, HID input and
output, URB unlinking, isochronous audio in both directions and the haptics path.
No controller or drivers needed:  python tests\\test_usbip_device.py
"""
import array
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()   # don't pick up the user's debug.json
from dualsense_usb import CONFIG_DESC, REPORT_DESC, WiredDualSense, bt_input_to_usb, usb_output_to_bt
from usbip_server import UsbIpServer
from link_stats import LinkStats
import opus

PORT = 13240


class FakeBluetooth:
    on_input = on_mic = None
    headset = False
    speaker_format = 0x36
    link = LinkStats()

    def __init__(self):
        self.outputs, self.haptics, self.frames, self.mic = [], [], [], []

    def write_usb_output(self, report):
        self.outputs.append(usb_output_to_bt(report, 0))

    def write_media(self, block, frame=None):
        if block:
            self.haptics.append(block)
        if frame:
            self.frames.append(frame)

    def set_mic(self, on):
        self.mic.append(on)

    def get_feature(self, rid, size):
        return bytes([rid]) + bytes(size - 1)


def report_sizes(desc):
    """bytes per (kind, report id), from walking the HID report descriptor"""
    i, rsize, rcount, rid, sizes = 0, 0, 0, 0, {}
    while i < len(desc):
        b = desc[i]
        n = [0, 1, 2, 4][b & 3]
        val = int.from_bytes(desc[i + 1:i + 1 + n], "little")
        tag, i = b & 0xFC, i + 1 + n
        if tag == 0x74:
            rsize = val
        elif tag == 0x94:
            rcount = val
        elif tag == 0x84:
            rid = val
        elif tag in (0x80, 0x90, 0xB0):
            key = ({0x80: "in", 0x90: "out", 0xB0: "feature"}[tag], rid)
            sizes[key] = sizes.get(key, 0) + rsize * rcount
    return {k: v // 8 for k, v in sizes.items()}


def recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        assert chunk, "server closed the connection"
        buf += chunk
    return buf


def main():
    sizes = report_sizes(REPORT_DESC)
    assert sizes[("in", 1)] == 63 and sizes[("out", 2)] == 47 and sizes[("feature", 5)] == 40
    assert sum(1 for k in sizes if k[0] == "feature") == 22

    bt = FakeBluetooth()
    dev = WiredDualSense(bt)
    server = UsbIpServer(dev, port=PORT)
    server.bind()
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # device list
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall(struct.pack(">HHI", 0x0111, 0x8005, 0))
    _, code, status, ndev = struct.unpack(">HHII", recv(s, 12))
    assert (code, status, ndev) == (0x0005, 0, 1)
    recv(s, 312 + 4 * 4)
    s.close()

    # import
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall(struct.pack(">HHI", 0x0111, 0x8003, 0) + b"1-1".ljust(32, b"\0"))
    _, code, status = struct.unpack(">HHI", recv(s, 8))
    info = recv(s, 312)
    assert status == 0 and struct.unpack(">HH", info[300:304]) == (0x054C, 0x0CE6)

    def submit(seq, ep, dir_in, length, setup=bytes(8), data=b"", npackets=-1, iso=b""):
        s.sendall(struct.pack(">IIIIIIiiiI", 1, seq, 0x10002, int(dir_in), ep, 0, length, 0, npackets, 0)
                  + setup + data + iso)

    def ret(dir_in=True, iso=False):
        h = recv(s, 48)
        cmd, seq = struct.unpack(">II", h[:8])
        if cmd == 4:
            return cmd, seq, struct.unpack(">i", h[20:24])[0], b""
        status, actual, _, npackets, _ = struct.unpack(">iiiii", h[20:40])
        data = recv(s, actual) if actual and dir_in else b""
        if iso:
            recv(s, 16 * npackets)
        return cmd, seq, status, data

    def control(seq, rtype, req, value, index, length):
        submit(seq, 0, True, length, struct.pack("<BBHHH", rtype, req, value, index, length))
        return ret()

    assert control(1, 0x80, 6, 0x0100, 0, 18)[3][8:12] == bytes.fromhex("4c05e60c")
    assert control(2, 0x80, 6, 0x0200, 0, 255)[3] == CONFIG_DESC
    assert control(3, 0x81, 6, 0x2200, 3, 512)[3] == REPORT_DESC
    assert control(4, 0x80, 6, 0x0302, 0x409, 255)[3][2:].decode("utf-16-le") == "DualSense Wireless Controller"
    assert control(5, 0x80, 6, 0x0600, 0, 10)[2] == -32      # device qualifier: stall
    assert len(control(6, 0xA1, 1, 0x0305, 3, 41)[3]) == 41  # feature 0x05 on HID interface 3

    # interrupt IN waits for a Bluetooth input report
    submit(10, 4, True, 64)
    time.sleep(0.1)
    bt.on_input(bt_input_to_usb(bytes([0x31, 0x00]) + bytes(range(1, 64)) + bytes(13)))
    _, seq, status, data = ret()
    assert (seq, status, len(data)) == (10, 0, 64) and data[:3] == bytes([1, 1, 2])

    # unlinking a pending IN
    submit(11, 4, True, 64)
    time.sleep(0.05)
    s.sendall(struct.pack(">IIIIII", 2, 12, 0x10002, 0, 4, 11) + bytes(24))
    assert ret()[:3] == (4, 12, -104)

    # interrupt OUT -> Bluetooth output report 0x31 with CRC
    submit(13, 3, False, 48, data=bytes([0x02, 0x03, 0x00, 0x80, 0x80]) + bytes(43))
    assert ret(dir_in=False)[2] == 0
    pkt = bt.outputs[-1]
    assert len(pkt) == 78 and pkt[:3] == bytes([0x31, 0, 0x10])
    assert pkt[-4:] == struct.pack("<I", zlib.crc32(b"\xa2" + pkt[:-4]) & 0xFFFFFFFF)

    # iso OUT on the speaker/haptics endpoint: 4ch 48 kHz, haptics on channels 3-4
    t0 = time.perf_counter()
    for seq in range(20, 30):   # 100 ms, enough for a few haptics blocks
        frames = array.array("h", [0, 0, 20000, -20000] * 48 * 10).tobytes()
        descs = b"".join(struct.pack(">IIIi", i * 384, 384, 0, 0) for i in range(10))
        submit(seq, 1, False, len(frames), data=frames, npackets=10, iso=descs)
    for _ in range(10):
        assert ret(dir_in=False, iso=True)[2] == 0
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed > 80, f"iso OUT completed too fast ({elapsed:.0f} ms), not paced in real time"
    time.sleep(0.1)
    assert bt.haptics and any(bt.haptics[0]), "no haptics blocks produced"

    # speaker audio on channels 1-2 -> 200-byte Opus frames, after routing the audio
    outputs_before = len(bt.outputs)
    for seq in range(30, 40):
        tone = [int(8000 * math.sin(2 * math.pi * 440 * (seq * 480 + i) / 48000)) for i in range(480)]
        frames = array.array("h", [v for t in tone for v in (t, t, 0, 0)]).tobytes()
        descs = b"".join(struct.pack(">IIIi", i * 384, 384, 0, 0) for i in range(10))
        submit(seq, 1, False, len(frames), data=frames, npackets=10, iso=descs)
    for _ in range(10):
        assert ret(dir_in=False, iso=True)[2] == 0
    time.sleep(0.1)
    assert bt.frames and all(len(f) == 200 and f[0] == 0xF4 for f in bt.frames), "no speaker frames"
    setup = bt.outputs[outputs_before]
    assert setup[3] & 0x80 and setup[10] == 0x30, "speaker not routed"

    # iso IN on the mic endpoint: silence until the mic is opened, then its audio
    descs = b"".join(struct.pack(">IIIi", i * 196, 196, 0, 0) for i in range(4))
    submit(40, 2, True, 784, npackets=4, iso=descs)
    _, _, status, data = ret(iso=True)
    assert status == 0 and data == bytes(len(data)) and len(data) == 4 * 192
    assert control(41, 0x01, 0x0B, 1, 2, 0)[2] == 0 and bt.mic == [True]   # SET_INTERFACE 2 alt 1
    enc = opus.Encoder(71)
    for k in range(8):   # more than the mic's 50 ms cushion
        pcm = array.array("h", [int(8000 * math.sin(2 * math.pi * 300 * i / 48000))
                                for i in range(k * 480, (k + 1) * 480) for _ in (0, 1)])
        bt.on_mic(k, enc.encode(pcm))
    submit(42, 2, True, 784, npackets=4, iso=descs)
    _, _, status, data = ret(iso=True)
    assert status == 0 and any(data), "mic audio didn't reach the USB mic"

    print("OK: descriptors, HID in/out, unlink, iso audio, haptics, speaker and mic all pass")


if __name__ == "__main__":
    main()
