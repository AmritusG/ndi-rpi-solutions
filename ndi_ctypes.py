"""
ndi_ctypes.py — Lightweight Python ctypes wrapper for the NDI SDK (libndi.so)

Loads libndi.so directly via ctypes. No compilation needed.
Optimized: function signatures configured once, structs pre-allocated.
"""

import ctypes
import ctypes.util
import os
import numpy as np

_ndi = None
_funcs_configured = False


def _load_ndi():
    global _ndi
    if _ndi is not None:
        return _ndi

    search_paths = [
        "libndi.so", "libndi.so.6",
        "/usr/local/lib/libndi.so", "/usr/local/lib/libndi.so.6",
        "/opt/ndi/lib/libndi.so", "/opt/ndi/lib/libndi.so.6",
    ]
    sdk_dir = os.environ.get("NDI_SDK_DIR", "")
    if sdk_dir:
        search_paths.insert(0, os.path.join(sdk_dir, "lib", "aarch64-rpi4-linux-gnueabi", "libndi.so.6"))
    found = ctypes.util.find_library("ndi")
    if found:
        search_paths.insert(0, found)

    for path in search_paths:
        try:
            _ndi = ctypes.CDLL(path)
            _configure_functions()
            return _ndi
        except OSError:
            continue
    raise OSError("Cannot find libndi.so. Run: sudo bash install_ndi_sdk.sh")


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

FRAME_TYPE_NONE     = 0
FRAME_TYPE_VIDEO    = 1
FRAME_TYPE_AUDIO    = 2
FRAME_TYPE_METADATA = 3

FOURCC_VIDEO_TYPE_BGRX = ord('B') | (ord('G') << 8) | (ord('R') << 16) | (ord('X') << 24)
FOURCC_VIDEO_TYPE_UYVY = ord('U') | (ord('Y') << 8) | (ord('V') << 16) | (ord('Y') << 24)

RECV_BANDWIDTH_LOWEST  = 0
RECV_BANDWIDTH_HIGHEST = 100
RECV_COLOR_FORMAT_BGRX_BGRA = 0
RECV_COLOR_FORMAT_UYVY_BGRA = 1
RECV_COLOR_FORMAT_FASTEST   = 100
RECV_COLOR_FORMAT_BEST      = 101


# ═══════════════════════════════════════════════════════════════════════════
# Structures
# ═══════════════════════════════════════════════════════════════════════════

class NDI_source_t(ctypes.Structure):
    _fields_ = [("p_ndi_name", ctypes.c_char_p), ("p_url_address", ctypes.c_char_p)]

class NDI_find_create_t(ctypes.Structure):
    _fields_ = [("show_local_sources", ctypes.c_bool), ("p_groups", ctypes.c_char_p), ("p_extra_ips", ctypes.c_char_p)]

class NDI_send_create_t(ctypes.Structure):
    _fields_ = [("p_ndi_name", ctypes.c_char_p), ("p_groups", ctypes.c_char_p), ("clock_video", ctypes.c_bool), ("clock_audio", ctypes.c_bool)]

class NDI_recv_create_v3_t(ctypes.Structure):
    _fields_ = [("source_to_connect_to", NDI_source_t), ("color_format", ctypes.c_int), ("bandwidth", ctypes.c_int), ("allow_video_fields", ctypes.c_bool), ("p_ndi_recv_name", ctypes.c_char_p)]

class NDI_video_frame_v2_t(ctypes.Structure):
    _fields_ = [
        ("xres", ctypes.c_int), ("yres", ctypes.c_int), ("FourCC", ctypes.c_uint32),
        ("frame_rate_N", ctypes.c_int), ("frame_rate_D", ctypes.c_int),
        ("picture_aspect_ratio", ctypes.c_float), ("frame_format_type", ctypes.c_int),
        ("timecode", ctypes.c_int64), ("p_data", ctypes.c_void_p),
        ("line_stride_in_bytes", ctypes.c_int), ("p_metadata", ctypes.c_char_p),
        ("timestamp", ctypes.c_int64),
    ]

class NDI_audio_frame_v2_t(ctypes.Structure):
    _fields_ = [
        ("sample_rate", ctypes.c_int), ("no_channels", ctypes.c_int),
        ("no_samples", ctypes.c_int), ("timecode", ctypes.c_int64),
        ("p_data", ctypes.c_void_p), ("channel_stride_in_bytes", ctypes.c_int),
        ("p_metadata", ctypes.c_char_p), ("timestamp", ctypes.c_int64),
    ]

