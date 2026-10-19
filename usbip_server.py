"""Minimal USB/IP server that exports one emulated USB device.

usbip-win2's virtual host controller connects to this server, so whatever
device object we export appears in Windows as a real, wired USB device.
Protocol reference: https://docs.kernel.org/usb/usbip_protocol.html
"""
import socket
import struct
import threading

USBIP_VERSION = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST = 0x8005, 0x0005
OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4

EPIPE = -32        # stall
ECONNRESET = -104  # URB unlinked

USB_SPEED_FULL = 2


class Urb:
    __slots__ = ("seq", "ep", "dir_in", "length", "setup", "data", "npackets", "start_frame", "iso")

    def __init__(self, seq, ep, dir_in, length, setup, data, npackets, start_frame=0, iso=()):
        self.seq, self.ep, self.dir_in = seq, ep, dir_in
        self.length, self.setup, self.data, self.npackets = length, setup, data, npackets
        self.start_frame = start_frame
        self.iso = iso  # isochronous packets: [(offset, length)]

    def iso_packets(self):
        """OUT iso data split into its packets."""
        return [self.data[off:off + ln] for off, ln in self.iso]


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client disconnected")
        buf += chunk
    return bytes(buf)


class UsbIpServer:
    """Serves a single device. `device` must provide:
        busid, info() -> dict, interfaces() -> [(cls, sub, proto)],
        submit(urb, server) -> None | (status, data), cancel(seq) -> bool,
        attached(server), detached()
    submit() returning None means the URB is pending; the device later calls
    server.complete(urb, status, data).
    """

    def __init__(self, device, host="127.0.0.1", port=3240):
        self.device, self.host, self.port = device, host, port
        self._sock = None
        self._wlock = threading.Lock()

    # ---- sending -------------------------------------------------------
    def _send(self, data):
        with self._wlock:
            if self._sock:
                self._sock.sendall(data)

    def complete(self, urb, status=0, data=b""):
        if urb.iso:
            return self._complete_iso(urb, data)
        if urb.dir_in:
            data = data[: urb.length]
            actual, payload = len(data), data
        else:
            actual, payload = (len(urb.data) if status == 0 else 0), b""
        hdr = struct.pack(">IIIII", RET_SUBMIT, urb.seq, 0, 0, 0)
        hdr += struct.pack(">iiiii", status, actual, 0, urb.npackets, 0) + bytes(8)
        try:
            self._send(hdr + payload)
        except OSError:
            pass

    def _complete_iso(self, urb, packets):
        """packets: for IN, a list of per-packet payloads; ignored for OUT
        (every packet is reported as fully transferred)."""
        descs, payload = b"", b""
        for i, (off, ln) in enumerate(urb.iso):
            if urb.dir_in:
                chunk = packets[i][:ln] if i < len(packets) else b""
                payload += chunk  # IN data is sent packed, without gaps
                actual = len(chunk)
            else:
                actual = ln
            descs += struct.pack(">IIIi", off, ln, actual, 0)
        total = len(payload) if urb.dir_in else sum(ln for _, ln in urb.iso)
        hdr = struct.pack(">IIIII", RET_SUBMIT, urb.seq, 0, 0, 0)
        hdr += struct.pack(">iiiii", 0, total, urb.start_frame, len(urb.iso), 0) + bytes(8)
        try:
            self._send(hdr + payload + descs)
        except OSError:
            pass

    # ---- device info ---------------------------------------------------
    def _devinfo(self):
        i = self.device.info()
        busnum, devnum = (int(x) for x in self.device.busid.split("-"))
        return struct.pack(
            ">256s32sIIIHHHBBBBBB",
            b"/sys/devices/dualsense-bridge", self.device.busid.encode(),
            busnum, devnum, USB_SPEED_FULL,
            i["vid"], i["pid"], i["bcd"], i["cls"], i["subcls"], i["proto"],
            1, 1, len(self.device.interfaces()),
        )

    # ---- connection handling -------------------------------------------
    def bind(self):
        """Bind the listening socket; raises OSError if the port is taken."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind((self.host, self.port))
        srv.listen(1)
        self._listener = srv
        print(f"[usbip] listening on {self.host}:{self.port}, busid {self.device.busid}")

    def serve_forever(self):
        if getattr(self, "_listener", None) is None:
            self.bind()
        while True:
            conn, addr = self._listener.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                self._handle(conn)
            except (ConnectionError, OSError) as e:
                print(f"[usbip] connection closed: {e}")
            finally:
                if self._sock is conn:
                    self._sock = None
                    self.device.detached()
                conn.close()

    def _handle(self, conn):
        version, code, _status = struct.unpack(">HHI", _recv_exact(conn, 8))
        if code == OP_REQ_DEVLIST:
            body = struct.pack(">HHII", USBIP_VERSION, OP_REP_DEVLIST, 0, 1) + self._devinfo()
            for cls, sub, proto in self.device.interfaces():
                body += struct.pack(">BBBB", cls, sub, proto, 0)
            conn.sendall(body)
            return
        if code != OP_REQ_IMPORT:
            raise ConnectionError(f"unexpected op 0x{code:04x}")

        busid = _recv_exact(conn, 32).split(b"\0")[0].decode()
        if busid != self.device.busid or self._sock is not None:
            conn.sendall(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 1))
            return
        conn.sendall(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 0) + self._devinfo())
        print("[usbip] device attached")
        self._sock = conn
        self.device.attached(self)
        self._urb_loop(conn)

    def _urb_loop(self, conn):
        while True:
            hdr = _recv_exact(conn, 48)
            cmd, seq, _devid, direction, ep = struct.unpack(">IIIII", hdr[:20])
            if cmd == CMD_SUBMIT:
                _flags, length, start_frame, npackets, _interval = struct.unpack(">IiiiI", hdr[20:40])
                setup = hdr[40:48]
                dir_in = direction == 1
                data = _recv_exact(conn, length) if (not dir_in and length > 0) else b""
                iso = ()
                if npackets > 0:  # iso packet descriptors follow the data
                    raw = _recv_exact(conn, 16 * npackets)
                    iso = [struct.unpack_from(">II", raw, 16 * i) for i in range(npackets)]
                urb = Urb(seq, ep, dir_in, length, setup, data, npackets, start_frame, iso)
                result = self.device.submit(urb, self)
                if result is not None:
                    self.complete(urb, *result)
            elif cmd == CMD_UNLINK:
                (target,) = struct.unpack(">I", hdr[20:24])
                status = ECONNRESET if self.device.cancel(target) else 0
                self._send(struct.pack(">IIIIIi", RET_UNLINK, seq, 0, 0, 0, status) + bytes(24))
            else:
                raise ConnectionError(f"unexpected command {cmd}")
