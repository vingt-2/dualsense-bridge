"""DualSense Bluetooth -> wired USB bridge.

Usage:
    python bridge.py            # start the bridge, then: usbip attach -r 127.0.0.1 -b 1-1
"""
import ctypes
import threading
import time

import hid

from dualsense_usb import (BT_REPORT_SIZE, VID, PID, WiredDualSense, bt_input_to_usb,
                           firmware_uses_vibration_v2, usb_output_to_bt, _bt_crc)
from haptics import DEFAULT_DEPTH, bt_haptics_report
from usbip_server import UsbIpServer

BT_HID_GUID = b"00001124-0000-1000-8000-00805f9b34fb"

# Windows' default timer ticks every 15.6 ms, which makes time.sleep() far too coarse
# for pacing audio and haptics (a 5 ms sleep takes 15.6 ms). Ask for 1 ms.
ctypes.WinDLL("winmm").timeBeginPeriod(1)


class BluetoothDualSense:
    def __init__(self):
        self.dev = None
        self.path = None
        self.vibration_v2 = True
        self.on_input = None
        self.on_state = None  # called with True/False on connect/disconnect
        self.before_open = None  # called with the HID path before the controller is opened
        self._seq = 0
        self._haptics_counter = 0
        self._haptics_last = 0.0
        self.haptics_depth = DEFAULT_DEPTH   # controller buffer, ~ms (see haptics.py)
        self._io = threading.Lock()  # serialises writes / feature reports

    # -- connection -------------------------------------------------------
    def _find(self):
        for d in hid.enumerate(VID, PID):
            if BT_HID_GUID in d["path"].lower():
                return d["path"]
        return None

    def connect_blocking(self):
        while True:
            path = self._find()
            if path:
                if self.before_open:
                    self.before_open(path)
                try:
                    dev = hid.device()
                    dev.open_path(path)
                    self.dev, self.path = dev, path
                    # Reading calibration switches the controller to full 0x31 reports.
                    # If it fails, Windows still lists a controller that is switched off.
                    if self.get_feature(0x05, 41, quiet=True) is None:
                        self.dev = None
                        dev.close()
                        time.sleep(1)
                        continue
                    self.vibration_v2 = firmware_uses_vibration_v2(self.get_feature(0x20, 64))
                    print(f"[bt] DualSense connected (rumble {'v2' if self.vibration_v2 else 'v1'})")
                    if self.on_state:
                        self.on_state(True)
                    return
                except OSError as e:
                    print(f"[bt] open failed: {e}")
            time.sleep(1)

    def run(self):
        while True:
            self.connect_blocking()
            simple_reports = 0
            try:
                while True:
                    r = self.dev.read(BT_REPORT_SIZE, 1000)
                    if not r:
                        continue
                    if r[0] == 0x31 and len(r) >= 65:
                        if self.on_input:
                            self.on_input(bt_input_to_usb(r))
                    elif r[0] == 0x01:
                        simple_reports += 1
                        if simple_reports % 50 == 1:  # still in simple mode; ask again
                            self.get_feature(0x05, 41)
            except OSError as e:
                print(f"[bt] disconnected: {e}")
                self.dev = None
                if self.on_state:
                    self.on_state(False)

    # -- reports ----------------------------------------------------------
    def write_usb_output(self, usb_report):
        if not self.dev:
            return
        with self._io:
            pkt = usb_output_to_bt(usb_report, self._seq, self.vibration_v2)
            self._seq = (self._seq + 1) & 0x0F
            try:
                self.dev.write(pkt)
            except OSError as e:
                print(f"[bt] write failed: {e}")

    def write_haptics(self, samples):
        """samples: interleaved int8 stereo at 3 kHz (see haptics.py)."""
        if not self.dev:
            return
        with self._io:
            # Start each stream (after a pause) at counter 0: with a continuing counter,
            # every other effect played stretched to ~2x its length (measured via IMU).
            now = time.perf_counter()
            if now - self._haptics_last > 0.05:
                self._haptics_counter = 0
            self._haptics_last = now
            pkt = bt_haptics_report(samples, self._haptics_counter, seq=self._haptics_counter,
                                    depth=self.haptics_depth)
            self._haptics_counter = (self._haptics_counter + 1) & 0xFF
            try:
                self.dev.write(pkt)
            except OSError as e:
                print(f"[bt] haptics write failed: {e}")

    def get_feature(self, rid, size, quiet=False):
        """Returns the report in USB form (no CRC), or None."""
        if not self.dev:
            return None
        with self._io:
            try:
                data = bytes(self.dev.get_feature_report(rid, size))
            except OSError as e:
                if not quiet:
                    print(f"[bt] get feature 0x{rid:02x} failed: {e}")
                return None
        # BT feature reports carry a CRC32 (seed 0xA3) in their last 4 bytes; blank it.
        if len(data) >= 5 and _bt_crc(0xA3, data[:-4]) == data[-4:]:
            data = data[:-4] + bytes(4)
        return data.ljust(size, b"\0")


def main():
    bt = BluetoothDualSense()
    usb = WiredDualSense(bt)
    threading.Thread(target=bt.run, daemon=True).start()
    UsbIpServer(usb).serve_forever()


if __name__ == "__main__":
    main()