class NDI_metadata_frame_t(ctypes.Structure):
    _fields_ = [("length", ctypes.c_int), ("timecode", ctypes.c_int64), ("p_data", ctypes.c_char_p)]


# ═══════════════════════════════════════════════════════════════════════════
# Configure all function signatures ONCE (big perf win — no per-call setup)
# ═══════════════════════════════════════════════════════════════════════════

def _configure_functions():
    global _funcs_configured
    if _funcs_configured:
        return
    lib = _ndi

    lib.NDIlib_initialize.restype = ctypes.c_bool
    lib.NDIlib_destroy.restype = None
    lib.NDIlib_version.restype = ctypes.c_char_p

    lib.NDIlib_find_create_v2.restype = ctypes.c_void_p
    lib.NDIlib_find_create_v2.argtypes = [ctypes.POINTER(NDI_find_create_t)]
    lib.NDIlib_find_wait_for_sources.restype = ctypes.c_bool
    lib.NDIlib_find_wait_for_sources.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.NDIlib_find_get_current_sources.restype = ctypes.POINTER(NDI_source_t)
    lib.NDIlib_find_get_current_sources.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    lib.NDIlib_find_destroy.argtypes = [ctypes.c_void_p]

    lib.NDIlib_send_create.restype = ctypes.c_void_p
    lib.NDIlib_send_create.argtypes = [ctypes.POINTER(NDI_send_create_t)]
    lib.NDIlib_send_send_video_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(NDI_video_frame_v2_t)]
    lib.NDIlib_send_get_no_connections.restype = ctypes.c_int
    lib.NDIlib_send_get_no_connections.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.NDIlib_send_destroy.argtypes = [ctypes.c_void_p]

    lib.NDIlib_recv_create_v3.restype = ctypes.c_void_p
    lib.NDIlib_recv_create_v3.argtypes = [ctypes.POINTER(NDI_recv_create_v3_t)]
    lib.NDIlib_recv_connect.argtypes = [ctypes.c_void_p, ctypes.POINTER(NDI_source_t)]
    lib.NDIlib_recv_capture_v2.restype = ctypes.c_int
    lib.NDIlib_recv_capture_v2.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(NDI_video_frame_v2_t),
        ctypes.POINTER(NDI_audio_frame_v2_t), ctypes.POINTER(NDI_metadata_frame_t),
        ctypes.c_uint32,
    ]
    lib.NDIlib_recv_free_video_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(NDI_video_frame_v2_t)]
    lib.NDIlib_recv_free_audio_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(NDI_audio_frame_v2_t)]
    lib.NDIlib_recv_free_metadata.argtypes = [ctypes.c_void_p, ctypes.POINTER(NDI_metadata_frame_t)]
    lib.NDIlib_recv_destroy.argtypes = [ctypes.c_void_p]

    # PTZ functions
    lib.NDIlib_recv_ptz_is_supported.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_is_supported.argtypes = [ctypes.c_void_p]
    lib.NDIlib_recv_ptz_zoom.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_zoom.argtypes = [ctypes.c_void_p, ctypes.c_float]
    lib.NDIlib_recv_ptz_pan_tilt.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_pan_tilt.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_float]
    lib.NDIlib_recv_ptz_pan_tilt_speed.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_pan_tilt_speed.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_float]
    lib.NDIlib_recv_ptz_auto_focus.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_auto_focus.argtypes = [ctypes.c_void_p]
    lib.NDIlib_recv_ptz_focus.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_focus.argtypes = [ctypes.c_void_p, ctypes.c_float]
    lib.NDIlib_recv_ptz_focus_speed.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_focus_speed.argtypes = [ctypes.c_void_p, ctypes.c_float]
    lib.NDIlib_recv_ptz_store_preset.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_store_preset.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.NDIlib_recv_ptz_recall_preset.restype = ctypes.c_bool
    lib.NDIlib_recv_ptz_recall_preset.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_float]

    _funcs_configured = True


# ═══════════════════════════════════════════════════════════════════════════
# Pre-allocated structs (avoid per-frame heap allocation)
# ═══════════════════════════════════════════════════════════════════════════

