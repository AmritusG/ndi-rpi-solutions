#!/usr/bin/env python3
"""
ndi_hdmi.py - Output an NDI stream to HDMI display.
Dual-mode: fullscreen window (desktop) or framebuffer (headless).
Designed for Raspberry Pi 4/5.
Exit: Escape, Q, Ctrl+C, or SIGTERM.
"""

import argparse, ctypes, fcntl, glob, json, mmap, os, re, select, signal
import struct, subprocess, sys, threading, time
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


# --- Stats reporting (JSON to /tmp for web panel) ---

def stats_path(hdmi_index):
    return f"/tmp/ndi-hdmi-{hdmi_index}-stats.json"

def write_stats(hdmi_index, data):
    try:
        with open(stats_path(hdmi_index), "w") as f:
            json.dump(data, f)
    except:
        pass

def clear_stats(hdmi_index):
    try:
        os.remove(stats_path(hdmi_index))
    except:
        pass


# --- Desktop / display environment detection ---

def detect_display_env():
    """Find DISPLAY or WAYLAND_DISPLAY even if not in our env.
    Scans /proc/*/environ to find a running desktop session."""
    if os.environ.get("DISPLAY"):
        return {"DISPLAY": os.environ["DISPLAY"]}
    if os.environ.get("WAYLAND_DISPLAY"):
        return {
            "WAYLAND_DISPLAY": os.environ["WAYLAND_DISPLAY"],
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"),
        }
    for proc_dir in sorted(glob.glob("/proc/[0-9]*/environ")):
        try:
            with open(proc_dir, "rb") as f:
                env_data = f.read()
            env_pairs = {}
            for item in env_data.split(b"\x00"):
                if b"=" in item:
                    k, v = item.split(b"=", 1)
                    env_pairs[k.decode(errors="ignore")] = v.decode(errors="ignore")
            if "DISPLAY" in env_pairs:
                result = {"DISPLAY": env_pairs["DISPLAY"]}
                if "XAUTHORITY" in env_pairs:
                    result["XAUTHORITY"] = env_pairs["XAUTHORITY"]
                return result
            if "WAYLAND_DISPLAY" in env_pairs:
                return {
                    "WAYLAND_DISPLAY": env_pairs["WAYLAND_DISPLAY"],
                    "XDG_RUNTIME_DIR": env_pairs.get("XDG_RUNTIME_DIR", "/run/user/1000"),
                }
        except (PermissionError, FileNotFoundError, OSError):
            continue
    return None

def has_desktop():
    env = detect_display_env()
    if env:
        return True
    for proc_name in ["wayfire", "Xorg", "weston", "labwc", "sway"]:
        try:
            r = subprocess.run(["pgrep", "-x", proc_name], capture_output=True, timeout=2)
            if r.returncode == 0:
                return True
        except:
            pass
    return False

def setup_display_env():
    """Set DISPLAY/WAYLAND_DISPLAY in our env so OpenCV can open windows."""
    env = detect_display_env()
    if env:
        for k, v in env.items():
            os.environ.setdefault(k, v)
        print(f"[OK] Display env: {env}")
        return True
    print("[WARN] No display env found")
    return False


# --- Keyboard listener (headless / framebuffer mode) ---

EV_KEY = 0x01
KEY_ESC = 1
KEY_Q = 16
INPUT_EVENT_SIZE = 24
INPUT_EVENT_FMT = "llHHi"

