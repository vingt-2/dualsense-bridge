"""Minimal libopus binding (ctypes) for the controller's Bluetooth speaker and mic audio.

opus.dll comes from the PyOgg package (BSD-licensed libopus build); in the frozen exe it
is bundled next to the code (see build.ps1).
"""
import ctypes
import importlib.util
import os
import sys

RATE = 48000
OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051   # CELT only, like the PS5's speaker stream
OPUS_SET_BITRATE = 4002
OPUS_SET_VBR = 4006
OPUS_SET_COMPLEXITY = 4010


def _find_dll():
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(here, "opus.dll")]
    spec = importlib.util.find_spec("pyogg")
    if spec and spec.submodule_search_locations:
        candidates += [os.path.join(p, "opus.dll") for p in spec.submodule_search_locations]
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise OSError("opus.dll not found (pip install PyOgg)")


_lib = None


def _opus():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(_find_dll())
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = (ctypes.c_int32, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int))
        lib.opus_encoder_ctl.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int32)
        lib.opus_encode.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_int32)
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = (ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
        lib.opus_decode.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
                                    ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
        _lib = lib
    return _lib


class Encoder:
    """Stereo 48 kHz, constant bitrate: every 10 ms frame comes out exactly frame_bytes."""

    def __init__(self, frame_bytes=200, complexity=5):
        lib = _opus()
        err = ctypes.c_int()
        self._st = lib.opus_encoder_create(RATE, 2, OPUS_APPLICATION_RESTRICTED_LOWDELAY, ctypes.byref(err))
        if err.value:
            raise OSError(f"opus_encoder_create failed ({err.value})")
        self.frame_bytes = frame_bytes
        lib.opus_encoder_ctl(self._st, OPUS_SET_BITRATE, frame_bytes * 8 * 100)
        lib.opus_encoder_ctl(self._st, OPUS_SET_VBR, 0)
        lib.opus_encoder_ctl(self._st, OPUS_SET_COMPLEXITY, complexity)
        self._out = (ctypes.c_ubyte * (frame_bytes * 2))()

    def encode(self, pcm):
        """pcm: array('h') of 480 interleaved stereo frames -> frame_bytes of Opus."""
        buf = (ctypes.c_short * len(pcm)).from_buffer(pcm)
        n = _opus().opus_encode(self._st, buf, len(pcm) // 2, self._out, self.frame_bytes)
        if n < 0:
            raise OSError(f"opus_encode failed ({n})")
        return bytes(self._out[:n]).ljust(self.frame_bytes, b"\0")


class Decoder:
    def __init__(self, channels=2):
        lib = _opus()
        err = ctypes.c_int()
        self.channels = channels
        self._st = lib.opus_decoder_create(RATE, channels, ctypes.byref(err))
        if err.value:
            raise OSError(f"opus_decoder_create failed ({err.value})")
        self._pcm = (ctypes.c_short * (960 * channels))()   # up to 20 ms

    def decode(self, data, frames=480):
        """Opus packet (None: conceal a lost one) -> list of int16, interleaved, or []."""
        if data is None:
            n = _opus().opus_decode(self._st, None, 0, self._pcm, frames, 0)
        else:
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            n = _opus().opus_decode(self._st, buf, len(data), self._pcm, 960, 0)
        return self._pcm[:n * self.channels] if n > 0 else []
