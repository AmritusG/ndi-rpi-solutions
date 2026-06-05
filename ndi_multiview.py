#!/usr/bin/env python3
"""
ndi_multiview.py - Multi-tile NDI multiviewer for Raspberry Pi.
Receives multiple NDI sources and composites them into a grid on a single HDMI output.
Supports SDL2 (desktop) and framebuffer (CLI/headless) modes.
Exit: Escape, Q, Ctrl+C, or SIGTERM.
"""

import argparse, ctypes, json, os, re, signal, struct, sys, threading, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndi_ctypes as ndi

running = True
exit_reason = ""

def _sig(s, f):
    global running, exit_reason
    running = False
    exit_reason = "Ctrl+C" if s == signal.SIGINT else "SIGTERM"

signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


# ═══════════════════════════════════════════════════════════════════════════
# Stats (JSON to /tmp for web panel)
# ═══════════════════════════════════════════════════════════════════════════

STATS_PATH = "/tmp/ndi-multiview-stats.json"

def write_stats(data):
    try:
        with open(STATS_PATH, "w") as f:
            json.dump(data, f)
    except:
        pass

def clear_stats():
    try:
        os.remove(STATS_PATH)
    except:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Thread-safe NDI capture (each thread gets its own structs)
# ═══════════════════════════════════════════════════════════════════════════

def recv_capture_local(receiver, vf, af, mf, timeout_ms=50):
    """Thread-safe recv_capture using caller-owned frame structs.
    Returns (frame_type, numpy_BGRA_or_None, width, height)."""
    ft = ndi._ndi.NDIlib_recv_capture_v2(
        receiver, ctypes.byref(vf), ctypes.byref(af), ctypes.byref(mf), timeout_ms)

    if ft == ndi.FRAME_TYPE_VIDEO and vf.p_data:
        w, h = vf.xres, vf.yres
        stride = vf.line_stride_in_bytes if vf.line_stride_in_bytes > 0 else w * 4
        size = h * stride
        buf = (ctypes.c_uint8 * size).from_address(vf.p_data)
        if stride == w * 4:
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4)).copy()
        else:
            raw = np.frombuffer(buf, dtype=np.uint8).copy().reshape((h, stride))
            frame = raw[:, :w * 4].reshape((h, w, 4))
        ndi._ndi.NDIlib_recv_free_video_v2(receiver, ctypes.byref(vf))
        return ndi.FRAME_TYPE_VIDEO, frame, w, h

    elif ft == ndi.FRAME_TYPE_AUDIO:
        ndi._ndi.NDIlib_recv_free_audio_v2(receiver, ctypes.byref(af))
        return ndi.FRAME_TYPE_AUDIO, None, 0, 0

    elif ft == ndi.FRAME_TYPE_METADATA:
        ndi._ndi.NDIlib_recv_free_metadata(receiver, ctypes.byref(mf))
        return ndi.FRAME_TYPE_METADATA, None, 0, 0

    return ndi.FRAME_TYPE_NONE, None, 0, 0


# ═══════════════════════════════════════════════════════════════════════════
# Source finder (reuses ndi_ctypes discover)
# ═══════════════════════════════════════════════════════════════════════════

def find_source(source_name, max_attempts=20):
    """Find a specific NDI source by name (substring match)."""
    for attempt in range(max_attempts):
        if not running:
            return None
        sources = ndi.discover_sources(timeout_ms=2000)
        for s in sources:
            if source_name.lower() in s["name"].lower():
                return s
        print(f"  [{source_name}] Scan {attempt + 1}/{max_attempts}...")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Tile receiver thread
# ═══════════════════════════════════════════════════════════════════════════

class TileReceiver:
    """Receives frames from one NDI source or displays a static image."""

    def __init__(self, tile_index, source_name):
        self.tile_index = tile_index
        self.source_name = source_name
        self.lock = threading.Lock()
        self.latest_frame = None   # numpy BGRA
        self.frame_w = 0
        self.frame_h = 0
        self.frame_count = 0
        self.fps = 0.0
        self.connected = False
        self.error = None
        self._thread = None
        self._receiver = None
        self.is_image = source_name.startswith("image://")

    def start(self):
        if not self.source_name:
            return
        if self.is_image:
            self._load_image()
        else:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _load_image(self):
        """Load a static image file as a BGRA frame."""
        import cv2
        filename = self.source_name.replace("image://", "", 1)
        # Search in static/images/ relative to script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(script_dir, "static", "images", filename)
        if not os.path.exists(path):
            self.error = f"Image not found: {filename}"
            print(f"[Tile {self.tile_index}] {self.error}")
            return
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            self.error = f"Could not read image: {filename}"
            print(f"[Tile {self.tile_index}] {self.error}")
            return
        # Convert to BGRA
        if len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        with self.lock:
            self.latest_frame = img
            self.frame_w = img.shape[1]
            self.frame_h = img.shape[0]
            self.frame_count = 1
        self.connected = True
        self.fps = 0.0
        print(f"[Tile {self.tile_index}] Static image: {filename} ({self.frame_w}x{self.frame_h})")

    def stop(self):
        # Thread will exit when `running` goes False
        if self._receiver:
            try:
                ndi.recv_destroy(self._receiver)
            except:
                pass
            self._receiver = None

    def _run(self):
        global running
        # Thread-local NDI frame structs (avoids global struct collision)
        vf = ndi.NDI_video_frame_v2_t()
        af = ndi.NDI_audio_frame_v2_t()
        mf = ndi.NDI_metadata_frame_t()

        print(f"[Tile {self.tile_index}] Searching for '{self.source_name}'...")
        target = find_source(self.source_name)
        if not target:
            self.error = f"Source '{self.source_name}' not found"
            print(f"[Tile {self.tile_index}] {self.error}")
            return

        print(f"[Tile {self.tile_index}] Found: {target['name']}")
        self._receiver = ndi.recv_create(source_dict=target)
        if not self._receiver:
            self.error = "Failed to create receiver"
            print(f"[Tile {self.tile_index}] {self.error}")
            return
        ndi.recv_connect(self._receiver, target)
        self.connected = True

        count = 0
        start = time.monotonic()
        last_fps = start
        last_fps_count = 0

        while running:
            try:
                ft, frame, w, h = recv_capture_local(
                    self._receiver, vf, af, mf, timeout_ms=50)
            except Exception:
                time.sleep(0.001)
                continue

            if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
                with self.lock:
                    self.latest_frame = frame
                    self.frame_w = w
                    self.frame_h = h
                count += 1
                self.frame_count = count

                now = time.monotonic()
                if now - last_fps >= 1.0:
                    interval_frames = count - last_fps_count
                    interval_elapsed = now - last_fps
                    self.fps = interval_frames / interval_elapsed if interval_elapsed > 0 else 0
                    last_fps_count = count
                    last_fps = now

        # Cleanup
        if self._receiver:
            try:
                ndi.recv_destroy(self._receiver)
            except:
                pass
            self._receiver = None

    def get_frame(self):
        """Get the latest frame (thread-safe). Returns (frame, w, h) or (None, 0, 0)."""
        with self.lock:
            return self.latest_frame, self.frame_w, self.frame_h