def _find_keyboards():
    kbds = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            buf = bytearray(64)
            try:
                fcntl.ioctl(fd, 0x80404520, buf)
                if buf[0] & (1 << EV_KEY):
                    kbuf = bytearray(96)
                    fcntl.ioctl(fd, 0x80604521, kbuf)
                    if kbuf[KEY_ESC // 8] & (1 << (KEY_ESC % 8)):
                        kbds.append((path, fd))
                        continue
            except OSError:
                pass
            os.close(fd)
        except (OSError, PermissionError):
            pass
    return kbds

def _keyboard_listener():
    global running, exit_reason
    kbds = _find_keyboards()
    if not kbds:
        print("[i] No keyboard found (use Ctrl+C, web panel, or SSH)")
        return
    print(f"[i] Keyboard: {', '.join(p for p, _ in kbds)}")
    print("[i] Press Escape or Q to stop.\n")
    fds = {fd: path for path, fd in kbds}
    try:
        while running:
            readable, _, _ = select.select(list(fds.keys()), [], [], 0.5)
            for fd in readable:
                try:
                    data = os.read(fd, INPUT_EVENT_SIZE * 8)
                    for i in range(0, len(data) - INPUT_EVENT_SIZE + 1, INPUT_EVENT_SIZE):
                        _, _, ev_type, ev_code, ev_value = struct.unpack(
                            INPUT_EVENT_FMT, data[i:i + INPUT_EVENT_SIZE])
                        if ev_type == EV_KEY and ev_value == 1 and ev_code in (KEY_ESC, KEY_Q):
                            exit_reason = "Escape key" if ev_code == KEY_ESC else "Q key"
                            running = False
                            return
                except (OSError, BlockingIOError):
                    pass
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except:
                pass


# --- VT blanking (headless only) ---

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01
_saved_vt_fd = None

def vt_take_over():
    global _saved_vt_fd
    # Disable cursor blinking via sysfs
    try:
        with open("/sys/class/graphics/fbcon/cursor_blink", "w") as f:
            f.write("0")
    except:
        try:
            subprocess.run(["sudo", "sh", "-c",
                           "echo 0 > /sys/class/graphics/fbcon/cursor_blink"],
                           capture_output=True, timeout=3)
        except:
            pass

    for tty in ["/dev/tty0", "/dev/tty1", "/dev/console"]:
        try:
            fd = os.open(tty, os.O_RDWR)
            # Hide cursor via escape sequence
            os.write(fd, b"\033[?25l")   # hide cursor
            os.write(fd, b"\033[2J")     # clear screen
            os.write(fd, b"\033[H")      # home position
            # Switch to graphics mode (disables text rendering)
            fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)
            _saved_vt_fd = fd
            print(f"[OK] Console blanked, cursor hidden ({tty})")
            return True
        except (OSError, PermissionError):
            try:
                os.close(fd)
            except:
                pass
    return False

def vt_restore():
    global _saved_vt_fd
    if _saved_vt_fd is not None:
        try:
            fcntl.ioctl(_saved_vt_fd, KDSETMODE, KD_TEXT)
            os.write(_saved_vt_fd, b"\033[?25h")  # restore cursor
            os.close(_saved_vt_fd)
        except:
            pass
        _saved_vt_fd = None
    # Re-enable cursor blinking
    try:
        with open("/sys/class/graphics/fbcon/cursor_blink", "w") as f:
            f.write("1")
    except:
        pass


# --- Framebuffer (headless mode) ---

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FSCREENINFO_SIZE = 168

