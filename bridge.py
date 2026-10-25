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
from media import (MIC_TYPE, PS5_CONFIG, SPEAKER_BUFFERS, audio_control_report,
                   speaker_report, speaker_report_pair)
from link_stats import LinkStats
from usbip_server import UsbIpServer

BT_HID_GUID = b"00001124-0000-1000-8000-00805f9b34fb"
IOCTL_BTH_DISCONNECT_DEVICE = 0x41000C

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
        self.on_buttons = None   # called with the 3 button bytes whenever they change
        self.on_mic = None       # called with (counter, Opus bytes) for each mic packet
        self.mic_enabled = False
        self.headset = False     # play speaker audio on the headphone jack
        # 0x39 (two Opus frames per report, as a PS5 sends) rides over Bluetooth stalls far
        # better than 0x36 (one): ~3 dropouts in 5 s against ~20, same link.
        self.speaker_format = 0x39
        self.speaker_buffers = SPEAKER_BUFFERS
        self.ps5_config = PS5_CONFIG
        self._pending = None         # 0x39: the first half of a report
        self._seq = 0
        self._haptics_counter = 0
        self._haptics_last = 0.0
        self.haptics_depth = DEFAULT_DEPTH   # controller buffer, ~ms (see haptics.py)
        self._io = threading.Lock()  # serialises writes / feature reports
        self.link = LinkStats()      # Bluetooth link quality, for the tray's quality window

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
                    if self.mic_enabled:
                        self.set_mic(True)
                    self.link.connection(True)
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
            buttons = None
            try:
                while True:
                    r = self.dev.read(BT_REPORT_SIZE, 1000)
                    if not r:
                        continue
                    if r[0] == 0x31 and len(r) >= 65:
                        if r[1] & 0x0F == MIC_TYPE:   # microphone audio, not gamepad state
                            if self.on_mic:
                                self.on_mic(r[2], bytes(r[3:74]))
                            continue
                        self.link.input_report()
                        if self.on_input:
                            self.on_input(bt_input_to_usb(r))
                        if self.on_buttons and r[9:12] != buttons:
                            buttons = r[9:12]
                            self.on_buttons(*buttons)
                    elif r[0] == 0x01:
                        simple_reports += 1
                        if simple_reports % 50 == 1:  # still in simple mode; ask again
                            self.get_feature(0x05, 41)
            except OSError as e:
                print(f"[bt] disconnected: {e}")
                self.dev = None
                self.link.connection(False)
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

    def write_media(self, samples, frame=None):
        """samples: interleaved int8 stereo at 3 kHz (see haptics.py), or None;
        frame: an Opus frame for the speaker (see media.py), or None."""
        if not self.dev:
            return
        with self._io:
            # Start each stream (after a pause) at counter 0: with a continuing counter,
            # every other effect played stretched to ~2x its length (measured via IMU).
            now = time.perf_counter()
            if now - self._haptics_last > 0.05:
                self._haptics_counter = 0
            self._haptics_last = now
            if frame is None:
                self._pending = None
                pkt = bt_haptics_report(samples, self._haptics_counter, seq=self._haptics_counter,
                                        depth=self.haptics_depth, mic=self.mic_enabled)
            elif self.speaker_format == 0x39:
                if self._pending is None:
                    self._pending = (samples or bytes(64), frame)
                    return
                (h0, f0), self._pending = self._pending, None
                pkt = speaker_report_pair((h0, samples or bytes(64)), (f0, frame),
                                          self._haptics_counter, self._haptics_counter * 2,
                                          self.mic_enabled, self.headset, self.ps5_config)
            else:
                pkt = speaker_report(samples or bytes(64), frame, self._haptics_counter,
                                     self._haptics_counter, self.haptics_depth,
                                     self.mic_enabled, self.headset, self.speaker_buffers)
            self._haptics_counter = (self._haptics_counter + 1) & 0xFF
            try:
                self.dev.write(pkt)
            except OSError as e:
                print(f"[bt] haptics write failed: {e}")

    def set_mic(self, on):
        """Start or stop the controller streaming its microphone back."""
        self.mic_enabled = on
        if not self.dev:
            return
        with self._io:
            try:
                self.dev.write(audio_control_report(self._haptics_counter, on))
            except OSError as e:
                print(f"[bt] mic control write failed: {e}")
        print(f"[bt] microphone {'on' if on else 'off'}")

    def power_off(self):
        """Turn the controller off by dropping its Bluetooth link (as DS4Windows does):
        the DualSense powers down when the PC disconnects it."""
        info = self.get_feature(0x09, 20)   # pairing info: controller MAC at bytes 1-6, LSB first
        if not info:
            return False
        addr = ctypes.c_uint64(int.from_bytes(info[1:7], "little"))
        bt_api = ctypes.WinDLL("bthprops.cpl")
        k32 = ctypes.windll.kernel32
        bt_api.BluetoothFindFirstRadio.restype = ctypes.c_void_p
        params = ctypes.c_uint32(4)   # BLUETOOTH_FIND_RADIO_PARAMS.dwSize
        radio = ctypes.c_void_p()
        find = bt_api.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(radio))
        if not find:
            print("[bt] power off: no Bluetooth radio found")
            return False
        done = False
        try:
            while True:
                returned = ctypes.c_uint32()
                if k32.DeviceIoControl(radio, IOCTL_BTH_DISCONNECT_DEVICE, ctypes.byref(addr), 8,
                                       None, 0, ctypes.byref(returned), None):
                    done = True
                k32.CloseHandle(radio)
                if done or not bt_api.BluetoothFindNextRadio(ctypes.c_void_p(find), ctypes.byref(radio)):
                    break
        finally:
            bt_api.BluetoothFindRadioClose(ctypes.c_void_p(find))
        print(f"[bt] power off {'sent' if done else 'failed'}")
        return done

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
