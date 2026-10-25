"""List the displays and change which one is the main (primary) display.

Windows has no "set primary" call: the primary display is the one at (0, 0), so every
display is moved by the same offset in one batch (ChangeDisplaySettingsEx with
CDS_NORESET, then applied together), the new one getting CDS_SET_PRIMARY.

Monitor names come from the display configuration API (the names Settings shows); the
older EnumDisplayDevices only says "Generic PnP Monitor". Displays are remembered by
their monitor device path, which survives reboots; \\\\.\\DISPLAYn numbers can change.
"""
import ctypes
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32")

ENUM_CURRENT_SETTINGS = -1
DM_POSITION = 0x20
CDS_UPDATEREGISTRY = 0x01
CDS_SET_PRIMARY = 0x10
CDS_NORESET = 0x10000000
DISP_CHANGE_SUCCESSFUL = 0
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x4
QDC_ONLY_ACTIVE_PATHS = 0x2
GET_SOURCE_NAME, GET_TARGET_NAME = 1, 2


class _DevMode(ctypes.Structure):   # DEVMODEW, display variant
    _fields_ = [("dmDeviceName", wt.WCHAR * 32), ("dmSpecVersion", wt.WORD),
                ("dmDriverVersion", wt.WORD), ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD),
                ("dmFields", wt.DWORD), ("dmPosition", wt.POINT),
                ("dmDisplayOrientation", wt.DWORD), ("dmDisplayFixedOutput", wt.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", wt.WCHAR * 32),
                ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD), ("dmPelsWidth", wt.DWORD),
                ("dmPelsHeight", wt.DWORD), ("dmDisplayFlags", wt.DWORD),
                ("dmDisplayFrequency", wt.DWORD), ("dmICMMethod", wt.DWORD),
                ("dmICMIntent", wt.DWORD), ("dmMediaType", wt.DWORD), ("dmDitherType", wt.DWORD),
                ("dmReserved1", wt.DWORD), ("dmReserved2", wt.DWORD),
                ("dmPanningWidth", wt.DWORD), ("dmPanningHeight", wt.DWORD)]


class _DisplayDevice(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("DeviceName", wt.WCHAR * 32), ("DeviceString", wt.WCHAR * 128),
                ("StateFlags", wt.DWORD), ("DeviceID", wt.WCHAR * 128), ("DeviceKey", wt.WCHAR * 128)]


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _PathInfo(ctypes.Structure):   # DISPLAYCONFIG_PATH_INFO
    _fields_ = [("sourceAdapter", _Luid), ("sourceId", ctypes.c_uint32),
                ("sourceModeIdx", ctypes.c_uint32), ("sourceFlags", ctypes.c_uint32),
                ("targetAdapter", _Luid), ("targetId", ctypes.c_uint32),
                ("targetModeIdx", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wt.BOOL),
                ("targetFlags", ctypes.c_uint32), ("flags", ctypes.c_uint32)]


class _Header(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32),
                ("adapterId", _Luid), ("id", ctypes.c_uint32)]


class _SourceName(ctypes.Structure):
    _fields_ = [("header", _Header), ("viewGdiDeviceName", wt.WCHAR * 32)]


class _TargetName(ctypes.Structure):
    _fields_ = [("header", _Header), ("flags", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("edidManufactureId", ctypes.c_uint16),
                ("edidProductCodeId", ctypes.c_uint16), ("connectorInstance", ctypes.c_uint32),
                ("monitorFriendlyDeviceName", wt.WCHAR * 64), ("monitorDevicePath", wt.WCHAR * 128)]


def _monitor_names():
    """{'\\\\.\\DISPLAY1': (friendly name, monitor device path)} for active displays."""
    n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths),
                                          ctypes.byref(n_modes)):
        return {}
    paths = (_PathInfo * n_paths.value)()
    modes = (ctypes.c_byte * (64 * n_modes.value))()   # DISPLAYCONFIG_MODE_INFO: 64 bytes each
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                 ctypes.byref(n_modes), modes, None):
        return {}
    names = {}
    for p in paths[:n_paths.value]:
        src = _SourceName(_Header(GET_SOURCE_NAME, ctypes.sizeof(_SourceName), p.sourceAdapter, p.sourceId))
        tgt = _TargetName(_Header(GET_TARGET_NAME, ctypes.sizeof(_TargetName), p.targetAdapter, p.targetId))
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(src)) or \
                user32.DisplayConfigGetDeviceInfo(ctypes.byref(tgt)):
            continue
        names[src.viewGdiDeviceName] = (tgt.monitorFriendlyDeviceName, tgt.monitorDevicePath.lower())
    return names


def _current_mode(device):
    dm = _DevMode()
    dm.dmSize = ctypes.sizeof(_DevMode)
    if not user32.EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return None
    return dm


def list_displays():
    """Displays that are part of the desktop: dicts with device, id, name, width, height,
    x, y, primary."""
    names = _monitor_names()
    out, i = [], 0
    while True:
        dd = _DisplayDevice(cb=ctypes.sizeof(_DisplayDevice))
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        i += 1
        if not dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        dm = _current_mode(dd.DeviceName)
        if dm is None:
            continue
        friendly, path = names.get(dd.DeviceName, ("", ""))
        if not path:   # fall back to the monitor's interface path from EnumDisplayDevices
            mon = _DisplayDevice(cb=ctypes.sizeof(_DisplayDevice))
            user32.EnumDisplayDevicesW(dd.DeviceName, 0, ctypes.byref(mon), 1)
            path = mon.DeviceID.lower() or dd.DeviceName
        out.append({"device": dd.DeviceName, "id": path,
                    "name": friendly or dd.DeviceName.replace("\\\\.\\", ""),
                    "width": dm.dmPelsWidth, "height": dm.dmPelsHeight,
                    "x": dm.dmPosition.x, "y": dm.dmPosition.y,
                    "primary": bool(dd.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE)})
    return out


def label(d):
    number = d["device"].replace("\\\\.\\DISPLAY", "")
    return f"{d['name']} (display {number}, {d['width']}×{d['height']})"


def set_primary(device):
    """Make this display (e.g. '\\\\.\\DISPLAY2') the primary one."""
    displays = list_displays()
    new = next((d for d in displays if d["device"] == device), None)
    if new is None:
        raise OSError(f"{device} is not part of the desktop")
    if new["primary"]:
        return
    dx, dy = new["x"], new["y"]
    # The new primary must be the one at (0, 0): shift everything by its position.
    for d in sorted(displays, key=lambda d: d["device"] != device):
        dm = _current_mode(d["device"])
        dm.dmFields = DM_POSITION
        dm.dmPosition.x, dm.dmPosition.y = d["x"] - dx, d["y"] - dy
        flags = CDS_UPDATEREGISTRY | CDS_NORESET
        if d["device"] == device:
            flags |= CDS_SET_PRIMARY
        r = user32.ChangeDisplaySettingsExW(d["device"], ctypes.byref(dm), None, flags, None)
        if r != DISP_CHANGE_SUCCESSFUL:
            raise OSError(f"Windows refused the change for {d['device']} (code {r})")
    r = user32.ChangeDisplaySettingsExW(None, None, None, 0, None)   # apply them all at once
    if r != DISP_CHANGE_SUCCESSFUL:
        raise OSError(f"Windows refused the display change (code {r})")