class Framebuffer:
    FBIOPUT_VSCREENINFO = 0x4601

    def __init__(self, device="/dev/fb0"):
        self.device = device
        self.fd = os.open(device, os.O_RDWR)
        self.fb_file = open(device, "r+b")

        # Try to set 32bpp for direct BGRA writes
        self._try_set_depth(32)

        # Read current framebuffer info
        self._read_info()
        print(f"[OK] Framebuffer {device}: {self.xres}x{self.yres} @ {self.bpp}bpp"
              f" (line_length={self.line_length})")

    def _read_info(self):
        buf = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, buf)
        vals = struct.unpack_from("7I", buf, 0)
        self.xres, self.yres = vals[0], vals[1]
        self.xres_virtual, self.yres_virtual = vals[2], vals[3]
        self.bpp = vals[6]
        self.bytes_per_pixel = self.bpp // 8
        fbuf = bytearray(FSCREENINFO_SIZE)
        fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fbuf)
        self.line_length = struct.unpack_from("I", fbuf, 48)[0]
        self.fb_size = self.line_length * self.yres_virtual
        # Re-create mmap if needed
        if hasattr(self, 'mm'):
            try: self.mm.close()
            except: pass
        self.mm = mmap.mmap(
            self.fb_file.fileno(), self.fb_size,
            mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def _try_set_depth(self, depth):
        """Try to change framebuffer bit depth via ioctl."""
        try:
            buf = bytearray(160)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, buf)
            # Set bits_per_pixel (offset 24 = 6th uint32)
            struct.pack_into("I", buf, 24, depth)
            fcntl.ioctl(self.fd, self.FBIOPUT_VSCREENINFO, buf)
            print(f"[OK] Set framebuffer depth to {depth}bpp")
            return True
        except Exception as e:
            print(f"[i] Could not set {depth}bpp: {e}")
            return False

    @staticmethod
    def _bgra_to_rgb565(bgra_frame):
        """Convert BGRA numpy array to RGB565 (little-endian uint16)."""
        b = bgra_frame[:, :, 0].astype(np.uint16)
        g = bgra_frame[:, :, 1].astype(np.uint16)
        r = bgra_frame[:, :, 2].astype(np.uint16)
        rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return rgb565

    def write_frame(self, bgra_frame):
        h, w = bgra_frame.shape[:2]
        # Premultiply alpha over black if stream has transparency
        # Sample 5 pixels — if all 255, skip (no transparency)
        needs_premul = (bgra_frame[0, 0, 3] < 255 or bgra_frame[h-1, w-1, 3] < 255 or
                        bgra_frame[h//2, w//2, 3] < 255 or bgra_frame[0, w-1, 3] < 255 or
                        bgra_frame[h-1, 0, 3] < 255)
        if needs_premul:
            import cv2 as _cv2
            if not hasattr(self, '_premul_buf') or self._premul_buf.shape[:2] != (h, w):
                self._premul_buf = np.empty((h, w, 4), dtype=np.uint8)
                self._premul_ch = [np.empty((h, w), dtype=np.uint8) for _ in range(4)]
                self._premul_ch[3][:] = 255
            b, g, r, a_ch = _cv2.split(bgra_frame)
            _cv2.multiply(b, a_ch, dst=self._premul_ch[0], scale=1.0/255)
            _cv2.multiply(g, a_ch, dst=self._premul_ch[1], scale=1.0/255)
            _cv2.multiply(r, a_ch, dst=self._premul_ch[2], scale=1.0/255)
            _cv2.merge(self._premul_ch, dst=self._premul_buf)
            bgra_frame = self._premul_buf
        if self.bpp == 32:
            # 32bpp: write BGRA directly
            row_bytes = w * 4
            if self.line_length == row_bytes:
                size = h * row_bytes
                self.mm[0:size] = bgra_frame.data
            else:
                for y in range(min(h, self.yres)):
                    off = y * self.line_length
                    self.mm[off:off + row_bytes] = bgra_frame[y].data
        elif self.bpp == 16:
            # 16bpp: convert BGRA → RGB565
            rgb565 = self._bgra_to_rgb565(bgra_frame)
            raw = rgb565.tobytes()
            row_bytes = w * 2
            if self.line_length == row_bytes:
                size = h * row_bytes
                self.mm[0:size] = raw
            else:
                for y in range(min(h, self.yres)):
                    off = y * self.line_length
                    row_start = y * row_bytes
                    self.mm[off:off + row_bytes] = raw[row_start:row_start + row_bytes]

    def clear(self):
        self.mm.seek(0)
        self.mm.write(b'\x00' * self.fb_size)

    def close(self):
        try:
            self.clear()
            self.mm.close()
            self.fb_file.close()
            os.close(self.fd)
        except:
            pass


def detect_hdmi_outputs():
    outputs = []
    for i in range(4):
        dev = f"/dev/fb{i}"
        if not os.path.exists(dev):
            continue
        try:
            fb = Framebuffer(dev)
            outputs.append({"index": i, "device": dev, "width": fb.xres,
                            "height": fb.yres, "bpp": fb.bpp,
                            "label": f"HDMI-{i} ({fb.xres}x{fb.yres})"})
            fb.close()
        except Exception as e:
            outputs.append({"index": i, "device": dev, "width": 0, "height": 0,
                            "label": f"HDMI-{i} (error)", "error": str(e)})
    return outputs


# --- Scale frame with aspect ratio ---

def scale_frame(cv2, frame, w, h, render_w, render_h):
    src_aspect = w / h
    rdr_aspect = render_w / render_h
    if abs(src_aspect - rdr_aspect) < 0.01:
        return cv2.resize(frame, (render_w, render_h), interpolation=cv2.INTER_LINEAR)
    rendered = np.zeros((render_h, render_w, frame.shape[2]), dtype=np.uint8)
    if src_aspect > rdr_aspect:
        nw = render_w
        nh = int(render_w / src_aspect)
        content = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        y_off = (render_h - nh) // 2
        rendered[y_off:y_off + nh, :] = content
    else:
        nh = render_h
        nw = int(render_h * src_aspect)
        content = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        x_off = (render_w - nw) // 2
        rendered[:, x_off:x_off + nw] = content
    return rendered


def _find_source(source_name, max_attempts=30):
    for attempt in range(max_attempts):
        if not running:
            break
        sources = ndi.discover_sources(timeout_ms=2000)
        for s in sources:
            if source_name.lower() in s["name"].lower():
                return s
        print(f"  Scan {attempt + 1}/{max_attempts}...")
    return None


# --- Desktop mode: SDL2 hardware-accelerated fullscreen ---

def run_desktop_output(source_name, hdmi_index, render_w, render_h):
    global running, exit_reason
    import ctypes as ct
    import atexit
    import cv2

    # If render_w/render_h specified, we CPU-scale frames before GPU upload
    do_scale = render_w > 0 and render_h > 0
    if do_scale:
        print(f"[i] Output resolution: {render_w}x{render_h} (CPU scaling enabled)")
    else:
        print(f"[i] Output resolution: stream native (no scaling)")

    def _cleanup():
        clear_stats(hdmi_index)

    atexit.register(_cleanup)

    if not ndi.initialize():
        print("[ERR] NDI init failed.")
        sys.exit(1)

    setup_display_env()

    # --- Load SDL2 ---
    try:
        sdl = ct.CDLL("libSDL2-2.0.so.0")
    except OSError:
        try:
            sdl = ct.CDLL("libSDL2.so")
        except OSError:
            print("[ERR] SDL2 not found. Install: sudo apt install -y libsdl2-2.0-0")
            sys.exit(1)

    # SDL constants
    SDL_INIT_VIDEO = 0x00000020
    SDL_WINDOW_FULLSCREEN_DESKTOP = 0x00001001
    SDL_WINDOW_SHOWN = 0x00000004
    SDL_RENDERER_ACCELERATED = 0x00000002
    SDL_RENDERER_PRESENTVSYNC = 0x00000004
    SDL_TEXTUREACCESS_STREAMING = 1
    SDL_PIXELFORMAT_ARGB8888 = 0x16362004  # matches BGRA byte order on little-endian
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

    if sdl.SDL_Init(SDL_INIT_VIDEO) < 0:
        print(f"[ERR] SDL_Init failed: {sdl.SDL_GetError()}")
        sys.exit(1)

    window = sdl.SDL_CreateWindow(
        b"NDI Output", 0, 0, 1920, 1080,
        SDL_WINDOW_FULLSCREEN_DESKTOP | SDL_WINDOW_SHOWN)
    if not window:
        print(f"[ERR] SDL window failed: {sdl.SDL_GetError()}")
        sys.exit(1)

    renderer = sdl.SDL_CreateRenderer(window, -1, SDL_RENDERER_ACCELERATED)
    if not renderer:
        # Fallback to software renderer
        renderer = sdl.SDL_CreateRenderer(window, -1, 0)
    if not renderer:
        print(f"[ERR] SDL renderer failed: {sdl.SDL_GetError()}")
        sys.exit(1)

    print("[OK] SDL2 fullscreen window (hardware-accelerated)")

    # Hide mouse cursor
    sdl.SDL_ShowCursor.restype = ct.c_int
    sdl.SDL_ShowCursor.argtypes = [ct.c_int]
    sdl.SDL_ShowCursor(0)

    # Find NDI source
    print(f"[i] Looking for NDI source: '{source_name}'...")
    write_stats(hdmi_index, {"state": "searching", "source": source_name})

    target = _find_source(source_name)
    if not target:
        print(f"[ERR] Source '{source_name}' not found.")
        write_stats(hdmi_index, {"state": "error", "error": "Source not found"})
        sdl.SDL_DestroyRenderer(renderer)
        sdl.SDL_DestroyWindow(window)
        sdl.SDL_Quit()
        sys.exit(1)

    print(f"[OK] Found: {target['name']}")
    receiver = ndi.recv_create(source_dict=target)
    if not receiver:
        print("[ERR] Failed to create NDI receiver.")
        sdl.SDL_DestroyRenderer(renderer)
        sdl.SDL_DestroyWindow(window)
        sdl.SDL_Quit()
        sys.exit(1)
    ndi.recv_connect(receiver, target)

    print(f"[OK] Receiving → SDL2 fullscreen (GPU texture upload)")
    print("[i] Press Escape or Q to stop.\n")

    # Get actual window/output size for aspect ratio calculation
    class SDL_Rect(ct.Structure):
        _fields_ = [("x", ct.c_int), ("y", ct.c_int), ("w", ct.c_int), ("h", ct.c_int)]

    sdl.SDL_GetRendererOutputSize.argtypes = [ct.c_void_p, ct.POINTER(ct.c_int), ct.POINTER(ct.c_int)]
    sdl.SDL_GetRendererOutputSize.restype = ct.c_int
    out_w, out_h = ct.c_int(0), ct.c_int(0)
    sdl.SDL_GetRendererOutputSize(renderer, ct.byref(out_w), ct.byref(out_h))
    screen_w, screen_h = out_w.value, out_h.value
    print(f"[i] Renderer output: {screen_w}x{screen_h}")

    texture = None
    tex_w, tex_h = 0, 0
    dst_rect = None
    frame_count = 0
    start = time.monotonic()
    last_stats = start
    last_stats_frames = 0
    event_buf = ct.create_string_buffer(64)  # SDL_Event is 56 bytes

    while running:
        # Poll SDL events
        while sdl.SDL_PollEvent(event_buf):
            event_type = struct.unpack_from("I", event_buf, 0)[0]
            if event_type == SDL_QUIT:
                exit_reason = "Window closed"
                running = False
            elif event_type == SDL_KEYDOWN:
                # SDL_KeyboardEvent: type(4) + timestamp(4) + windowID(4) + state(1) + repeat(1) + padding(2) + keysym
                # keysym starts at offset 16: scancode(4) + sym(4)
                sym = struct.unpack_from("i", event_buf, 20)[0]
                if sym == SDLK_ESCAPE:
                    exit_reason = "Escape key"
                    running = False
                elif sym == SDLK_q:
                    exit_reason = "Q key"
                    running = False

        if not running:
            break

        try:
            ft, frame, w, h = ndi.recv_capture(receiver, timeout_ms=16)
        except Exception:
            time.sleep(0.001)
            continue

        if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
            # Determine display frame size (scaled or native)
            if do_scale:
                disp_w, disp_h = render_w, render_h
            else:
                disp_w, disp_h = w, h

            # Recreate texture if display size changed
            if disp_w != tex_w or disp_h != tex_h:
                if texture:
                    sdl.SDL_DestroyTexture(texture)
                texture = sdl.SDL_CreateTexture(
                    renderer, SDL_PIXELFORMAT_ARGB8888,
                    SDL_TEXTUREACCESS_STREAMING, disp_w, disp_h)
                tex_w, tex_h = disp_w, disp_h

                # Calculate letterbox/pillarbox destination rect
                tex_aspect = disp_w / disp_h
                scr_aspect = screen_w / screen_h
                if abs(tex_aspect - scr_aspect) < 0.01:
                    dst_rect = None
                elif tex_aspect > scr_aspect:
                    dw = screen_w
                    dh = int(screen_w / tex_aspect)
                    dy = (screen_h - dh) // 2
                    dst_rect = SDL_Rect(0, dy, dw, dh)
                else:
                    dh = screen_h
                    dw = int(screen_h * tex_aspect)
                    dx = (screen_w - dw) // 2
                    dst_rect = SDL_Rect(dx, 0, dw, dh)

                scale_label = f" (scaled from {w}x{h})" if do_scale else ""
                if dst_rect:
                    print(f"[OK] Texture: {disp_w}x{disp_h}{scale_label} → display {dst_rect.w}x{dst_rect.h}+{dst_rect.x}+{dst_rect.y}")
                else:
                    print(f"[OK] Texture: {disp_w}x{disp_h}{scale_label} (fills screen)")

            # Scale frame if needed
            if do_scale and (w != render_w or h != render_h):
                display_frame = cv2.resize(frame, (render_w, render_h), interpolation=cv2.INTER_LINEAR)
                if not display_frame.flags["C_CONTIGUOUS"]:
                    display_frame = np.ascontiguousarray(display_frame)
            else:
                display_frame = frame if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame)

            # Upload to GPU texture
            sdl.SDL_UpdateTexture(texture, None, display_frame.ctypes.data, disp_w * 4)

            sdl.SDL_RenderClear(renderer)
            sdl.SDL_RenderCopy(renderer, texture, None,
                               ct.byref(dst_rect) if dst_rect else None)
            sdl.SDL_RenderPresent(renderer)
            frame_count += 1

            now = time.monotonic()
            if now - last_stats >= 1.0:
                stats_elapsed = now - last_stats
                stats_frames = frame_count - last_stats_frames
                fps = stats_frames / stats_elapsed if stats_elapsed > 0 else 0
                last_stats_frames = frame_count
                last_stats = now
                write_stats(hdmi_index, {
                    "state": "running", "fps": round(fps, 1),
                    "frames": frame_count,
                    "source_w": w, "source_h": h,
                    "render_w": disp_w, "render_h": disp_h,
                    "source": source_name,
                })

            if frame_count % 300 == 0:
                elapsed = time.monotonic() - start
                fps_avg = frame_count / elapsed if elapsed > 0 else 0
                scale_info = f" (scaled from {w}x{h})" if do_scale and (w != render_w or h != render_h) else ""
                print(f"  Frames: {frame_count} | FPS: {fps_avg:.1f} | {disp_w}x{disp_h}{scale_info}")

    reason = exit_reason or "stopped"
    print(f"\n[i] Shutting down ({reason})...")
    clear_stats(hdmi_index)
    try:
        if texture:
            sdl.SDL_DestroyTexture(texture)
        sdl.SDL_DestroyRenderer(renderer)
        sdl.SDL_DestroyWindow(window)
        sdl.SDL_Quit()
    except:
        pass
    try:
        ndi.recv_destroy(receiver)
        ndi.destroy()
    except:
        pass
    print("[OK] Output stopped.")
    sys.exit(0)  # explicit clean exit so systemd doesn't restart