_send_vf = NDI_video_frame_v2_t()
_recv_vf = NDI_video_frame_v2_t()
_recv_af = NDI_audio_frame_v2_t()
_recv_mf = NDI_metadata_frame_t()


# ═══════════════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════════════

def initialize():
    return _load_ndi().NDIlib_initialize()

def destroy():
    try: _ndi.NDIlib_destroy()
    except: pass

def version():
    return _load_ndi().NDIlib_version().decode()


# ─── Find ─────────────────────────────────────────────────────────────────

def find_create(show_local=True, extra_ips=None):
    s = NDI_find_create_t()
    s.show_local_sources = show_local
    s.p_groups = None
    s.p_extra_ips = extra_ips.encode() if isinstance(extra_ips, str) else extra_ips
    return _ndi.NDIlib_find_create_v2(ctypes.byref(s))

def find_wait_for_sources(finder, timeout_ms=1000):
    return _ndi.NDIlib_find_wait_for_sources(finder, timeout_ms)

def find_get_current_sources(finder):
    num = ctypes.c_uint32(0)
    ptr = _ndi.NDIlib_find_get_current_sources(finder, ctypes.byref(num))
    sources = []
    for i in range(num.value):
        s = ptr[i]
        name = s.p_ndi_name.decode() if s.p_ndi_name else ""
        url = s.p_url_address.decode() if s.p_url_address else ""
        sources.append({"name": name, "url": url, "_name_bytes": name.encode(), "_url_bytes": url.encode()})
    return sources

def find_destroy(finder):
    _ndi.NDIlib_find_destroy(finder)

def discover_sources(timeout_ms=5000, extra_ips=None):
    finder = find_create(extra_ips=extra_ips)
    if not finder: return []
    find_wait_for_sources(finder, timeout_ms)
    sources = find_get_current_sources(finder)
    find_destroy(finder)
    return sources


# ─── Send ─────────────────────────────────────────────────────────────────

def send_create(ndi_name, clock_video=True, clock_audio=False):
    s = NDI_send_create_t()
    s.p_ndi_name = ndi_name.encode()
    s.p_groups = None
    s.clock_video = clock_video
    s.clock_audio = clock_audio
    return _ndi.NDIlib_send_create(ctypes.byref(s))

def send_video_v2(sender, frame_data, fps_n=30000, fps_d=1000):
    """Send frame. frame_data must be (H,W,4) uint8 C-contiguous BGRX."""
    vf = _send_vf
    h, w = frame_data.shape[:2]
    vf.xres = w
    vf.yres = h
    vf.FourCC = FOURCC_VIDEO_TYPE_BGRX
    vf.frame_rate_N = fps_n
    vf.frame_rate_D = fps_d
    vf.picture_aspect_ratio = 0.0
    vf.frame_format_type = 1
    vf.timecode = -1
    vf.p_data = frame_data.ctypes.data
    vf.line_stride_in_bytes = w * 4
    vf.p_metadata = None
    vf.timestamp = -1
    _ndi.NDIlib_send_send_video_v2(sender, ctypes.byref(vf))

def send_get_no_connections(sender, timeout_ms=0):
    return _ndi.NDIlib_send_get_no_connections(sender, timeout_ms)

def send_destroy(sender):
    _ndi.NDIlib_send_destroy(sender)


# ─── Receive ──────────────────────────────────────────────────────────────

def recv_create(source_dict=None, color_format=RECV_COLOR_FORMAT_BGRX_BGRA,
                bandwidth=RECV_BANDWIDTH_HIGHEST, allow_video_fields=True, name=None):
    s = NDI_recv_create_v3_t()
    if source_dict:
        s.source_to_connect_to.p_ndi_name = source_dict.get("_name_bytes", b"")
        s.source_to_connect_to.p_url_address = source_dict.get("_url_bytes", b"")
    else:
        s.source_to_connect_to.p_ndi_name = None
        s.source_to_connect_to.p_url_address = None
    s.color_format = color_format
    s.bandwidth = bandwidth
    s.allow_video_fields = allow_video_fields
    s.p_ndi_recv_name = name.encode() if name else None
    return _ndi.NDIlib_recv_create_v3(ctypes.byref(s))