# ═══════════════════════════════════════════════════════════════════════════
# Grid layout calculation
# ═══════════════════════════════════════════════════════════════════════════

def calc_tile_rects(cols, rows, screen_w, screen_h, padding=2):
    """Calculate tile rectangles for a uniform grid layout (backward compat).
    Returns list of (x, y, w, h) tuples, one per tile (row-major order)."""
    tile_w = screen_w // cols
    tile_h = screen_h // rows
    rects = []
    for row in range(rows):
        for col in range(cols):
            x = col * tile_w + padding
            y = row * tile_h + padding
            w = tile_w - 2 * padding
            h = tile_h - 2 * padding
            rects.append((x, y, w, h))
    return rects


def calc_flex_tile_rects(layout_rows, screen_w, screen_h, padding=2):
    """Calculate tile rects for a flexible layout.
    layout_rows: [{"tiles": [{"flex": N, ...}, ...]}, ...]
    Each row gets equal height. Within a row, tiles divide width by flex.
    Returns list of (x, y, w, h) tuples in row-major order."""
    num_rows = len(layout_rows)
    if num_rows == 0:
        return []
    row_h = screen_h // num_rows
    rects = []
    for ri, row in enumerate(layout_rows):
        tiles = row.get("tiles", [])
        if not tiles:
            continue
        total_flex = sum(t.get("flex", 1) for t in tiles)
        if total_flex <= 0:
            total_flex = len(tiles)
        y = ri * row_h
        x_cursor = 0
        for ti, tile in enumerate(tiles):
            flex = tile.get("flex", 1)
            tw = int(screen_w * flex / total_flex)
            # Last tile in row takes remaining width (avoids rounding gaps)
            if ti == len(tiles) - 1:
                tw = screen_w - x_cursor
            rects.append((x_cursor + padding, y + padding,
                          tw - 2 * padding, row_h - 2 * padding))
            x_cursor += tw
    return rects


def parse_layout_file(path):
    """Read a flex layout JSON file. Returns (layout_rows, source_names, tally_map, meta).
    layout_rows: [{"tiles": [{"source":..., "tally":..., "flex":...}, ...]}, ...]
    source_names: flat list of non-empty source names (row-major)
    tally_map: {tile_index: tally_string}
    meta: {labels, output_res, mode}
    """
    with open(path) as f:
        data = json.load(f)
    layout_rows = data.get("rows", [])
    source_names = []
    tally_map = {}
    idx = 0
    for row in layout_rows:
        for tile in row.get("tiles", []):
            src = tile.get("source", "")
            if src:
                source_names.append(src)
            tally = tile.get("tally", "none")
            if tally and tally != "none":
                tally_map[idx] = tally
            idx += 1
    meta = {
        "labels": data.get("labels", True),
        "output_res": data.get("output_res", ""),
        "mode": data.get("mode", "auto"),
    }
    return layout_rows, source_names, tally_map, meta


def scale_frame_to_tile(cv2, frame, src_w, src_h, tile_w, tile_h):
    """Scale frame to fit tile with letterbox/pillarbox, return (tile_w x tile_h) BGRA."""
    src_aspect = src_w / src_h
    tile_aspect = tile_w / tile_h

    if abs(src_aspect - tile_aspect) < 0.01:
        return cv2.resize(frame, (tile_w, tile_h), interpolation=cv2.INTER_LINEAR)

    result = np.zeros((tile_h, tile_w, 4), dtype=np.uint8)
    if src_aspect > tile_aspect:
        # Source wider → letterbox
        nw = tile_w
        nh = int(tile_w / src_aspect)
        scaled = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        y_off = (tile_h - nh) // 2
        result[y_off:y_off + nh, :nw] = scaled
    else:
        # Source taller → pillarbox
        nh = tile_h
        nw = int(tile_h * src_aspect)
        scaled = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        x_off = (tile_w - nw) // 2
        result[:nh, x_off:x_off + nw] = scaled
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Tally borders, overlays, and labels
# ═══════════════════════════════════════════════════════════════════════════

_overlay_cache = {}  # filename -> BGRA numpy array

def load_overlay_image(filename):
    """Load and cache a BGRA overlay image with alpha."""
    if filename in _overlay_cache:
        return _overlay_cache[filename]
    import cv2
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, "static", "images", filename)
    if not os.path.exists(path):
        print(f"[WARN] Overlay not found: {filename}")
        _overlay_cache[filename] = None
        return None
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        _overlay_cache[filename] = None
        return None
    if len(img.shape) == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    _overlay_cache[filename] = img
    return img


_overlay_rect_cache = {}  # tile_index -> list of (dst_y1, dst_y2, dst_x1, dst_x2, patch_rgb, patch_alpha_u16, patch_inv_alpha_u16)