# --- Headless mode: direct framebuffer ---

def run_framebuffer_output(source_name, hdmi_index, render_w, render_h, fb_device):
    global running, exit_reason
    import cv2

    if not ndi.initialize():
        print("[ERR] NDI init failed.")
        sys.exit(1)

    vt_take_over()

    # ── Try DRM 32bpp first (fast path — no RGB565 conversion) ──
    use_drm = False
    disp = None
    try:
        from drm_display import DRMDisplay
        pref_w = render_w if render_w > 0 else 1920
        pref_h = render_h if render_h > 0 else 1080
        disp = DRMDisplay(preferred_w=pref_w, preferred_h=pref_h)
        screen_w, screen_h = disp.width, disp.height
        use_drm = True
        print(f"[OK] Using DRM 32bpp — zero color conversion")
    except Exception as e:
        print(f"[i] DRM not available ({e}), falling back to fbdev")

    # ── Fallback: fbdev (may be 16bpp with RGB565 conversion) ──
    fb = None
    if not use_drm:
        dev = f"/dev/fb{fb_device}"
        if not os.path.exists(dev):
            for fallback in range(4):
                if os.path.exists(f"/dev/fb{fallback}"):
                    dev = f"/dev/fb{fallback}"
                    print(f"[i] /dev/fb{fb_device} not found, using {dev}")
                    break
            else:
                print("[ERR] No framebuffer found.")
                vt_restore()
                sys.exit(1)

        fb = Framebuffer(dev)
        screen_w, screen_h = fb.xres, fb.yres

    rw = render_w if render_w > 0 else screen_w
    rh = render_h if render_h > 0 else screen_h
    backend = "DRM 32bpp" if use_drm else f"fbdev {fb.bpp}bpp"

    print(f"[i] Looking for NDI source: '{source_name}'...")
    write_stats(hdmi_index, {"state": "searching", "source": source_name})
    target = _find_source(source_name)
    if not target:
        print(f"[ERR] Source '{source_name}' not found.")
        write_stats(hdmi_index, {"state": "error", "error": "Source not found"})
        if use_drm:
            disp.close()
        else:
            fb.close()
        vt_restore()
        sys.exit(1)

    print(f"[OK] Found: {target['name']}")
    receiver = ndi.recv_create(source_dict=target)
    if not receiver:
        if use_drm:
            disp.close()
        else:
            fb.close()
        vt_restore()
        sys.exit(1)
    ndi.recv_connect(receiver, target)

    res_info = f"{rw}x{rh}"
    if rw != screen_w or rh != screen_h:
        res_info += f" -> {screen_w}x{screen_h}"
    print(f"[OK] Receiving -> {screen_w}x{screen_h} ({backend})")
    print("[i] Press Escape or Q to stop.\n")

    kb_thread = threading.Thread(target=_keyboard_listener, daemon=True)
    kb_thread.start()

    frame_count = 0
    start = time.monotonic()
    last_stats = start
    last_stats_frames = 0

    while running:
        try:
            ft, frame, w, h = ndi.recv_capture(receiver, timeout_ms=50)
        except Exception:
            time.sleep(0.01)
            continue

        if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
            # Scale directly to screen size in one step
            if w == screen_w and h == screen_h:
                final = frame
            else:
                final = scale_frame(cv2, frame, w, h, screen_w, screen_h)
            # Ensure BGRA
            if final.shape[2] == 3:
                final = cv2.cvtColor(final, cv2.COLOR_BGR2BGRA)
            if not final.flags["C_CONTIGUOUS"]:
                final = np.ascontiguousarray(final)

            if use_drm:
                disp.write_frame(final)
            else:
                fb.write_frame(final)

            frame_count += 1

            now = time.monotonic()
            if now - last_stats >= 1.0:
                stats_elapsed = now - last_stats
                stats_frames = frame_count - last_stats_frames
                fps = stats_frames / stats_elapsed if stats_elapsed > 0 else 0
                last_stats_frames = frame_count
                last_stats = now
                write_stats(hdmi_index, {
                    "state": "running", "fps": round(fps, 1),
                    "frames": frame_count,
                    "source_w": w, "source_h": h,
                    "render_w": rw, "render_h": rh,
                    "source": source_name,
                })

            if frame_count % 300 == 0:
                elapsed = time.monotonic() - start
                fps_avg = frame_count / elapsed if elapsed > 0 else 0
                print(f"  Frames: {frame_count} | FPS: {fps_avg:.1f}")

    reason = exit_reason or "stopped"
    print(f"\n[i] Shutting down ({reason})...")
    clear_stats(hdmi_index)
    try:
        if use_drm:
            disp.clear()
            disp.close()
        else:
            fb.close()
        vt_restore()
    except:
        pass
    try:
        ndi.recv_destroy(receiver)
        ndi.destroy()
    except:
        pass
    print("[OK] Output stopped.")
    sys.exit(0)