def recv_connect(receiver, source_dict):
    if source_dict:
        src = NDI_source_t()
        src.p_ndi_name = source_dict.get("_name_bytes", b"")
        src.p_url_address = source_dict.get("_url_bytes", b"")
        _ndi.NDIlib_recv_connect(receiver, ctypes.byref(src))

def recv_capture(receiver, timeout_ms=100):
    """Returns (frame_type, numpy_BGRX_or_None, width, height).
    Each call returns a NEW numpy array (safe to hold across calls)."""
    vf = _recv_vf
    af = _recv_af
    mf = _recv_mf

    ft = _ndi.NDIlib_recv_capture_v2(
        receiver, ctypes.byref(vf), ctypes.byref(af), ctypes.byref(mf), timeout_ms)

    if ft == FRAME_TYPE_VIDEO and vf.p_data:
        w, h = vf.xres, vf.yres
        stride = vf.line_stride_in_bytes if vf.line_stride_in_bytes > 0 else w * 4
        size = h * stride
        buf = (ctypes.c_uint8 * size).from_address(vf.p_data)
        if stride == w * 4:
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4)).copy()
        else:
            raw = np.frombuffer(buf, dtype=np.uint8).copy().reshape((h, stride))
            frame = raw[:, :w*4].reshape((h, w, 4))
        _ndi.NDIlib_recv_free_video_v2(receiver, ctypes.byref(vf))
        return FRAME_TYPE_VIDEO, frame, w, h

    elif ft == FRAME_TYPE_AUDIO:
        _ndi.NDIlib_recv_free_audio_v2(receiver, ctypes.byref(af))
        return FRAME_TYPE_AUDIO, None, 0, 0

    elif ft == FRAME_TYPE_METADATA:
        _ndi.NDIlib_recv_free_metadata(receiver, ctypes.byref(mf))
        return FRAME_TYPE_METADATA, None, 0, 0

    return FRAME_TYPE_NONE, None, 0, 0

def recv_destroy(receiver):
    _ndi.NDIlib_recv_destroy(receiver)


# ═══════════════════════════════════════════════════════════════════════════
# PTZ Control (requires active NDI receiver connection)
# ═══════════════════════════════════════════════════════════════════════════

def recv_ptz_is_supported(receiver):
    """Check if connected source supports PTZ."""
    return _ndi.NDIlib_recv_ptz_is_supported(receiver)

def recv_ptz_zoom(receiver, zoom):
    """Set zoom level. 0.0 = wide, 1.0 = tele."""
    return _ndi.NDIlib_recv_ptz_zoom(receiver, ctypes.c_float(zoom))

def recv_ptz_pan_tilt(receiver, pan, tilt):
    """Set absolute pan/tilt. Both -1.0 to 1.0."""
    return _ndi.NDIlib_recv_ptz_pan_tilt(receiver, ctypes.c_float(pan), ctypes.c_float(tilt))

def recv_ptz_pan_tilt_speed(receiver, pan_speed, tilt_speed):
    """Set pan/tilt speed. Both -1.0 to 1.0. 0.0 = stop."""
    return _ndi.NDIlib_recv_ptz_pan_tilt_speed(receiver, ctypes.c_float(pan_speed), ctypes.c_float(tilt_speed))

def recv_ptz_auto_focus(receiver):
    """Enable auto-focus."""
    return _ndi.NDIlib_recv_ptz_auto_focus(receiver)

def recv_ptz_focus(receiver, focus):
    """Set manual focus. 0.0 = near, 1.0 = far."""
    return _ndi.NDIlib_recv_ptz_focus(receiver, ctypes.c_float(focus))

def recv_ptz_focus_speed(receiver, speed):
    """Set focus speed. -1.0 to 1.0. 0.0 = stop."""
    return _ndi.NDIlib_recv_ptz_focus_speed(receiver, ctypes.c_float(speed))

def recv_ptz_store_preset(receiver, preset):
    """Store current position as preset (0-based index)."""
    return _ndi.NDIlib_recv_ptz_store_preset(receiver, int(preset))

def recv_ptz_recall_preset(receiver, preset, speed=1.0):
    """Recall preset (0-based index) at given speed (0.0 to 1.0)."""
    return _ndi.NDIlib_recv_ptz_recall_preset(receiver, int(preset), ctypes.c_float(speed))