def _build_overlay_rects(overlay_configs_for_tile, tile_w, tile_h, tile_index):
    """Pre-compute rectangular overlay patches for fast per-frame application.
    Instead of boolean masks over the full tile (28ms), uses slice operations
    on small rectangles (~1ms per overlay)."""
    import cv2
    rects = []

    for ocfg in overlay_configs_for_tile:
        if not ocfg or not ocfg.get("image"):
            continue
        img = load_overlay_image(ocfg["image"])
        if img is None:
            continue

        scale = ocfg.get("scale", 1.0)
        if scale != 1.0 and scale > 0:
            img = cv2.resize(img, (max(1, int(img.shape[1] * scale)),
                                    max(1, int(img.shape[0] * scale))),
                              interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

        ox = int(ocfg.get("x", 0))
        oy = int(ocfg.get("y", 0))
        oh, ow = img.shape[:2]

        src_x1 = max(0, -ox); src_y1 = max(0, -oy)
        dst_x1 = max(0, ox);  dst_y1 = max(0, oy)
        src_x2 = min(ow, tile_w - ox)
        src_y2 = min(oh, tile_h - oy)
        if src_x2 <= src_x1 or src_y2 <= src_y1:
            continue
        dst_x2 = dst_x1 + (src_x2 - src_x1)
        dst_y2 = dst_y1 + (src_y2 - src_y1)

        patch = img[src_y1:src_y2, src_x1:src_x2]
        patch_rgb = patch[:, :, :3].copy()
        patch_alpha = patch[:, :, 3]

        # Check if fully opaque (common for logos/graphics)
        is_fully_opaque = (patch_alpha.min() == 255)

        if is_fully_opaque:
            # Direct copy — no alpha math needed at all
            rects.append((dst_y1, dst_y2, dst_x1, dst_x2, patch_rgb, None, None, True))
            print(f"[overlay] Tile {tile_index}: rect {dst_x1},{dst_y1} {src_x2-src_x1}x{src_y2-src_y1} (opaque, direct copy)")
        else:
            # Pre-compute alpha blend arrays for this rectangle only
            alpha_u16 = patch_alpha.astype(np.uint16)[:, :, np.newaxis]
            inv_alpha_u16 = 255 - alpha_u16
            fg_premul = patch_rgb.astype(np.uint16) * alpha_u16
            rects.append((dst_y1, dst_y2, dst_x1, dst_x2, patch_rgb, fg_premul, inv_alpha_u16, False))
            print(f"[overlay] Tile {tile_index}: rect {dst_x1},{dst_y1} {src_x2-src_x1}x{src_y2-src_y1} (alpha blend)")

    _overlay_rect_cache[tile_index] = rects
    return rects


def apply_overlay_rects(canvas, tile_index):
    """Apply pre-computed rectangular overlay patches.
    Opaque: one numpy slice copy (~0.5ms).
    Alpha: integer blend on small rectangle (~2ms)."""
    rects = _overlay_rect_cache.get(tile_index)
    if not rects:
        return
    for (dy1, dy2, dx1, dx2, patch_rgb, fg_premul, inv_alpha, is_opaque) in rects:
        if is_opaque:
            canvas[dy1:dy2, dx1:dx2, :3] = patch_rgb
        else:
            bg = canvas[dy1:dy2, dx1:dx2, :3].astype(np.uint16)
            blended = (fg_premul + bg * inv_alpha) >> 8
            canvas[dy1:dy2, dx1:dx2, :3] = blended.astype(np.uint8)


def composite_overlay(canvas, overlay_cfg, tile_w, tile_h):
    """Legacy per-overlay composite (used by desktop compositor)."""
    if not overlay_cfg or not overlay_cfg.get("image"):
        return
    import cv2
    img = load_overlay_image(overlay_cfg["image"])
    if img is None:
        return

    scale = overlay_cfg.get("scale", 1.0)
    if scale != 1.0 and scale > 0:
        img = cv2.resize(img, (max(1, int(img.shape[1] * scale)),
                                max(1, int(img.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)

    ox = int(overlay_cfg.get("x", 0))
    oy = int(overlay_cfg.get("y", 0))
    oh, ow = img.shape[:2]
    src_x1 = max(0, -ox); src_y1 = max(0, -oy)
    dst_x1 = max(0, ox);  dst_y1 = max(0, oy)
    src_x2 = min(ow, tile_w - ox); src_y2 = min(oh, tile_h - oy)
    dst_x2 = dst_x1 + (src_x2 - src_x1)
    dst_y2 = dst_y1 + (src_y2 - src_y1)
    if src_x2 <= src_x1 or src_y2 <= src_y1:
        return
    ov = img[src_y1:src_y2, src_x1:src_x2]
    alpha = ov[:, :, 3:4].astype(np.uint16)
    inv_a = 255 - alpha
    bg = canvas[dst_y1:dst_y2, dst_x1:dst_x2, :3].astype(np.uint16)
    fg = ov[:, :, :3].astype(np.uint16)
    canvas[dst_y1:dst_y2, dst_x1:dst_x2, :3] = ((fg * alpha + bg * inv_a) >> 8).astype(np.uint8)
    canvas[dst_y1:dst_y2, dst_x1:dst_x2, 3] = 255
# ═══════════════════════════════════════════════════════════════════════════

TALLY_COLORS = {
    "none":    (0, 0, 0, 0),
    "program": (0, 0, 255, 255),    # Red  (BGRA)
    "preview": (0, 255, 0, 255),    # Green (BGRA)
}

def draw_tally_border(cv2, canvas, x, y, w, h, tally, thickness=4):
    """Draw a colored border around a tile on the canvas."""
    if tally == "none" or tally not in TALLY_COLORS:
        return
    color = TALLY_COLORS[tally][:3]  # BGR for cv2
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), color, thickness)


def draw_label(cv2, canvas, x, y, w, h, text, label_cfg=None):
    """Draw a label on a tile with full customization.
    label_cfg: {text, position, show_tally, color, bg_color, font_scale, tally}
    """
    if label_cfg is None:
        label_cfg = {}

    position = label_cfg.get("position", "bottom")
    if position == "none":
        return

    display_text = label_cfg.get("text", "") or text
    if not display_text:
        return

    font_scale = label_cfg.get("font_scale", 0.5)
    if font_scale <= 0:
        font_scale = 0.5
    thickness = max(1, int(font_scale + 0.5))

    # Parse colors (hex string to BGR tuple)
    def hex_to_bgr(hex_str, default):
        if not hex_str or len(hex_str) < 7:
            return default
        try:
            r = int(hex_str[1:3], 16)
            g = int(hex_str[3:5], 16)
            b = int(hex_str[5:7], 16)
            return (b, g, r)
        except:
            return default

    text_color = hex_to_bgr(label_cfg.get("color"), (255, 255, 255))
    bg_color = hex_to_bgr(label_cfg.get("bg_color"), None)

    # Tally-colored background override
    tally = label_cfg.get("tally", "none")
    show_tally = label_cfg.get("show_tally", False)
    if show_tally and tally == "program":
        bg_color = (0, 0, 180)  # dark red
    elif show_tally and tally == "preview":
        bg_color = (0, 140, 0)  # dark green

    font = cv2.FONT_HERSHEY_SIMPLEX
    text_size = cv2.getTextSize(display_text, font, font_scale, thickness)[0]
    bar_h = text_size[1] + int(12 * font_scale / 0.5)

    # Vertical position
    if position == "top":
        bar_y = y
    elif position == "center":
        bar_y = y + (h - bar_h) // 2
    else:
        bar_y = y + h - bar_h

    # Clamp to canvas
    bar_y = max(y, min(bar_y, y + h - bar_h))

    # Background
    if bg_color:
        cv2.rectangle(canvas, (x, bar_y), (x + w, bar_y + bar_h), bg_color, -1)
    else:
        # Semi-transparent dark overlay
        region = canvas[bar_y:bar_y + bar_h, x:x + w]
        if region.size > 0:
            canvas[bar_y:bar_y + bar_h, x:x + w] = (region * 0.4).astype(np.uint8)

    # Horizontal alignment
    h_align = label_cfg.get("h_align", "left")
    text_y = bar_y + bar_h - int(6 * font_scale / 0.5)
    if h_align == "center":
        text_x = x + (w - text_size[0]) // 2
    elif h_align == "right":
        text_x = x + w - text_size[0] - 8
    else:
        text_x = x + 8

    cv2.putText(canvas, display_text, (text_x, text_y), font, font_scale, text_color, thickness)


def extract_label_configs(layout_rows, tally_map):
    """Extract flat list of label configs from layout_rows, merging tally info."""
    configs = []
    idx = 0
    for row in layout_rows:
        for tile in row.get("tiles", []):
            label = tile.get("label", {})
            cfg = {
                "text": label.get("text", ""),
                "position": label.get("position", "bottom"),
                "h_align": label.get("h_align", "left"),
                "show_tally": label.get("show_tally", False),
                "color": label.get("color", "#ffffff"),
                "bg_color": label.get("bg_color", ""),
                "font_scale": label.get("font_scale", 0.5),
                "tally": tally_map.get(idx, tile.get("tally", "none")),
            }
            configs.append(cfg)
            idx += 1
    return configs


def extract_overlay_configs(layout_rows):
    """Extract flat list of overlay layer lists from layout_rows.
    Returns list of lists: [[{image,x,y,scale}, ...], ...]
    Handles both old single-overlay format and new multi-layer format.
    """
    configs = []
    for row in layout_rows:
        for tile in row.get("tiles", []):
            # New format: overlays array
            overlays = tile.get("overlays")
            if overlays and isinstance(overlays, list):
                configs.append(overlays)
            else:
                # Old format: single overlay dict
                ov = tile.get("overlay")
                if ov and isinstance(ov, dict) and ov.get("image"):
                    configs.append([ov])
                else:
                    configs.append([])
    return configs


# ═══════════════════════════════════════════════════════════════════════════
# Desktop mode: SDL2 compositor
# ═══════════════════════════════════════════════════════════════════════════

def run_desktop_multiview(tiles, layout_rows, layout_desc, tally_map, show_labels, output_w=0, output_h=0):
    """SDL2 hardware-accelerated multiviewer."""
    global running, exit_reason
    import ctypes as ct
    import cv2

    # Reuse display env detection from ndi_hdmi
    try:
        from ndi_hdmi import setup_display_env
        setup_display_env()
    except ImportError:
        pass

    # --- Load SDL2 ---
    try:
        sdl = ct.CDLL("libSDL2-2.0.so.0")
    except OSError:
        try:
            sdl = ct.CDLL("libSDL2.so")
        except OSError:
            print("[ERR] SDL2 not found.")
            sys.exit(1)

    # SDL constants
    SDL_INIT_VIDEO = 0x00000020
    SDL_WINDOW_FULLSCREEN_DESKTOP = 0x00001001
    SDL_WINDOW_SHOWN = 0x00000004
    SDL_RENDERER_ACCELERATED = 0x00000002
    SDL_TEXTUREACCESS_STREAMING = 1
    SDL_PIXELFORMAT_ARGB8888 = 0x16362004
    SDL_QUIT = 0x100
    SDL_KEYDOWN = 0x300
    SDLK_ESCAPE = 27
    SDLK_q = ord('q')

    # SDL functions
    sdl.SDL_Init.restype = ct.c_int
    sdl.SDL_CreateWindow.restype = ct.c_void_p
    sdl.SDL_CreateWindow.argtypes = [ct.c_char_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_uint32]
    sdl.SDL_CreateRenderer.restype = ct.c_void_p
    sdl.SDL_CreateRenderer.argtypes = [ct.c_void_p, ct.c_int, ct.c_uint32]
    sdl.SDL_CreateTexture.restype = ct.c_void_p
    sdl.SDL_CreateTexture.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_int, ct.c_int, ct.c_int]
    sdl.SDL_UpdateTexture.restype = ct.c_int
    sdl.SDL_UpdateTexture.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_int]
    sdl.SDL_RenderClear.argtypes = [ct.c_void_p]
    sdl.SDL_RenderCopy.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p]
    sdl.SDL_RenderPresent.argtypes = [ct.c_void_p]
    sdl.SDL_DestroyTexture.argtypes = [ct.c_void_p]
    sdl.SDL_DestroyRenderer.argtypes = [ct.c_void_p]
    sdl.SDL_DestroyWindow.argtypes = [ct.c_void_p]
    sdl.SDL_PollEvent.restype = ct.c_int
    sdl.SDL_PollEvent.argtypes = [ct.c_void_p]
    sdl.SDL_Quit.restype = None
    sdl.SDL_GetError.restype = ct.c_char_p
    sdl.SDL_ShowCursor.restype = ct.c_int
    sdl.SDL_ShowCursor.argtypes = [ct.c_int]
    sdl.SDL_GetRendererOutputSize.argtypes = [ct.c_void_p, ct.POINTER(ct.c_int), ct.POINTER(ct.c_int)]
    sdl.SDL_GetRendererOutputSize.restype = ct.c_int
    sdl.SDL_SetRenderDrawColor.argtypes = [ct.c_void_p, ct.c_uint8, ct.c_uint8, ct.c_uint8, ct.c_uint8]
    sdl.SDL_RenderFillRect.argtypes = [ct.c_void_p, ct.c_void_p]

    class SDL_Rect(ct.Structure):
        _fields_ = [("x", ct.c_int), ("y", ct.c_int), ("w", ct.c_int), ("h", ct.c_int)]

    if sdl.SDL_Init(SDL_INIT_VIDEO) < 0:
        print(f"[ERR] SDL_Init failed: {sdl.SDL_GetError()}")
        sys.exit(1)

    window = sdl.SDL_CreateWindow(
        b"NDI Multiview", 0, 0, 1920, 1080,
        SDL_WINDOW_FULLSCREEN_DESKTOP | SDL_WINDOW_SHOWN)
    if not window:
        print(f"[ERR] SDL window failed: {sdl.SDL_GetError()}")
        sys.exit(1)

    renderer = sdl.SDL_CreateRenderer(window, -1, SDL_RENDERER_ACCELERATED)
    if not renderer:
        renderer = sdl.SDL_CreateRenderer(window, -1, 0)
    if not renderer:
        print(f"[ERR] SDL renderer failed: {sdl.SDL_GetError()}")
        sys.exit(1)

    sdl.SDL_ShowCursor(0)

    out_w, out_h = ct.c_int(0), ct.c_int(0)
    sdl.SDL_GetRendererOutputSize(renderer, ct.byref(out_w), ct.byref(out_h))
    screen_w, screen_h = out_w.value, out_h.value

    # Set logical resolution — GPU upscales to physical screen for free
    if output_w > 0 and output_h > 0 and (output_w != screen_w or output_h != screen_h):
        sdl.SDL_RenderSetLogicalSize.argtypes = [ct.c_void_p, ct.c_int, ct.c_int]
        sdl.SDL_RenderSetLogicalSize.restype = ct.c_int
        sdl.SDL_RenderSetLogicalSize(renderer, output_w, output_h)
        print(f"[OK] SDL2 multiview: logical {output_w}x{output_h} → physical {screen_w}x{screen_h}")
        logical_w, logical_h = output_w, output_h
    else:
        logical_w, logical_h = screen_w, screen_h
        print(f"[OK] SDL2 multiview: {screen_w}x{screen_h}")

    print(f"     {layout_desc}, {len(tiles)} tiles")

    # Calculate tile layout based on logical resolution
    tile_rects = calc_flex_tile_rects(layout_rows, logical_w, logical_h, padding=2)
    label_configs = extract_label_configs(layout_rows, tally_map)
    overlay_configs = extract_overlay_configs(layout_rows)

    # Per-tile state: texture, current resolution
    tile_textures = [None] * len(tiles)
    tile_tex_sizes = [(0, 0)] * len(tiles)

    event_buf = ct.create_string_buffer(64)
    comp_frame_count = 0
    start = time.monotonic()
    last_stats = start
    last_stats_frames = 0
    target_interval = 1.0 / 30  # 30fps compositor tick

    while running:
        tick_start = time.monotonic()

        # Poll SDL events
        while sdl.SDL_PollEvent(event_buf):
            event_type = struct.unpack_from("I", event_buf, 0)[0]
            if event_type == SDL_QUIT:
                exit_reason = "Window closed"
                running = False
            elif event_type == SDL_KEYDOWN:
                sym = struct.unpack_from("i", event_buf, 20)[0]
                if sym == SDLK_ESCAPE or sym == SDLK_q:
                    exit_reason = "Key pressed"
                    running = False

        # Clear screen to black
        sdl.SDL_SetRenderDrawColor(renderer, 0, 0, 0, 255)
        sdl.SDL_RenderClear(renderer)

        tile_stats = []
        any_stream_frame = False
        for i, tile in enumerate(tiles):
            if i >= len(tile_rects):
                break

            tx, ty, tw, th = tile_rects[i]
            frame, fw, fh = tile.get_frame()

            if frame is not None:
                if not tile.is_image:
                    any_stream_frame = True

            if frame is not None:
                # Scale frame to tile size (CPU — needed for label/tally overlay)
                scaled = scale_frame_to_tile(cv2, frame, fw, fh, tw, th)

                # Overlay images
                olayers = overlay_configs[i] if i < len(overlay_configs) else []
                for ocfg in olayers:
                    if ocfg.get("image"):
                        composite_overlay(scaled, ocfg, tw, th)

                # Draw tally border
                tally = tally_map.get(i, "none")
                if tally != "none":
                    draw_tally_border(cv2, scaled, 0, 0, tw, th, tally, thickness=4)

                # Draw label
                if show_labels:
                    lcfg = label_configs[i] if i < len(label_configs) else {}
                    draw_label(cv2, scaled, 0, 0, tw, th, tile.source_name, lcfg)

                # Ensure contiguous for SDL
                if not scaled.flags["C_CONTIGUOUS"]:
                    scaled = np.ascontiguousarray(scaled)

                # Create/recreate texture if tile size changed
                if tile_tex_sizes[i] != (tw, th):
                    if tile_textures[i]:
                        sdl.SDL_DestroyTexture(tile_textures[i])
                    tile_textures[i] = sdl.SDL_CreateTexture(
                        renderer, SDL_PIXELFORMAT_ARGB8888,
                        SDL_TEXTUREACCESS_STREAMING, tw, th)
                    tile_tex_sizes[i] = (tw, th)

                # Upload to GPU
                sdl.SDL_UpdateTexture(tile_textures[i], None, scaled.ctypes.data, tw * 4)

                # Render at tile position
                dst = SDL_Rect(tx, ty, tw, th)
                sdl.SDL_RenderCopy(renderer, tile_textures[i], None, ct.byref(dst))

            tile_stats.append({
                "source": tile.source_name,
                "connected": tile.connected,
                "fps": round(tile.fps, 1),
                "frames": tile.frame_count,
                "res": f"{tile.frame_w}x{tile.frame_h}" if tile.frame_w else "—",
                "tally": tally_map.get(i, "none"),
                "error": tile.error,
            })

        sdl.SDL_RenderPresent(renderer)
        if any_stream_frame:
            comp_frame_count += 1

        # Stats
        now = time.monotonic()
        if now - last_stats >= 1.0:
            stats_elapsed = now - last_stats
            stats_frames = comp_frame_count - last_stats_frames
            comp_fps = stats_frames / stats_elapsed if stats_elapsed > 0 else 0
            last_stats_frames = comp_frame_count
            write_stats({
                "state": "running",
                "mode": "desktop",
                "layout": layout_desc,
                "comp_fps": round(comp_fps, 1),
                "comp_frames": comp_frame_count,
                "screen": f"{logical_w}x{logical_h}",
                "tiles": tile_stats,
            })
            last_stats = now

            if comp_frame_count % 300 < 30:
                print(f"  Compositor: {comp_fps:.1f} fps | {comp_frame_count} frames | "
                      + " | ".join(f"T{i}:{t.fps:.0f}fps" for i, t in enumerate(tiles)))

        # Pace to target frame rate
        elapsed_tick = time.monotonic() - tick_start
        sleep_time = target_interval - elapsed_tick
        if sleep_time > 0.001:
            time.sleep(sleep_time)

    # Cleanup
    print(f"\n[i] Shutting down ({exit_reason or 'stopped'})...")
    for tex in tile_textures:
        if tex:
            sdl.SDL_DestroyTexture(tex)
    sdl.SDL_DestroyRenderer(renderer)
    sdl.SDL_DestroyWindow(window)
    sdl.SDL_Quit()