# --- Entry point ---

def run_hdmi_output(source_name, hdmi_index=0, target_w=0, target_h=0, fb_device=0, force_mode=None):
    if force_mode == "framebuffer":
        desktop = False
    elif force_mode == "desktop":
        desktop = True
    else:
        desktop = has_desktop()
    mode = "desktop (SDL2 fullscreen)" if desktop else "headless (framebuffer)"
    print(f"[i] Display mode: {mode}" + (" (forced)" if force_mode else " (auto-detected)"))
    if desktop:
        run_desktop_output(source_name, hdmi_index, target_w, target_h)
    else:
        run_framebuffer_output(source_name, hdmi_index, target_w, target_h, fb_device)


def main():
    p = argparse.ArgumentParser(description="NDI to HDMI output")
    p.add_argument("--source", type=str, help="NDI source name")
    p.add_argument("--hdmi", type=int, default=0, help="Output slot ID")
    p.add_argument("--fb", type=int, default=0, help="Framebuffer device")
    p.add_argument("--mode", choices=["auto", "desktop", "framebuffer"], default="auto",
                   help="Display mode: auto, desktop (SDL2 window), or framebuffer")
    p.add_argument("--resolution", type=str, default="", help="WxH")
    p.add_argument("--width", type=int, default=0)
    p.add_argument("--height", type=int, default=0)
    p.add_argument("--list", action="store_true")
    p.add_argument("--list-hdmi", action="store_true")
    a = p.parse_args()

    if a.list_hdmi:
        for o in detect_hdmi_outputs():
            print(f"  HDMI-{o['index']}  {o['label']}")
        return
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
    if not a.source:
        p.error("--source is required")

    tw, th = a.width, a.height
    if a.resolution:
        m = re.match(r"(\d+)x(\d+)", a.resolution)
        if m:
            tw, th = int(m.group(1)), int(m.group(2))
        else:
            p.error(f"Invalid resolution: {a.resolution}")

    force = None if a.mode == "auto" else a.mode
    run_hdmi_output(a.source, a.hdmi, tw, th, a.fb, force_mode=force)


if __name__ == "__main__":
    main()
