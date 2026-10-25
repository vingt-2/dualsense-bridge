"""Keep Windows' default playback/recording devices when the virtual controller's
sound device appears (Windows otherwise switches the default to it, like it does
for a real wired DualSense, and PC sound goes silent).

Uses IMMDeviceEnumerator to read the defaults and the undocumented but widely
used IPolicyConfig interface to set them back.
"""
import ctypes
import time
import uuid
from ctypes import wintypes as wt

ole32 = ctypes.OleDLL("ole32")
CLSCTX_ALL = 0x17
E_RENDER, E_CAPTURE = 0, 1
ROLES = (0, 1, 2)  # console, multimedia, communications


def _guid(s):
    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(s).bytes_le)


CLSID_MMDeviceEnumerator = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
CLSID_PolicyConfig = _guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")
IID_IPolicyConfig = _guid("F8679F50-850A-41CF-9C72-430F290290C8")


def _call(obj, index, *args, argtypes=()):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _release(obj):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


def _create(clsid, iid):
    obj = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid), ctypes.byref(obj))
    return obj


def get_defaults():
    """{(flow, role): endpoint id} for the current default devices."""
    ole32.CoInitialize(None)
    enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    out = {}
    try:
        for flow in (E_RENDER, E_CAPTURE):
            for role in ROLES:
                dev = ctypes.c_void_p()
                try:  # IMMDeviceEnumerator::GetDefaultAudioEndpoint
                    _call(enum, 4, flow, role, ctypes.byref(dev),
                          argtypes=(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))
                except OSError:
                    continue  # no device for this role
                pid = wt.LPWSTR()
                _call(dev, 5, ctypes.byref(pid), argtypes=(ctypes.POINTER(wt.LPWSTR),))  # GetId
                out[(flow, role)] = pid.value
                ole32.CoTaskMemFree(pid)
                _release(dev)
    finally:
        _release(enum)
    return out


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3),
                ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", ctypes.c_ulong)]


PKEY_Device_FriendlyName = _PropertyKey(_guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)
DEVICE_STATE_ACTIVE = 1
VT_LPWSTR = 31


def list_outputs():
    """[(endpoint id, friendly name)] of the playback devices that are plugged in."""
    ole32.CoInitialize(None)
    enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    out = []
    try:
        coll = ctypes.c_void_p()  # IMMDeviceEnumerator::EnumAudioEndpoints
        _call(enum, 3, E_RENDER, DEVICE_STATE_ACTIVE, ctypes.byref(coll),
              argtypes=(ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)))
        count = ctypes.c_uint()
        _call(coll, 3, ctypes.byref(count), argtypes=(ctypes.POINTER(ctypes.c_uint),))  # GetCount
        for i in range(count.value):
            dev, pid, store = ctypes.c_void_p(), wt.LPWSTR(), ctypes.c_void_p()
            _call(coll, 4, i, ctypes.byref(dev), argtypes=(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)))
            _call(dev, 5, ctypes.byref(pid), argtypes=(ctypes.POINTER(wt.LPWSTR),))  # GetId
            endpoint_id = pid.value
            ole32.CoTaskMemFree(pid)
            name = endpoint_id
            _call(dev, 4, 0, ctypes.byref(store),  # OpenPropertyStore(STGM_READ)
                  argtypes=(ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)))
            var = _PropVariant()
            _call(store, 5, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(var),  # GetValue
                  argtypes=(ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant)))
            if var.vt == VT_LPWSTR:
                name = ctypes.wstring_at(var.value)
            ole32.PropVariantClear(ctypes.byref(var))
            _release(store)
            _release(dev)
            out.append((endpoint_id, name))
        _release(coll)
    finally:
        _release(enum)
    return sorted(out, key=lambda d: d[1].lower())


def set_output(endpoint_id):
    """Make this the default playback device for every role."""
    for role in ROLES:
        set_default(endpoint_id, role)


def set_default(endpoint_id, role):
    ole32.CoInitialize(None)
    pc = _create(CLSID_PolicyConfig, IID_IPolicyConfig)
    try:  # IPolicyConfig::SetDefaultEndpoint
        _call(pc, 13, endpoint_id, role, argtypes=(wt.LPCWSTR, ctypes.c_int))
    finally:
        _release(pc)


def guard_defaults(before, seconds=15, playback_free=None):
    """After the virtual device is attached, put back any default that Windows
    switched away from `before`. Runs for `seconds`, since the sound device
    shows up a little after the USB attach. Once the `playback_free` event is set,
    playback defaults are left alone (the bridge chose one on purpose)."""
    restored = set()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        now = get_defaults()
        for key, original in before.items():
            if key[0] == E_RENDER and playback_free and playback_free.is_set():
                continue
            if now.get(key) != original:
                set_default(original, key[1])
                if key not in restored:
                    restored.add(key)
                    kind = "playback" if key[0] == E_RENDER else "recording"
                    print(f"[audio] restored default {kind} device (role {key[1]})")
        time.sleep(0.5)