# ═══════════════════════════════════════════════════════════════════════════
# Framebuffer mode: CPU compositor
# ═══════════════════════════════════════════════════════════════════════════

def run_framebuffer_multiview(tiles, layout_rows, layout_desc, tally_map, show_labels, fb_device=0, output_w=0, output_h=0):
    """Headless multiviewer — tries DRM 32bpp first, falls back to fbdev 16bpp."""
    global running, exit_reason
    import cv2

    from ndi_hdmi import vt_take_over, vt_restore
    vt_take_over()

    # ── Try DRM 32bpp first (fast path — no RGB565 conversion) ──
    use_drm = False
    disp = None
    try:
        from drm_display import DRMDisplay
        pref_w = output_w if output_w > 0 else 1920
        pref_h = output_h if output_h > 0 else 1080
        disp = DRMDisplay(preferred_w=pref_w, preferred_h=pref_h)
        screen_w, screen_h = disp.width, disp.height
        use_drm = True
        print(f"[OK] Using DRM 32bpp — zero color conversion")
    except Exception as e:
        print(f"[i] DRM not available ({e}), falling back to fbdev")

    # ── Fallback: fbdev (may be 16bpp with RGB565 conversion) ──
    fb = None
    if not use_drm:
        from ndi_hdmi import Framebuffer
        dev = f"/dev/fb{fb_device}"
        if not os.path.exists(dev):
            for fallback in range(4):
                if os.path.exists(f"/dev/fb{fallback}"):
                    dev = f"/dev/fb{fallback}"
                    break
            else:
                print("[ERR] No framebuffer found.")
                vt_restore()
                sys.exit(1)
        fb = Framebuffer(dev)
        screen_w, screen_h = fb.xres, fb.yres

    # ── Tile layout ──
    comp_w = output_w if output_w > 0 else screen_w
    comp_h = output_h if output_h > 0 else screen_h
    do_final_scale = (comp_w != screen_w or comp_h != screen_h)
    backend = "DRM 32bpp" if use_drm else f"fbdev {fb.bpp}bpp"

    if do_final_scale:
        print(f"[OK] Multiview: compose {comp_w}x{comp_h} -> display {screen_w}x{screen_h} ({backend})")
    else:
        print(f"[OK] Multiview: {screen_w}x{screen_h} ({backend}, direct tile writes)")
    print(f"     {layout_desc}, {len(tiles)} tiles")

    tile_rects = calc_flex_tile_rects(layout_rows, comp_w, comp_h, padding=2)
    label_configs = extract_label_configs(layout_rows, tally_map)
    overlay_configs = extract_overlay_configs(layout_rows)
    tile_buffers = [np.zeros((th, tw, 4), dtype=np.uint8) for (tx, ty, tw, th) in tile_rects]
    prev_frame_counts = [0] * len(tiles)

    # Pre-compute rectangular overlay patches (computed once, ~1ms per overlay per frame)
    for i, (tx, ty, tw, th) in enumerate(tile_rects):
        olayers = overlay_configs[i] if i < len(overlay_configs) else []
        if olayers and any(o.get("image") for o in olayers):
            _build_overlay_rects(olayers, tw, th, i)

    # Full-frame composition buffer (in regular RAM — fast writes).
    # Tiles are composited here first, then the whole frame is bulk-written
    # to DRM mmap in one memcpy (~4ms) instead of per-tile strided writes (~40ms).
    comp_buf = np.zeros((screen_h, screen_w, 4), dtype=np.uint8)

    # Clear display
    if use_drm:
        disp.clear()
    else:
        fb.clear()

    # ── Tile writer (abstracts DRM vs fbdev) ──
    if use_drm:
        def write_tile(bgra_tile, fx, fy, fw, fh):
            # Write to RAM composition buffer (fast)
            comp_buf[fy:fy + fh, fx:fx + fw] = bgra_tile
        def flush_frame():
            # Bulk write full frame to DRM mmap (one memcpy)
            disp.write_frame(comp_buf)
    else:
        bpp = fb.bpp
        ll = fb.line_length
        def write_tile(bgra_tile, fx, fy, fw, fh):
            # Write to RAM composition buffer (fast)
            comp_buf[fy:fy + fh, fx:fx + fw] = bgra_tile
        _fb_premul = [None]
        _fb_ch = [None]
        def flush_frame():
            out = comp_buf
            ch, cw = comp_buf.shape[:2]
            needs_premul = (comp_buf[0, 0, 3] < 255 or comp_buf[ch-1, cw-1, 3] < 255 or
                            comp_buf[ch//2, cw//2, 3] < 255 or comp_buf[0, cw-1, 3] < 255 or
                            comp_buf[ch-1, 0, 3] < 255)
            if needs_premul:
                if _fb_premul[0] is None:
                    _fb_premul[0] = np.empty_like(comp_buf)
                    _fb_ch[0] = [np.empty(comp_buf.shape[:2], dtype=np.uint8) for _ in range(4)]
                    _fb_ch[0][3][:] = 255
                b, g, r, a_ch = cv2.split(comp_buf)
                cv2.multiply(b, a_ch, dst=_fb_ch[0][0], scale=1.0/255)
                cv2.multiply(g, a_ch, dst=_fb_ch[0][1], scale=1.0/255)
                cv2.multiply(r, a_ch, dst=_fb_ch[0][2], scale=1.0/255)
                cv2.merge(_fb_ch[0], dst=_fb_premul[0])
                out = _fb_premul[0]
            # Write full frame to fbdev
            if bpp == 32:
                row_bytes = screen_w * 4
                for y in range(screen_h):
                    off = y * ll
                    fb.mm[off:off + row_bytes] = out[y].data
            elif bpp == 16:
                b = out[:, :, 0].astype(np.uint16)
                g = out[:, :, 1].astype(np.uint16)
                r = out[:, :, 2].astype(np.uint16)
                rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                raw = rgb565.tobytes()
                row_bytes = screen_w * 2
                for y in range(screen_h):
                    off = y * ll
                    row_start = y * row_bytes
                    fb.mm[off:off + row_bytes] = raw[row_start:row_start + row_bytes]

    # ── Compositor loop ──
    comp_frame_count = 0
    start = time.monotonic()
    last_stats = start
    last_stats_frames = 0
    tile_stats = []

    while running:
        any_new = False
        any_stream_new = False
        for i, tile in enumerate(tiles):
            if i >= len(tile_rects):
                break
            if tile.frame_count != prev_frame_counts[i]:
                any_new = True
                if not tile.is_image:
                    any_stream_new = True
                tx, ty, tw, th = tile_rects[i]
                frame, fw, fh = tile.get_frame()

                if frame is not None:
                    prev_frame_counts[i] = tile.frame_count

                    # Scale to tile size
                    src_aspect = fw / fh if fh > 0 else 1.78
                    tile_aspect = tw / th if th > 0 else 1.78

                    if abs(src_aspect - tile_aspect) < 0.01:
                        cv2.resize(frame, (tw, th), dst=tile_buffers[i], interpolation=cv2.INTER_NEAREST)
                    else:
                        tile_buffers[i][:] = 0
                        if src_aspect > tile_aspect:
                            nw, nh = tw, int(tw / src_aspect)
                            scaled = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_NEAREST)
                            y_off = (th - nh) // 2
                            tile_buffers[i][y_off:y_off + nh, :nw] = scaled
                        else:
                            nh, nw = th, int(th * src_aspect)
                            scaled = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_NEAREST)
                            x_off = (tw - nw) // 2
                            tile_buffers[i][:nh, x_off:x_off + nw] = scaled

                    # Overlay images (pre-computed rects, ~1ms per overlay)
                    apply_overlay_rects(tile_buffers[i], i)

                    # Tally border
                    tally = tally_map.get(i, "none")
                    if tally != "none":
                        draw_tally_border(cv2, tile_buffers[i], 0, 0, tw, th, tally, thickness=4)

                    # Label
                    if show_labels:
                        lcfg = label_configs[i] if i < len(label_configs) else {}
                        draw_label(cv2, tile_buffers[i], 0, 0, tw, th, tile.source_name, lcfg)

                    buf = tile_buffers[i]
                    if not buf.flags["C_CONTIGUOUS"]:
                        buf = np.ascontiguousarray(buf)

                    if do_final_scale:
                        sx = int(tx * screen_w / comp_w)
                        sy = int(ty * screen_h / comp_h)
                        sw = int(tw * screen_w / comp_w)
                        sh = int(th * screen_h / comp_h)
                        buf = cv2.resize(buf, (sw, sh), interpolation=cv2.INTER_NEAREST)
                        write_tile(buf, sx, sy, sw, sh)
                    else:
                        write_tile(buf, tx, ty, tw, th)

        if not any_new:
            time.sleep(0.005)
            continue

        # Flush composed frame to display (one bulk memcpy)
        flush_frame()

        # Only count frames from streaming tiles for comp FPS
        if any_stream_new:
            comp_frame_count += 1

        now = time.monotonic()
        if now - last_stats >= 1.0:
            stats_elapsed = now - last_stats
            stats_frames = comp_frame_count - last_stats_frames
            comp_fps = stats_frames / stats_elapsed if stats_elapsed > 0 else 0
            last_stats_frames = comp_frame_count
            tile_stats = []
            for i, tile in enumerate(tiles):
                tile_stats.append({
                    "source": tile.source_name,
                    "connected": tile.connected,
                    "fps": round(tile.fps, 1),
                    "frames": tile.frame_count,
                    "res": f"{tile.frame_w}x{tile.frame_h}" if tile.frame_w else "\u2014",
                    "tally": tally_map.get(i, "none"),
                    "error": tile.error,
                })
            write_stats({
                "state": "running",
                "mode": "drm" if use_drm else "framebuffer",
                "layout": layout_desc,
                "comp_fps": round(comp_fps, 1),
                "comp_frames": comp_frame_count,
                "screen": f"{screen_w}x{screen_h}",
                "backend": backend,
                "tiles": tile_stats,
            })
            last_stats = now

            if comp_frame_count % 300 < 30:
                print(f"  Compositor: {comp_fps:.1f} fps | {comp_frame_count} frames | "
                      + " | ".join(f"T{i}:{t.fps:.0f}fps" for i, t in enumerate(tiles)))

    # Cleanup
    print(f"\n[i] Shutting down ({exit_reason or 'stopped'})...")
    if use_drm and disp:
        disp.close()
    if fb:
        fb.close()
    vt_restore()


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def run_multiview(source_names=None, layout="2x2", mode="auto", tally_map=None,
                  show_labels=True, fb_device=0, output_w=0, output_h=0,
                  layout_data=None):
    """Start the multiviewer.
    source_names: list of NDI source name strings (for simple layout)
    layout: "COLSxROWS" e.g. "2x2" (simple mode)
    layout_data: {"rows": [{"tiles": [{"source":..., "tally":..., "flex":...}]}]} (flex mode)
    If layout_data is provided, source_names/layout/tally_map are extracted from it.
    """
    global running

    # Build layout_rows and extract sources/tally from either format
    if layout_data and "rows" in layout_data:
        # Flex layout mode
        layout_rows = layout_data["rows"]
        source_names = []
        tally_map = tally_map or {}
        idx = 0
        for row in layout_rows:
            for tile in row.get("tiles", []):
                src = tile.get("source", "")
                if src:
                    source_names.append(src)
                else:
                    source_names.append("")
                tally = tile.get("tally", "none")
                if tally and tally != "none":
                    tally_map[idx] = tally
                idx += 1
        # Filter empty sources but keep index mapping
        num_tiles = sum(len(r.get("tiles", [])) for r in layout_rows)
        row_counts = [len(r.get("tiles", [])) for r in layout_rows]
        layout_desc = " + ".join(str(c) for c in row_counts) + f" ({num_tiles} tiles)"
    else:
        # Simple COLSxROWS layout — convert to flex layout_rows
        m = re.match(r"(\d+)x(\d+)", layout)
        if not m:
            print(f"[ERR] Invalid layout: {layout}. Use COLSxROWS e.g. 2x2")
            sys.exit(1)
        cols, rows = int(m.group(1)), int(m.group(2))
        max_tiles = cols * rows
        source_names = source_names or []
        if len(source_names) > max_tiles:
            source_names = source_names[:max_tiles]

        layout_rows = []
        idx = 0
        for r in range(rows):
            row_tiles = []
            for c in range(cols):
                src = source_names[idx] if idx < len(source_names) else ""
                tally = (tally_map or {}).get(idx, "none")
                row_tiles.append({"source": src, "tally": tally, "flex": 1})
                idx += 1
            layout_rows.append({"tiles": row_tiles})
        layout_desc = f"{cols}x{rows} grid"

    if tally_map is None:
        tally_map = {}

    # Filter to only non-empty sources for receivers
    active_sources = [s for s in source_names if s]

    print(f"\n  NDI Multiviewer — {layout_desc}")
    print(f"  Sources: {len(active_sources)}")
    print(f"  Output resolution: {output_w}x{output_h}" if output_w > 0 else "  Output resolution: screen native")
    for i, sn in enumerate(source_names):
        tally = tally_map.get(i, "none")
        tally_str = f" [{tally.upper()}]" if tally != "none" else ""
        print(f"    Tile {i}: {sn}{tally_str}")
    print()

    # Initialize NDI
    if not ndi.initialize():
        print("[ERR] NDI init failed.")
        sys.exit(1)

    # Start tile receivers
    tiles = []
    # Start receivers — one per non-empty source, mapped by tile index
    tile_source_map = {}  # tile_index -> receiver_index
    all_tile_sources = []
    idx = 0
    for row in layout_rows:
        for tile_def in row.get("tiles", []):
            all_tile_sources.append(tile_def.get("source", ""))
            idx += 1

    for i, sn in enumerate(all_tile_sources):
        if sn:
            tile = TileReceiver(i, sn)
            tile.start()
            tiles.append(tile)
            tile_source_map[i] = len(tiles) - 1
        else:
            # Empty tile — create a placeholder
            tile = TileReceiver(i, "")
            tiles.append(tile)

    # Write initial stats
    write_stats({
        "state": "starting",
        "layout": layout_desc,
        "tiles": [{"source": t.source_name, "connected": False} for t in tiles],
    })

    # Wait briefly for at least one tile to connect
    print("[i] Waiting for sources to connect...")
    deadline = time.monotonic() + 15
    while running and time.monotonic() < deadline:
        if any(t.connected for t in tiles):
            break
        time.sleep(0.5)

    connected = sum(1 for t in tiles if t.connected)
    print(f"[OK] {connected}/{len(tiles)} sources connected. Starting compositor.\n")

    # Detect display mode
    if mode == "framebuffer":
        desktop = False
    elif mode == "desktop":
        desktop = True
    else:
        try:
            from ndi_hdmi import has_desktop
            desktop = has_desktop()
        except ImportError:
            desktop = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")

    display_mode = "desktop (SDL2)" if desktop else "framebuffer"
    print(f"[i] Display mode: {display_mode}")

    try:
        if desktop:
            run_desktop_multiview(tiles, layout_rows, layout_desc, tally_map, show_labels, output_w, output_h)
        else:
            run_framebuffer_multiview(tiles, layout_rows, layout_desc, tally_map, show_labels, fb_device, output_w, output_h)
    finally:
        running = False
        # Stop all tile receivers
        for tile in tiles:
            tile.stop()
        clear_stats()
        ndi.destroy()
        print("[OK] Multiviewer stopped.")


def main():
    p = argparse.ArgumentParser(description="NDI Multiviewer")
    p.add_argument("--sources", type=str, default="",
                   help="Comma-separated NDI source names (simple mode)")
    p.add_argument("--layout", type=str, default="2x2",
                   help="Grid layout: COLSxROWS (e.g. 2x2, 3x2, 3x3) — simple mode")
    p.add_argument("--layout-file", type=str, default="",
                   help="Path to flex layout JSON file (overrides --sources/--layout/--tally)")
    p.add_argument("--mode", choices=["auto", "desktop", "framebuffer"], default="auto",
                   help="Display mode")
    p.add_argument("--fb", type=int, default=0, help="Framebuffer device number")
    p.add_argument("--no-labels", action="store_true", help="Hide source name labels")
    p.add_argument("--tally", type=str, default="",
                   help="Tally config: 0=program,2=preview (simple mode)")
    p.add_argument("--list", action="store_true", help="List NDI sources and exit")
    p.add_argument("--output-res", type=str, default="",
                   help="Output resolution WxH (e.g. 1920x1080). Default: use screen native.")
    a = p.parse_args()

    if a.list:
        if not ndi.initialize():
            print("[ERR] NDI init failed.")
            sys.exit(1)
        sources = ndi.discover_sources(timeout_ms=5000)
        if not sources:
            print("No NDI sources found.")
        else:
            for s in sources:
                print(f"  - {s['name']}")
        ndi.destroy()
        return

    # Parse output resolution
    out_w, out_h = 0, 0
    if a.output_res:
        m = re.match(r"(\d+)x(\d+)", a.output_res)
        if m:
            out_w, out_h = int(m.group(1)), int(m.group(2))
            print(f"[i] Output resolution: {out_w}x{out_h}")
        else:
            p.error(f"Invalid output resolution: {a.output_res}")
    else:
        print("[i] Output resolution: screen native (no --output-res)")

    if a.layout_file:
        # Flex layout mode — read from JSON file
        layout_rows, source_names, tally_map, meta = parse_layout_file(a.layout_file)
        show_labels = meta.get("labels", not a.no_labels)
        mode = a.mode if a.mode != "auto" else meta.get("mode", "auto")
        if not a.output_res and meta.get("output_res"):
            m2 = re.match(r"(\d+)x(\d+)", meta["output_res"])
            if m2:
                out_w, out_h = int(m2.group(1)), int(m2.group(2))
        run_multiview(mode=mode, show_labels=show_labels, fb_device=a.fb,
                      output_w=out_w, output_h=out_h,
                      layout_data={"rows": layout_rows})
    else:
        # Simple mode — sources + COLSxROWS layout
        source_names = [s.strip() for s in a.sources.split(",") if s.strip()]
        if not source_names:
            p.error("Either --sources or --layout-file is required")

        tally_map = {}
        if a.tally:
            for part in a.tally.split(","):
                if "=" in part:
                    idx, val = part.split("=", 1)
                    tally_map[int(idx.strip())] = val.strip()

        run_multiview(source_names=source_names, layout=a.layout, mode=a.mode,
                      tally_map=tally_map, show_labels=not a.no_labels, fb_device=a.fb,
                      output_w=out_w, output_h=out_h)


if __name__ == "__main__":
    main()
