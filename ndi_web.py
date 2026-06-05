#!/usr/bin/env python3
"""
NDI Web Control Panel — Flask backend
Features: Send, Receive Preview, dual HDMI output with stats, autostart.
"""

import argparse, glob, json, os, re, signal, socket, subprocess, sys, threading, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import cv2
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndi_ctypes as ndi

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["TEMPLATES_AUTO_RELOAD"] = True
HOSTNAME = socket.gethostname()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

PREVIEW_FPS = 15
PREVIEW_MAX_WIDTH = 640
SERVICE_PREFIX = "ndi-hdmi"

# --- Persistent NDI source finder (runs in background) ---
_known_sources = []  # list of {"name": ..., "url": ...}
_sources_lock = threading.Lock()

def _get_ndi_extra_ips():
    """Get extra IPs for NDI HX discovery: broadcast + any configured IPs."""
    ips = []
    # Subnet broadcast
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split('.')
        parts[3] = '255'
        ips.append('.'.join(parts))
    except:
        pass
    # Config file extra IPs (for HX cameras that don't respond to broadcast)
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(cfg_path) as f:
            cfg = json.load(f)
        extra = cfg.get("ndi_extra_ips", "")
        if extra:
            ips.extend([x.strip() for x in extra.split(',') if x.strip()])
    except:
        pass
    return ','.join(ips) if ips else None

_ndi_extra_ips = _get_ndi_extra_ips()

def _source_finder_loop():
    """Background thread: persistent NDI finder that polls for sources."""
    global _known_sources, _ndi_extra_ips
    finder = None
    last_extra_ips = None
    while True:
        try:
            # Recreate finder only if extra_ips changed
            current_extra = _ndi_extra_ips
            if finder is None or current_extra != last_extra_ips:
                if finder:
                    ndi.find_destroy(finder)
                finder = ndi.find_create(show_local=True, extra_ips=current_extra)
                last_extra_ips = current_extra

            if finder:
                # Wait for source changes (blocks up to 3s)
                ndi.find_wait_for_sources(finder, 3000)
                found = ndi.find_get_current_sources(finder)
                seen = set()
                sources = []
                for s in found:
                    if s["name"] not in seen:
                        seen.add(s["name"])
                        sources.append({"name": s["name"], "url": s.get("url", "")})
                with _sources_lock:
                    _known_sources = sources
        except:
            # If finder broke, recreate next loop
            if finder:
                try: ndi.find_destroy(finder)
                except: pass
            finder = None
            time.sleep(3)

# Standard output resolutions — always available regardless of EDID detection
STANDARD_RESOLUTIONS = [
    {"width": 3840, "height": 2160, "label": "3840x2160 (4K)"},
    {"width": 2560, "height": 1440, "label": "2560x1440 (1440p)"},
    {"width": 1920, "height": 1200, "label": "1920x1200 (WUXGA)"},
    {"width": 1920, "height": 1080, "label": "1920x1080 (1080p)"},
    {"width": 1680, "height": 1050, "label": "1680x1050 (WSXGA+)"},
    {"width": 1440, "height":  900, "label": "1440x900 (WXGA+)"},
    {"width": 1280, "height": 1024, "label": "1280x1024 (SXGA)"},
    {"width": 1280, "height":  720, "label": "1280x720 (720p)"},
    {"width": 1024, "height":  768, "label": "1024x768 (XGA)"},
    {"width":  720, "height":  576, "label": "720x576 (PAL)"},
    {"width":  720, "height":  480, "label": "720x480 (NTSC)"},
]


# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════

def load_config():
    try:
        with open(CONFIG_FILE) as f: return json.load(f)
    except: return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f: json.dump(cfg, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Screen control (blank / restore CLI)
# ═══════════════════════════════════════════════════════════════════════════

import fcntl

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01

def _screen_blank():
    """Force screen to black — keep VT in graphics mode, clear framebuffer."""
    # 1. Set VT to graphics mode (hides all text)
    for tty in ["/dev/tty0", "/dev/tty1", "/dev/console"]:
        try:
            fd = os.open(tty, os.O_RDWR)
            os.write(fd, b"\033[?25l")   # hide cursor
            os.write(fd, b"\033[2J")     # clear screen
            fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)
            os.close(fd)
            break
        except:
            try: os.close(fd)
            except: pass

    # 2. Disable cursor blink
    try:
        subprocess.run(["sudo", "sh", "-c",
                        "echo 0 > /sys/class/graphics/fbcon/cursor_blink"],
                       capture_output=True, timeout=3)
    except:
        pass

    # 3. Write zeros to framebuffer (clears any residual image)
    try:
        subprocess.run(["sudo", "dd", "if=/dev/zero", "of=/dev/fb0",
                        "bs=1M", "count=8"],
                       capture_output=True, timeout=5)
    except:
        pass


def _screen_restore_cli():
    """Restore CLI — show terminal with IP address."""
    # 1. Restore VT to text mode
    for tty in ["/dev/tty0", "/dev/tty1", "/dev/console"]:
        try:
            fd = os.open(tty, os.O_RDWR)
            fcntl.ioctl(fd, KDSETMODE, KD_TEXT)
            os.write(fd, b"\033[?25h")   # show cursor
            os.close(fd)
            break
        except:
            try: os.close(fd)
            except: pass

    # 2. Re-enable cursor blink
    try:
        subprocess.run(["sudo", "sh", "-c",
                        "echo 1 > /sys/class/graphics/fbcon/cursor_blink"],
                       capture_output=True, timeout=3)
    except:
        pass

    # 3. Print IP and hostname to tty1 so it's visible on screen
    try:
        ip_result = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=3)
        ip = ip_result.stdout.strip().split()[0] if ip_result.stdout.strip() else "unknown"
        hostname = socket.gethostname()
        msg = (f"\033[2J\033[H"  # clear + home
               f"\033[1;36m{'─' * 50}\033[0m\n"
               f"  \033[1;37mNDI Control Panel\033[0m\n"
               f"  \033[1;36m{'─' * 50}\033[0m\n\n"
               f"  Hostname:  \033[1;32m{hostname}\033[0m\n"
               f"  IP:        \033[1;32m{ip}\033[0m\n"
               f"  Web Panel: \033[1;34mhttp://{hostname}.local:5000\033[0m\n\n"
               f"  \033[0;37mOutput stopped. Use web panel to start.\033[0m\n\n")
        for tty in ["/dev/tty1", "/dev/tty0"]:
            try:
                fd = os.open(tty, os.O_WRONLY)
                os.write(fd, msg.encode())
                os.close(fd)
                break
            except:
                try: os.close(fd)
                except: pass
    except:
        pass


def _post_stop_screen():
    """After stopping an output, set screen state based on show_cli setting."""
    cfg = load_config()
    if cfg.get("show_cli", False):
        _screen_restore_cli()
    else:
        _screen_blank()


# ═══════════════════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════════════════

class StreamState(Enum):
    STOPPED = "stopped"; STARTING = "starting"; RUNNING = "running"; ERROR = "error"

@dataclass
class SenderInfo:
    state: StreamState = StreamState.STOPPED
    source_type: str = "test"; device: str = "0"; codec: str = "raw"
    width: int = 1280; height: int = 720; fps: int = 30
    ndi_name: str = ""; frame_count: int = 0
    actual_fps: float = 0.0; connections: int = 0; error: str = ""
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _sender: object = field(default=None, repr=False)
    _source: object = field(default=None, repr=False)
    def to_dict(self):
        return {"state": self.state.value, "source_type": self.source_type,
                "device": self.device, "codec": self.codec,
                "width": self.width, "height": self.height,
                "fps": self.fps, "ndi_name": self.ndi_name, "frame_count": self.frame_count,
                "actual_fps": round(self.actual_fps, 1), "connections": self.connections, "error": self.error}

@dataclass
class ReceiverInfo:
    state: StreamState = StreamState.STOPPED
    source_name: str = ""; width: int = 0; height: int = 0
    frame_count: int = 0; actual_fps: float = 0.0; error: str = ""
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _receiver: object = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _frame_jpeg: Optional[bytes] = field(default=None, repr=False)
    def to_dict(self):
        return {"state": self.state.value, "source_name": self.source_name,
                "width": self.width, "height": self.height, "frame_count": self.frame_count,
                "actual_fps": round(self.actual_fps, 1), "error": self.error}

@dataclass
class HdmiInfo:
    state: StreamState = StreamState.STOPPED
    source_name: str = ""; hdmi_index: int = 0
    width: int = 0; height: int = 0; error: str = ""
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)

    def to_dict(self):
        # Read live stats from the subprocess stats file
        stats = _read_hdmi_stats(self.hdmi_index)
        d = {"state": self.state.value, "source_name": self.source_name,
             "hdmi_index": self.hdmi_index, "width": self.width, "height": self.height,
             "error": self.error, "actual_fps": 0, "frame_count": 0,
             "source_w": 0, "source_h": 0, "render_w": 0, "render_h": 0}
        if stats and self.state == StreamState.RUNNING:
            d["actual_fps"] = stats.get("fps", 0)
            d["frame_count"] = stats.get("frames", 0)
            d["source_w"] = stats.get("source_w", 0)
            d["source_h"] = stats.get("source_h", 0)
            d["render_w"] = stats.get("render_w", 0)
            d["render_h"] = stats.get("render_h", 0)
        return d


def _read_hdmi_stats(hdmi_index):
    path = f"/tmp/ndi-hdmi-{hdmi_index}-stats.json"
    try:
        with open(path) as f: return json.load(f)
    except: return None


sender_info = SenderInfo()
receiver_info = ReceiverInfo()
hdmi_outputs = {0: HdmiInfo(hdmi_index=0), 1: HdmiInfo(hdmi_index=1)}


# ═══════════════════════════════════════════════════════════════════════════
# Video Sources
# ═══════════════════════════════════════════════════════════════════════════

class TestPatternSource:
    def __init__(self, w, h, fps):
        self.width, self.height, self.fps, self.n = w, h, fps, 0
        colors = [(192,192,192),(192,192,0),(0,192,192),(0,192,0),(192,0,192),(192,0,0),(0,0,192)]
        self.base = np.zeros((h, w, 4), dtype=np.uint8)
        bw = w // len(colors)
        for i, (r, g, b) in enumerate(colors):
            self.base[:, i*bw:((i+1)*bw if i < len(colors)-1 else w)] = [b, g, r, 255]
    def read(self):
        f = self.base.copy(); y = int((self.n * 3) % self.height)
        f[y:min(y+4, self.height), :] = [255,255,255,255]; self.n += 1; return True, f
    def release(self): pass

class WebcamSource:
    def __init__(self, dev, w, h, fps):
        self.cap = cv2.VideoCapture(dev)
        if not self.cap.isOpened(): raise RuntimeError(f"Cannot open webcam {dev}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w); self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
    def read(self):
        ret, f = self.cap.read()
        if ret: f = cv2.cvtColor(f, cv2.COLOR_BGR2BGRA)
        return ret, f
    def release(self): self.cap.release()

class PiCameraSource:
    def __init__(self, w, h, fps):
        from picamera2 import Picamera2
        self.picam = Picamera2()
        cfg = self.picam.create_video_configuration(main={"size": (w, h), "format": "XRGB8888"}, controls={"FrameRate": fps})
        self.picam.configure(cfg); self.picam.start(); time.sleep(1)
    def read(self):
        try:
            f = self.picam.capture_array()
            if f.shape[2] == 3: f = cv2.cvtColor(f, cv2.COLOR_BGR2BGRA)
            return True, f
        except: return False, None
    def release(self):
        try: self.picam.stop()
        except: pass

def try_quiet(fn):
    try: fn()
    except: pass


# ═══════════════════════════════════════════════════════════════════════════
# Sender Thread
# ═══════════════════════════════════════════════════════════════════════════

def _dev_to_index(device_str):
    """Convert device string to cv2 index: '/dev/video1' → 1, '1' → 1."""
    s = str(device_str)
    if s.startswith('/dev/video'):
        return int(s.replace('/dev/video', ''))
    try:
        return int(s)
    except:
        return 0

def _dev_to_path(device_str):
    """Convert device string to /dev path: '1' → '/dev/video1', '/dev/video1' → '/dev/video1'."""
    s = str(device_str)
    if s.startswith('/dev/'):
        return s
    return f'/dev/video{s}'

def _sender_loop():
    global sender_info; si = sender_info
    try:
        si.state = StreamState.STARTING

        # Raw (uncompressed) path — in-process
        dev_idx = _dev_to_index(si.device)
        if si.source_type == "webcam": source = WebcamSource(dev_idx, si.width, si.height, si.fps)
        elif si.source_type == "csi_hdmi": source = WebcamSource(dev_idx, si.width, si.height, si.fps)
        elif si.source_type == "picamera": source = PiCameraSource(si.width, si.height, si.fps)
        else: source = TestPatternSource(si.width, si.height, si.fps)
        si._source = source
        sender = ndi.send_create(si.ndi_name or f"{HOSTNAME}-NDI")
        if not sender: raise RuntimeError("Failed to create NDI sender")
        si._sender = sender; si.state = StreamState.RUNNING; si.frame_count = 0
        start = time.monotonic(); last_cc = start
        while not si._stop_event.is_set():
            ret, frame = source.read()
            if not ret: time.sleep(0.01); continue
            if frame.shape[1] != si.width or frame.shape[0] != si.height:
                frame = cv2.resize(frame, (si.width, si.height))
            if not frame.flags["C_CONTIGUOUS"]: frame = np.ascontiguousarray(frame)
            ndi.send_video_v2(sender, frame, fps_n=si.fps*1000, fps_d=1000)
            si.frame_count += 1; now = time.monotonic()
            elapsed = now - start
            if elapsed > 0: si.actual_fps = si.frame_count / elapsed
            if now - last_cc >= 2.0: si.connections = ndi.send_get_no_connections(sender, 0); last_cc = now
    except Exception as e: si.state = StreamState.ERROR; si.error = str(e); return
    finally:
        if si._source: try_quiet(si._source.release)
        if si._sender: try_quiet(lambda: ndi.send_destroy(si._sender))
        si._sender = si._source = None
        if si.state == StreamState.RUNNING: si.state = StreamState.STOPPED



# ═══════════════════════════════════════════════════════════════════════════
# Receiver Thread
# ═══════════════════════════════════════════════════════════════════════════

def _receiver_loop():
    global receiver_info; ri = receiver_info
    try:
        ri.state = StreamState.STARTING
        target = _find_source(ri.source_name, ri._stop_event)
        if not target: raise RuntimeError(f"Source '{ri.source_name}' not found after 10s")
        receiver = ndi.recv_create(source_dict=target)
        if not receiver: raise RuntimeError("Failed to create NDI receiver")
        ndi.recv_connect(receiver, target); ri._receiver = receiver
        ri.state = StreamState.RUNNING; ri.frame_count = 0
        start = time.monotonic(); preview_interval = 1.0 / PREVIEW_FPS; last_preview = 0.0
        while not ri._stop_event.is_set():
            try: ft, frame, w, h = ndi.recv_capture(receiver, timeout_ms=50)
            except: time.sleep(0.01); continue
            if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
                ri.width, ri.height = w, h; ri.frame_count += 1
                elapsed = time.monotonic() - start
                if elapsed > 0: ri.actual_fps = ri.frame_count / elapsed
                now = time.monotonic()
                if now - last_preview >= preview_interval:
                    last_preview = now; bgr = frame[:, :, :3]
                    if w > PREVIEW_MAX_WIDTH:
                        sc = PREVIEW_MAX_WIDTH / w
                        small = cv2.resize(bgr, (PREVIEW_MAX_WIDTH, int(h*sc)), interpolation=cv2.INTER_NEAREST)
                    else: small = bgr
                    _, jpeg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    with ri._lock: ri._frame_jpeg = jpeg.tobytes()
    except Exception as e: ri.state = StreamState.ERROR; ri.error = str(e); return
    finally:
        if ri._receiver: try_quiet(lambda: ndi.recv_destroy(ri._receiver))
        ri._receiver = None
        if ri.state == StreamState.RUNNING: ri.state = StreamState.STOPPED

def _find_source(name, stop_event=None):
    for _ in range(10):
        if stop_event and stop_event.is_set(): return None
        sources = ndi.discover_sources(timeout_ms=1000, extra_ips=_ndi_extra_ips)
        for s in sources:
            if name.lower() in s["name"].lower(): return s
    return None


# ═══════════════════════════════════════════════════════════════════════════
# HDMI Output
# ═══════════════════════════════════════════════════════════════════════════

def _get_hdmi_outputs():
    """Detect available framebuffer devices."""
    outputs = []
    for i in range(4):
        dev = f"/dev/fb{i}"
        if not os.path.exists(dev):
            continue
        try:
            import fcntl as _f, struct as _s
            fd = os.open(dev, os.O_RDONLY)
            buf = bytearray(160)
            _f.ioctl(fd, 0x4600, buf)
            xres, yres = _s.unpack_from("2I", buf, 0)
            os.close(fd)
            outputs.append({"index": i, "device": dev, "width": xres, "height": yres,
                            "label": f"fb{i} ({xres}x{yres})",
                            "modes": STANDARD_RESOLUTIONS})
        except Exception as e:
            outputs.append({"index": i, "device": dev, "width": 0, "height": 0,
                            "label": f"fb{i} (error)", "modes": STANDARD_RESOLUTIONS})
    return outputs


def _find_fb_device():
    """Find the first available framebuffer device index."""
    for i in range(4):
        if os.path.exists(f"/dev/fb{i}"):
            return i
    return 0


def _hdmi_start(source_name, hdmi_index=0, width=0, height=0, mode="auto"):
    global hdmi_outputs
    hi = hdmi_outputs.get(hdmi_index)
    if hi and hi._process and hi._process.poll() is None:
        return {"error": f"Output {hdmi_index} already running"}

    # Since all outputs share the same framebuffer, stop any other running output
    for other_idx, other_info in hdmi_outputs.items():
        if other_idx != hdmi_index and other_info._process and other_info._process.poll() is None:
            print(f"[i] Stopping Output {other_idx} (framebuffer shared)")
            _hdmi_stop(other_idx)

    info = HdmiInfo()
    info.source_name = source_name
    info.hdmi_index = hdmi_index
    info.width = width
    info.height = height
    info.state = StreamState.STARTING
    hdmi_outputs[hdmi_index] = info

    venv_python = os.path.join(os.path.expanduser("~"), "ndi-env", "bin", "python3")
    python_cmd = venv_python if os.path.exists(venv_python) else sys.executable
    script = os.path.join(SCRIPT_DIR, "ndi_hdmi.py")

    fb_dev = _find_fb_device()
    cmd = [python_cmd, script, "--source", source_name,
           "--hdmi", str(hdmi_index), "--fb", str(fb_dev)]
    if width > 0 and height > 0:
        cmd += ["--resolution", f"{width}x{height}"]
    cmd += ["--mode", mode]

    try:
        # Build env with display vars for desktop/auto modes
        child_env = os.environ.copy()
        if mode != "framebuffer":
            for proc_dir in glob.glob("/proc/[0-9]*/environ"):
                try:
                    with open(proc_dir, "rb") as f:
                        env_data = f.read()
                    for item in env_data.split(b"\x00"):
                        if b"=" in item:
                            k, v = item.split(b"=", 1)
                            ks = k.decode(errors="ignore")
                            if ks in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"):
                                child_env.setdefault(ks, v.decode(errors="ignore"))
                except:
                    continue

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                cwd=SCRIPT_DIR, preexec_fn=os.setsid, env=child_env)
        info._process = proc
        time.sleep(1.5)
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            info.state = StreamState.ERROR
            info.error = out[-500:] if out else "Process exited immediately"
            return info.to_dict()
        info.state = StreamState.RUNNING
    except Exception as e:
        info.state = StreamState.ERROR; info.error = str(e)
    return info.to_dict()


def _hdmi_stop(hdmi_index=0):
    global hdmi_outputs
    info = hdmi_outputs.get(hdmi_index)
    if not info:
        return {"state": "stopped", "hdmi_index": hdmi_index}

    # Stop the systemd autostart service first (prevents Restart=always from respawning)
    svc = _svc_name(hdmi_index)
    try:
        subprocess.run(["sudo", "systemctl", "stop", svc],
                       capture_output=True, timeout=5)
    except:
        pass
    try:
        subprocess.run(["sudo", "systemctl", "reset-failed", svc],
                       capture_output=True, timeout=3)
    except:
        pass

    if info._process and info._process.poll() is None:
        pid = info._process.pid
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            info._process.kill()
        except:
            pass
        try:
            info._process.wait(timeout=2)
        except:
            pass
    # Also kill any orphaned ndi_hdmi processes for this slot
    try:
        subprocess.run(["pkill", "-9", "-f", f"ndi_hdmi.py.*--hdmi {hdmi_index}"],
                       capture_output=True, timeout=3)
    except:
        pass
    # Close any orphaned SDL/OpenCV windows
    try:
        subprocess.run(["xdotool", "search", "--name", "NDI Output", "windowclose"],
                       capture_output=True, timeout=2)
    except:
        pass
    info.state = StreamState.STOPPED
    info._process = None
    try:
        os.remove(f"/tmp/ndi-hdmi-{hdmi_index}-stats.json")
    except:
        pass
    _post_stop_screen()
    return info.to_dict()


def _hdmi_check():
    for idx, info in hdmi_outputs.items():
        if info._process:
            if info._process.poll() is not None and info.state == StreamState.RUNNING:
                info.state = StreamState.STOPPED; info._process = None


# ═══════════════════════════════════════════════════════════════════════════
# Multiview
# ═══════════════════════════════════════════════════════════════════════════

_multiview_process = None
_multiview_config = {}  # {sources: [...], layout: "2x2", tally: {}, labels: True}

MULTIVIEW_STATS_PATH = "/tmp/ndi-multiview-stats.json"
IMAGES_DIR = os.path.join(SCRIPT_DIR, "static", "images")
os.makedirs(IMAGES_DIR, exist_ok=True)
ALLOWED_IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif'}

def _multiview_status():
    """Read multiview stats from the engine process.
    Works whether the process was started by the web panel or by systemd."""
    global _multiview_process
    # Check if web-panel-started process is still alive
    if _multiview_process and _multiview_process.poll() is not None:
        _multiview_process = None

    # Check stats file first — works regardless of who started the process
    stats_exists = os.path.exists(MULTIVIEW_STATS_PATH)
    if stats_exists:
        try:
            with open(MULTIVIEW_STATS_PATH) as f:
                stats = json.load(f)
            stats["running"] = True
            stats["config"] = _multiview_config
            return stats
        except:
            pass

    # If we have a web-panel-started process but no stats yet, it's starting
    if _multiview_process:
        return {"running": True, "state": "starting", "config": _multiview_config}

    # No stats file and no web-panel process — check if systemd service is running
    try:
        r = subprocess.run(["systemctl", "is-active", "ndi-multiview"],
                           capture_output=True, text=True, timeout=3)
        if r.stdout.strip() == "active":
            return {"running": True, "state": "starting", "config": _multiview_config}
    except:
        pass

    return {"running": False, "state": "stopped", "config": _multiview_config}


def _multiview_start(sources=None, layout="2x2", tally=None, labels=True, mode="auto",
                     output_res="", layout_data=None):
    global _multiview_process, _multiview_config

    if _multiview_process and _multiview_process.poll() is None:
        return {"error": "Multiview already running"}

    # Stop any running HDMI output (shares the display)
    for idx in list(hdmi_outputs.keys()):
        info = hdmi_outputs.get(idx)
        if info and info._process and info._process.poll() is None:
            print(f"[i] Stopping HDMI output {idx} (multiview needs the display)")
            _hdmi_stop(idx)

    _multiview_config = {
        "sources": sources or [],
        "layout": layout,
        "tally": tally or {},
        "labels": labels,
        "output_res": output_res,
        "layout_data": layout_data,
    }

    venv_python = os.path.join(os.path.expanduser("~"), "ndi-env", "bin", "python3")
    python_cmd = venv_python if os.path.exists(venv_python) else sys.executable
    script = os.path.join(SCRIPT_DIR, "ndi_multiview.py")

    # Write layout file and build command
    layout_file = os.path.join(SCRIPT_DIR, "multiview_layout.json")
    if layout_data and "rows" in layout_data:
        # Flex layout mode — write layout file
        layout_json = {
            "rows": layout_data["rows"],
            "labels": labels,
            "output_res": output_res,
            "mode": mode,
        }
        with open(layout_file, "w") as f:
            json.dump(layout_json, f, indent=2)

        cmd = [python_cmd, script, "--layout-file", layout_file, "--mode", mode]
        if output_res:
            cmd += ["--output-res", output_res]
        if not labels:
            cmd.append("--no-labels")

        # Count sources for validation
        src_count = sum(1 for r in layout_data["rows"]
                        for t in r.get("tiles", []) if t.get("source"))
        if src_count == 0:
            return {"error": "No sources in layout"}
    else:
        # Simple mode (backward compat)
        if not sources:
            return {"error": "No sources specified"}
        cmd = [python_cmd, script,
               "--sources", ",".join(sources),
               "--layout", layout,
               "--mode", mode]
        if not labels:
            cmd.append("--no-labels")
        if tally:
            tally_parts = [f"{k}={v}" for k, v in tally.items() if v != "none"]
            if tally_parts:
                cmd += ["--tally", ",".join(tally_parts)]
        if output_res:
            cmd += ["--output-res", output_res]

    try:
        child_env = os.environ.copy()
        if mode != "framebuffer":
            for proc_dir in glob.glob("/proc/[0-9]*/environ"):
                try:
                    with open(proc_dir, "rb") as f:
                        env_data = f.read()
                    for item in env_data.split(b"\x00"):
                        if b"=" in item:
                            k, v = item.split(b"=", 1)
                            ks = k.decode(errors="ignore")
                            if ks in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"):
                                child_env.setdefault(ks, v.decode(errors="ignore"))
                except:
                    continue

        child_env.setdefault("LD_LIBRARY_PATH", "/usr/local/lib")

        print(f"[DEBUG] Multiview cmd: {' '.join(cmd)}")
        _multiview_process = subprocess.Popen(
            cmd, stdout=None, stderr=None,
            cwd=SCRIPT_DIR, preexec_fn=os.setsid, env=child_env)

        print(f"[OK] Multiview started: PID {_multiview_process.pid}, layout={layout}, "
              f"sources={len(sources)}, output_res={output_res or 'native'}")
        return {"ok": True, "pid": _multiview_process.pid}
    except Exception as e:
        return {"error": str(e)}


def _multiview_stop():
    global _multiview_process

    # 1. Stop systemd service (prevents auto-restart)
    try:
        subprocess.run(["sudo", "systemctl", "stop", "ndi-multiview"],
                       capture_output=True, timeout=5)
    except:
        pass

    # 2. Reset any failed state so systemd doesn't try to restart
    try:
        subprocess.run(["sudo", "systemctl", "reset-failed", "ndi-multiview"],
                       capture_output=True, timeout=3)
    except:
        pass

    # 3. Kill web-panel-started process (SIGKILL immediately — no grace period)
    if _multiview_process:
        pid = _multiview_process.pid
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            _multiview_process.kill()
        except:
            pass
        try:
            _multiview_process.wait(timeout=2)
        except:
            pass

    _multiview_process = None

    # 4. Kill ALL orphaned ndi_multiview.py processes (belt and suspenders)
    try:
        subprocess.run(["pkill", "-9", "-f", "ndi_multiview.py"],
                       capture_output=True, timeout=3)
    except:
        pass

    # 5. Brief pause to let kernel reclaim resources
    time.sleep(0.2)

    # 6. Verify nothing survived
    try:
        result = subprocess.run(["pgrep", "-f", "ndi_multiview.py"],
                                capture_output=True, timeout=2)
        if result.returncode == 0:
            # Something survived — kill again
            subprocess.run(["pkill", "-9", "-f", "ndi_multiview.py"],
                           capture_output=True, timeout=3)
    except:
        pass

    # 7. Clean up stats file
    try:
        os.remove(MULTIVIEW_STATS_PATH)
    except:
        pass

    # 8. Set screen state (black or CLI) based on setting
    _post_stop_screen()

    return {"ok": True, "state": "stopped"}


def _multiview_update_tally(tally):
    """Update tally map. If multiview is running, restart with new tally."""
    global _multiview_config
    _multiview_config["tally"] = tally
    if _multiview_process and _multiview_process.poll() is None:
        # Restart with full config
        _multiview_stop()
        time.sleep(0.5)
        return _multiview_start(
            sources=_multiview_config.get("sources"),
            layout=_multiview_config.get("layout", "2x2"),
            tally=tally,
            labels=_multiview_config.get("labels", True),
            mode="auto",
            output_res=_multiview_config.get("output_res", ""),
            layout_data=_multiview_config.get("layout_data"),
        )
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# Autostart
# ═══════════════════════════════════════════════════════════════════════════

def _svc_name(idx): return f"{SERVICE_PREFIX}-hdmi{idx}"
def _svc_path(idx): return f"/etc/systemd/system/{_svc_name(idx)}.service"

# --- Boot-time HDMI resolution via kernel cmdline + config.txt ---

CMDLINE_PATH = "/boot/firmware/cmdline.txt"
CONFIG_PATH = "/boot/firmware/config.txt"

def _hdmi_connector_name(hdmi_index):
    """Pi 5: HDMI 0 (left) = HDMI-A-1, HDMI 1 (right) = HDMI-A-2."""
    return f"HDMI-A-{hdmi_index + 1}"

def _set_boot_resolution(hdmi_index, width, height):
    """Set HDMI output resolution in kernel cmdline.txt for next boot.
    Also ensures framebuffer_depth=32 in config.txt."""
    connector = _hdmi_connector_name(hdmi_index)

    # --- Update cmdline.txt with video= parameter + cursor/blank settings ---
    try:
        r = subprocess.run(["sudo", "cat", CMDLINE_PATH],
                           capture_output=True, text=True, timeout=5)
        cmdline = r.stdout.strip()

        # Remove any existing video= param for this connector
        cmdline = re.sub(rf'\s*video={re.escape(connector)}:\S*', '', cmdline)
        # Remove existing cursor/blank params (we'll re-add them)
        cmdline = re.sub(r'\s*vt\.global_cursor_default=\d', '', cmdline)
        cmdline = re.sub(r'\s*consoleblank=\d+', '', cmdline)
        cmdline = re.sub(r'\s*logo\.nologo', '', cmdline)

        # Add new params
        if width > 0 and height > 0:
            cmdline = cmdline.strip() + f" video={connector}:{width}x{height}@60"
        # Disable blinking cursor, console blanking, and boot logo
        cmdline = cmdline.strip() + " vt.global_cursor_default=0 consoleblank=0 logo.nologo"

        subprocess.run(["sudo", "tee", CMDLINE_PATH],
                       input=(cmdline.strip() + "\n").encode(),
                       capture_output=True, timeout=5)
        print(f"[OK] Boot resolution: {connector}:{width}x{height}@60 (cursor off, blank off)")
    except Exception as e:
        print(f"[WARN] Could not set boot resolution: {e}")

    # --- Ensure framebuffer_depth=32 in config.txt ---
    try:
        r = subprocess.run(["sudo", "cat", CONFIG_PATH],
                           capture_output=True, text=True, timeout=5)
        config = r.stdout

        # Check if framebuffer_depth is already set
        if re.search(r'^framebuffer_depth=', config, re.MULTILINE):
            config = re.sub(r'^framebuffer_depth=\d+',
                           'framebuffer_depth=32', config, flags=re.MULTILINE)
        else:
            # Add it before the first [section] or at end
            if '\n[' in config:
                config = re.sub(r'\n(\[)', r'\nframebuffer_depth=32\n\n\1',
                               config, count=1)
            else:
                config = config.rstrip() + "\nframebuffer_depth=32\n"

        subprocess.run(["sudo", "tee", CONFIG_PATH],
                       input=config.encode(), capture_output=True, timeout=5)
        print("[OK] framebuffer_depth=32 set in config.txt")
    except Exception as e:
        print(f"[WARN] Could not set framebuffer_depth: {e}")

def _clear_boot_resolution(hdmi_index):
    """Remove video= and cursor/blank parameters from cmdline.txt."""
    connector = _hdmi_connector_name(hdmi_index)
    try:
        r = subprocess.run(["sudo", "cat", CMDLINE_PATH],
                           capture_output=True, text=True, timeout=5)
        cmdline = r.stdout.strip()
        cmdline = re.sub(rf'\s*video={re.escape(connector)}:\S*', '', cmdline)
        cmdline = re.sub(r'\s*vt\.global_cursor_default=\d', '', cmdline)
        cmdline = re.sub(r'\s*consoleblank=\d+', '', cmdline)
        cmdline = re.sub(r'\s*logo\.nologo', '', cmdline)
        subprocess.run(["sudo", "tee", CMDLINE_PATH],
                       input=(cmdline.strip() + "\n").encode(),
                       capture_output=True, timeout=5)
    except:
        pass

def _autostart_status():
    cfg = load_config()
    out = []
    for idx in range(2):
        enabled = False
        if os.path.exists(_svc_path(idx)):
            r = subprocess.run(["systemctl", "is-enabled", _svc_name(idx)], capture_output=True, text=True)
            enabled = r.stdout.strip() == "enabled"
        ac = cfg.get(f"autostart_{idx}", {})
        out.append({"hdmi_index": idx, "enabled": enabled,
                     "source_name": ac.get("source", ""),
                     "width": ac.get("width", 0), "height": ac.get("height", 0)})
    return out

def _autostart_enable(hdmi_index, source_name, width=0, height=0, mode="auto"):
    # Only one autostart type allowed — disable multiview if active
    if _mv_autostart_status().get("enabled"):
        _mv_autostart_disable()
        print("[i] Disabled multiview autostart (switching to HDMI autostart)")

    cfg = load_config()
    cfg[f"autostart_{hdmi_index}"] = {"source": source_name, "width": width, "height": height, "mode": mode}
    save_config(cfg)

    # Set HDMI output resolution + 32bpp for next boot (CLI framebuffer mode)
    if width > 0 and height > 0:
        _set_boot_resolution(hdmi_index, width, height)

    user = os.environ.get("SUDO_USER", os.environ.get("USER", "pi"))
    home = os.path.expanduser(f"~{user}") if user != "root" else os.path.expanduser("~")
    venv_python = os.path.join(home, "ndi-env", "bin", "python3")
    python_cmd = venv_python if os.path.exists(venv_python) else sys.executable
    res_args = f" --resolution {width}x{height}" if width > 0 and height > 0 else ""
    svc = _svc_name(hdmi_index)

    wrapper_path = os.path.join(SCRIPT_DIR, f"start_hdmi{hdmi_index}.sh")
    wrapper_content = f"""#!/bin/bash
# Auto-generated wrapper for NDI HDMI Output {hdmi_index}

echo "[wrapper] Starting NDI HDMI Output {hdmi_index}..."

# --- Wait for network ---
echo "[wrapper] Waiting for network..."
for i in $(seq 1 30); do
    if ip route | grep -q default; then
        echo "[wrapper] Network ready"
        break
    fi
    sleep 1
done

export LD_LIBRARY_PATH=/usr/local/lib
cd {SCRIPT_DIR}

# --- Quick check: is a display server running? ---
# If graphical.target is active, a desktop is expected — wait for it.
# If not (CLI boot), skip straight to framebuffer.
FOUND_DISPLAY=0
if systemctl is-active --quiet graphical.target 2>/dev/null; then
    echo "[wrapper] Desktop detected, waiting for display server..."
    for attempt in $(seq 1 60); do
        for pid_env in /proc/[0-9]*/environ; do
            if grep -qz "WAYLAND_DISPLAY=" "$pid_env" 2>/dev/null; then
                export WAYLAND_DISPLAY=$(grep -z "^WAYLAND_DISPLAY=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                export XDG_RUNTIME_DIR=$(grep -z "^XDG_RUNTIME_DIR=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                [ -z "$XDG_RUNTIME_DIR" ] && export XDG_RUNTIME_DIR="/run/user/$(id -u {user})"
                FOUND_DISPLAY=1
                echo "[wrapper] Found Wayland: WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
                break 2
            fi
            if grep -qz "^DISPLAY=" "$pid_env" 2>/dev/null; then
                export DISPLAY=$(grep -z "^DISPLAY=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                XAUTH=$(grep -z "^XAUTHORITY=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                [ -n "$XAUTH" ] && export XAUTHORITY="$XAUTH"
                FOUND_DISPLAY=1
                echo "[wrapper] Found X11: DISPLAY=$DISPLAY"
                break 2
            fi
        done
        [ $((attempt % 10)) -eq 0 ] && echo "[wrapper] Still waiting for display... ($attempt/60)"
        sleep 1
    done
else
    echo "[wrapper] CLI boot — skipping display server wait"
fi

if [ "$FOUND_DISPLAY" = "1" ]; then
    MODE="desktop"
    echo "[wrapper] Using desktop (SDL2) mode"
else
    MODE="framebuffer"
    echo "[wrapper] Using framebuffer mode"
    # Hide console cursor before starting framebuffer output
    echo 0 | sudo tee /sys/class/graphics/fbcon/cursor_blink >/dev/null 2>&1
    sudo sh -c 'echo -e "\\033[?25l" > /dev/tty0' 2>/dev/null
    sudo sh -c 'echo -e "\\033[?25l" > /dev/tty1' 2>/dev/null
    setterm --cursor off --blank 0 2>/dev/null
fi

echo "[wrapper] Launching: --source \\"{source_name}\\" --mode $MODE{res_args}"
exec {python_cmd} {SCRIPT_DIR}/ndi_hdmi.py --source "{source_name}" --hdmi {hdmi_index} --fb 0 --mode $MODE{res_args}
"""

    content = f"""[Unit]
Description=NDI HDMI Output {hdmi_index} (Autostart)
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={SCRIPT_DIR}
ExecStart=/bin/bash {wrapper_path}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    try:
        # Write wrapper script
        with open(wrapper_path, "w") as f:
            f.write(wrapper_content)
        os.chmod(wrapper_path, 0o755)
        # Write and enable systemd service
        r = subprocess.run(["sudo", "tee", _svc_path(hdmi_index)], input=content.encode(), capture_output=True)
        if r.returncode != 0: return {"error": r.stderr.decode()}
        subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["sudo", "systemctl", "enable", svc], capture_output=True)
        return {"enabled": True, "hdmi_index": hdmi_index, "source_name": source_name, "width": width, "height": height}
    except Exception as e: return {"error": str(e)}

def _autostart_disable(hdmi_index):
    svc = _svc_name(hdmi_index)
    wrapper_path = os.path.join(SCRIPT_DIR, f"start_hdmi{hdmi_index}.sh")
    try:
        subprocess.run(["sudo", "systemctl", "stop", svc], capture_output=True)
        subprocess.run(["sudo", "systemctl", "disable", svc], capture_output=True)
        subprocess.run(["sudo", "rm", "-f", _svc_path(hdmi_index)], capture_output=True)
        subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
        _clear_boot_resolution(hdmi_index)
        try:
            os.remove(wrapper_path)
        except:
            pass
        return {"enabled": False, "hdmi_index": hdmi_index}
    except Exception as e: return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Multiview Autostart
# ═══════════════════════════════════════════════════════════════════════════

_MV_SVC = "ndi-multiview"
_MV_SVC_PATH = f"/etc/systemd/system/{_MV_SVC}.service"
_MV_WRAPPER = os.path.join(SCRIPT_DIR, "start_multiview.sh")
_MV_LAYOUT_FILE = os.path.join(SCRIPT_DIR, "multiview_layout.json")

def _mv_autostart_status():
    cfg = load_config()
    mv_cfg = cfg.get("multiview_autostart", {})
    enabled = False
    if os.path.exists(_MV_SVC_PATH):
        try:
            r = subprocess.run(["systemctl", "is-enabled", _MV_SVC],
                               capture_output=True, text=True, timeout=5)
            enabled = r.stdout.strip() == "enabled"
        except:
            pass
    return {
        "enabled": enabled,
        "sources": mv_cfg.get("sources", []),
        "layout": mv_cfg.get("layout", "2x2"),
        "tally": mv_cfg.get("tally", {}),
        "labels": mv_cfg.get("labels", True),
        "output_res": mv_cfg.get("output_res", ""),
        "layout_data": mv_cfg.get("layout_data"),
    }


def _mv_autostart_enable(sources=None, layout="2x2", tally=None, labels=True,
                        output_res="", layout_data=None):
    # Only one autostart type allowed — disable any HDMI autostart
    for a in _autostart_status():
        if a.get("enabled"):
            _autostart_disable(a["hdmi_index"])
            print(f"[i] Disabled HDMI {a['hdmi_index']} autostart (switching to multiview)")

    cfg = load_config()
    cfg["multiview_autostart"] = {
        "sources": sources or [], "layout": layout,
        "tally": tally or {}, "labels": labels,
        "output_res": output_res,
        "layout_data": layout_data,
    }
    save_config(cfg)

    # Set HDMI boot resolution
    if output_res:
        m = re.match(r"(\d+)x(\d+)", output_res)
        if m:
            _set_boot_resolution(0, int(m.group(1)), int(m.group(2)))
    else:
        _clear_boot_resolution(0)

    # Write layout file (used by both wrapper and live start)
    if layout_data and "rows" in layout_data:
        layout_json = {
            "rows": layout_data["rows"],
            "labels": labels,
            "output_res": output_res,
            "mode": "auto",
        }
        with open(_MV_LAYOUT_FILE, "w") as f:
            json.dump(layout_json, f, indent=2)

    user = os.environ.get("SUDO_USER", os.environ.get("USER", "pi"))
    home = os.path.expanduser(f"~{user}") if user != "root" else os.path.expanduser("~")
    venv_python = os.path.join(home, "ndi-env", "bin", "python3")
    python_cmd = venv_python if os.path.exists(venv_python) else sys.executable

    labels_arg = " --no-labels" if not labels else ""
    output_res_arg = f" --output-res {output_res}" if output_res else ""

    # Determine launch args — prefer layout-file, fall back to simple mode
    if layout_data and "rows" in layout_data:
        launch_args = f"--layout-file {_MV_LAYOUT_FILE}{labels_arg}{output_res_arg}"
    else:
        sources_arg = ",".join(sources or [])
        tally_arg = ""
        if tally:
            tally_parts = [f"{k}={v}" for k, v in tally.items() if v != "none"]
            if tally_parts:
                tally_joined = ','.join(tally_parts)
                tally_arg = f' --tally "{tally_joined}"'
        launch_args = f'--sources "{sources_arg}" --layout {layout}{labels_arg}{tally_arg}{output_res_arg}'

    wrapper_content = f"""#!/bin/bash
# Auto-generated wrapper for NDI Multiview

echo "[multiview] Starting NDI Multiview..."

# --- Wait for network ---
echo "[multiview] Waiting for network..."
for i in $(seq 1 30); do
    if ip route | grep -q default; then
        echo "[multiview] Network ready"
        break
    fi
    sleep 1
done

export LD_LIBRARY_PATH=/usr/local/lib
cd {SCRIPT_DIR}

# --- Detect display mode ---
FOUND_DISPLAY=0
if systemctl is-active --quiet graphical.target 2>/dev/null; then
    echo "[multiview] Desktop detected, waiting for display server..."
    for attempt in $(seq 1 60); do
        for pid_env in /proc/[0-9]*/environ; do
            if grep -qz "WAYLAND_DISPLAY=" "$pid_env" 2>/dev/null; then
                export WAYLAND_DISPLAY=$(grep -z "^WAYLAND_DISPLAY=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                export XDG_RUNTIME_DIR=$(grep -z "^XDG_RUNTIME_DIR=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                [ -z "$XDG_RUNTIME_DIR" ] && export XDG_RUNTIME_DIR="/run/user/$(id -u {user})"
                FOUND_DISPLAY=1
                echo "[multiview] Found Wayland: WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
                break 2
            fi
            if grep -qz "^DISPLAY=" "$pid_env" 2>/dev/null; then
                export DISPLAY=$(grep -z "^DISPLAY=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                XAUTH=$(grep -z "^XAUTHORITY=" "$pid_env" 2>/dev/null | tr -d '\\0' | cut -d= -f2)
                [ -n "$XAUTH" ] && export XAUTHORITY="$XAUTH"
                FOUND_DISPLAY=1
                echo "[multiview] Found X11: DISPLAY=$DISPLAY"
                break 2
            fi
        done
        [ $((attempt % 10)) -eq 0 ] && echo "[multiview] Still waiting for display... ($attempt/60)"
        sleep 1
    done
else
    echo "[multiview] CLI boot — skipping display server wait"
fi

if [ "$FOUND_DISPLAY" = "1" ]; then
    MODE="desktop"
    echo "[multiview] Using desktop (SDL2) mode"
else
    MODE="framebuffer"
    echo "[multiview] Using framebuffer mode"
    echo 0 | sudo tee /sys/class/graphics/fbcon/cursor_blink >/dev/null 2>&1
    sudo sh -c 'echo -e "\\033[?25l" > /dev/tty0' 2>/dev/null
    sudo sh -c 'echo -e "\\033[?25l" > /dev/tty1' 2>/dev/null
    setterm --cursor off --blank 0 2>/dev/null
fi

echo "[multiview] Launching: {launch_args} --mode $MODE"
exec {python_cmd} {SCRIPT_DIR}/ndi_multiview.py {launch_args} --mode $MODE
"""

    content_svc = f"""[Unit]
Description=NDI Multiview (Autostart)
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={SCRIPT_DIR}
ExecStart=/bin/bash {_MV_WRAPPER}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(_MV_WRAPPER, "w") as f:
            f.write(wrapper_content)
        os.chmod(_MV_WRAPPER, 0o755)
        r = subprocess.run(["sudo", "tee", _MV_SVC_PATH], input=content_svc.encode(), capture_output=True)
        if r.returncode != 0:
            return {"error": r.stderr.decode()}
        subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["sudo", "systemctl", "enable", _MV_SVC], capture_output=True)
        return {"enabled": True}
    except Exception as e:
        return {"error": str(e)}


def _mv_autostart_disable():
    try:
        subprocess.run(["sudo", "systemctl", "stop", _MV_SVC], capture_output=True)
        subprocess.run(["sudo", "systemctl", "disable", _MV_SVC], capture_output=True)
        subprocess.run(["sudo", "rm", "-f", _MV_SVC_PATH], capture_output=True)
        subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
        _clear_boot_resolution(0)
        try:
            os.remove(_MV_WRAPPER)
        except:
            pass
        cfg = load_config()
        cfg.pop("multiview_autostart", None)
        save_config(cfg)
        return {"enabled": False}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/api/status")
def api_status():
    _hdmi_check()
    cfg = load_config()
    return jsonify({"hostname": HOSTNAME,
                     "sender": sender_info.to_dict(),
                     "receiver": receiver_info.to_dict(),
                     "hdmi": {str(k): v.to_dict() for k, v in hdmi_outputs.items()},
                     "autostart": _autostart_status(),
                     "multiview": _multiview_status(),
                     "mv_autostart": _mv_autostart_status(),
                     "show_cli": cfg.get("show_cli", False),
                     "wall": _wall_worker_status(),
                     "ptz": {"ndi_source": _ptz_source_name or ""}})

@app.route("/api/sources")
def api_sources():
    try:
        with _sources_lock:
            sources = list(_known_sources)
        # If no cached sources yet (first few seconds), do a live scan
        if not sources:
            found = ndi.discover_sources(timeout_ms=5000, extra_ips=_ndi_extra_ips)
            sources = [{"name": s["name"], "url": s.get("url", "")} for s in found]
            with _sources_lock:
                _known_sources[:] = sources
        return jsonify({"sources": sources})
    except Exception as e:
        return jsonify({"sources": [], "error": str(e)}), 500

@app.route("/api/cameras")
def api_cameras():
    """List available V4L2 cameras with their formats."""
    cameras = []
    try:
        r = subprocess.run(['v4l2-ctl', '--list-devices'], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line and not line.startswith('/dev') and not line.startswith('\t'):
                cam_name = line.split('(')[0].strip().rstrip(':')
                bus_info = ""
                if '(' in line and ')' in line:
                    bus_info = line.split('(')[1].rstrip('):').strip()
                devices = []
                i += 1
                while i < len(lines) and (lines[i].startswith('\t') or lines[i].startswith('/dev')):
                    dev = lines[i].strip()
                    if dev.startswith('/dev/video'):
                        devices.append(dev)
                    i += 1
                if devices:
                    if any(skip in cam_name.lower() for skip in ['bcm2835', 'rpi-hevc', 'pispbe']):
                        continue
                    dev = devices[0]
                    cam_type = 'csi' if 'unicam' in cam_name.lower() else 'usb'
                    cameras.append({'name': cam_name, 'device': dev, 'type': cam_type})
            else:
                i += 1
    except:
        pass
    return jsonify({"cameras": cameras})

@app.route("/api/sender/start", methods=["POST"])
def api_sender_start():
    global sender_info
    if sender_info.state == StreamState.RUNNING: return jsonify({"error": "Already running"}), 400
    d = request.json or {}; sender_info = SenderInfo()
    sender_info.source_type = d.get("source_type", "test"); sender_info.device = d.get("device", "0")
    sender_info.codec = d.get("codec", "raw")
    sender_info.width = int(d.get("width", 1280)); sender_info.height = int(d.get("height", 720))
    sender_info.fps = int(d.get("fps", 30)); sender_info.ndi_name = d.get("ndi_name", f"{HOSTNAME}-NDI")
    t = threading.Thread(target=_sender_loop, daemon=True); sender_info.thread = t; t.start()
    time.sleep(0.3); return jsonify(sender_info.to_dict())

@app.route("/api/sender/stop", methods=["POST"])
def api_sender_stop():
    if sender_info.state not in (StreamState.RUNNING, StreamState.STARTING): return jsonify({"error": "Not running"}), 400
    sender_info._stop_event.set()
    if sender_info.thread: sender_info.thread.join(timeout=5)
    sender_info.state = StreamState.STOPPED; return jsonify(sender_info.to_dict())

@app.route("/api/receiver/start", methods=["POST"])
def api_receiver_start():
    global receiver_info
    if receiver_info.state == StreamState.RUNNING: return jsonify({"error": "Already running"}), 400
    d = request.json or {}; sn = d.get("source_name", "")
    if not sn: return jsonify({"error": "source_name required"}), 400
    receiver_info = ReceiverInfo(); receiver_info.source_name = sn
    t = threading.Thread(target=_receiver_loop, daemon=True); receiver_info.thread = t; t.start()
    time.sleep(0.3); return jsonify(receiver_info.to_dict())

@app.route("/api/receiver/stop", methods=["POST"])
def api_receiver_stop():
    if receiver_info.state not in (StreamState.RUNNING, StreamState.STARTING): return jsonify({"error": "Not running"}), 400
    receiver_info._stop_event.set()
    if receiver_info.thread: receiver_info.thread.join(timeout=5)
    receiver_info.state = StreamState.STOPPED; return jsonify(receiver_info.to_dict())

@app.route("/api/receiver/preview")
def api_receiver_preview():
    def gen():
        while True:
            if receiver_info.state != StreamState.RUNNING:
                blank = np.zeros((180,320,3), dtype=np.uint8)
                cv2.putText(blank, "No stream", (80,100), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80,80,80), 2)
                _, jpeg = cv2.imencode(".jpg", blank); data = jpeg.tobytes()
            else:
                with receiver_info._lock: data = receiver_info._frame_jpeg
                if not data: time.sleep(0.05); continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
            time.sleep(1.0 / PREVIEW_FPS)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ═══════════════════════════════════════════════════════════════════════════
# PTZ Control
# ═══════════════════════════════════════════════════════════════════════════

_ptz_receiver = None        # NDI receiver handle for PTZ
_ptz_source_name = None     # Currently connected NDI source
_ptz_lock = threading.Lock()


def _usb_ptz_cmd(device, ctrl, value):
    """Set a V4L2 control on a USB camera."""
    try:
        subprocess.run(['v4l2-ctl', '-d', str(device), '-c', f'{ctrl}={value}'],
                       capture_output=True, timeout=3)
        return True
    except:
        return False


def _usb_ptz_get(device, ctrl):
    """Get a V4L2 control value from a USB camera."""
    try:
        r = subprocess.run(['v4l2-ctl', '-d', str(device), '-C', ctrl],
                           capture_output=True, text=True, timeout=3)
        # Output: "pan_absolute: 0"
        return int(r.stdout.strip().split(':')[1].strip())
    except:
        return None


def _usb_ptz_get_controls(device):
    """Get available PTZ controls and their ranges for a USB camera."""
    controls = {}
    try:
        r = subprocess.run(['v4l2-ctl', '-d', str(device), '--list-ctrls-menus'],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.split('\n'):
            for name in ['pan_absolute', 'tilt_absolute', 'zoom_absolute',
                         'pan_speed', 'tilt_speed', 'zoom_continuous',
                         'focus_absolute', 'focus_auto']:
                if name in line and '(' in line:
                    ctrl = {'name': name}
                    for part in line.split():
                        if part.startswith('min='): ctrl['min'] = int(part.split('=')[1])
                        elif part.startswith('max='): ctrl['max'] = int(part.split('=')[1])
                        elif part.startswith('step='): ctrl['step'] = int(part.split('=')[1])
                        elif part.startswith('value='): ctrl['value'] = int(part.split('=')[1])
                    controls[name] = ctrl
    except:
        pass
    return controls


def _ndi_ptz_connect(source_name):
    """Connect a dedicated NDI receiver for PTZ control."""
    global _ptz_receiver, _ptz_source_name
    with _ptz_lock:
        if _ptz_receiver and _ptz_source_name == source_name:
            return _ptz_receiver  # Already connected
        if _ptz_receiver:
            ndi.recv_destroy(_ptz_receiver)
            _ptz_receiver = None
            _ptz_source_name = None

        # Find the source — check cache first, then do fresh scan
        src = None
        with _sources_lock:
            src = next((s for s in _known_sources if s['name'] == source_name), None)
        if not src:
            # Fresh scan with extra_ips for HX cameras
            found = ndi.discover_sources(timeout_ms=5000, extra_ips=_ndi_extra_ips)
            src = next((s for s in found if s['name'] == source_name), None)
        if not src:
            return None

        recv = ndi.recv_create(src, bandwidth=ndi.RECV_BANDWIDTH_HIGHEST)
        if not recv:
            return None
        _ptz_receiver = recv
        _ptz_source_name = source_name
        # Must capture at least one frame before PTZ support is reported
        for _ in range(50):  # Try for up to 5 seconds
            try:
                frame_type, _, _, _ = ndi.recv_capture(recv, timeout_ms=100)
                if frame_type != ndi.FRAME_TYPE_NONE:
                    break
            except:
                time.sleep(0.1)
        return recv


def _ndi_ptz_disconnect():
    """Disconnect PTZ receiver."""
    global _ptz_receiver, _ptz_source_name
    with _ptz_lock:
        if _ptz_receiver:
            ndi.recv_destroy(_ptz_receiver)
        _ptz_receiver = None
        _ptz_source_name = None


@app.route("/api/ptz/status")
def api_ptz_status():
    """Get PTZ status: available USB cameras with PTZ and NDI PTZ support."""
    usb_cams = []
    try:
        r = subprocess.run(['v4l2-ctl', '--list-devices'], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line and not line.startswith('/dev') and not line.startswith('\t'):
                cam_name = line.split('(')[0].strip().rstrip(':')
                devices = []
                i += 1
                while i < len(lines) and (lines[i].startswith('\t') or lines[i].startswith('/dev')):
                    dev = lines[i].strip()
                    if dev.startswith('/dev/video'):
                        devices.append(dev)
                    i += 1
                if devices:
                    if any(skip in cam_name.lower() for skip in ['bcm2835', 'rpi-hevc', 'pispbe']):
                        continue
                    dev = devices[0]
                    ctrls = _usb_ptz_get_controls(dev)
                    if ctrls:
                        usb_cams.append({'name': cam_name, 'device': dev, 'controls': ctrls})
            else:
                i += 1
    except:
        pass

    ndi_ptz = None
    with _ptz_lock:
        if _ptz_receiver and _ptz_source_name:
            supported = ndi.recv_ptz_is_supported(_ptz_receiver)
            ndi_ptz = {'source': _ptz_source_name, 'supported': supported}

    return jsonify({'usb_cameras': usb_cams, 'ndi_ptz': ndi_ptz})


@app.route("/api/ptz/usb/move", methods=["POST"])
def api_ptz_usb_move():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    pan = d.get("pan")      # Absolute position or None
    tilt = d.get("tilt")     # Absolute position or None
    pan_speed = d.get("pan_speed")   # Speed mode
    tilt_speed = d.get("tilt_speed") # Speed mode
    ok = True
    if pan is not None: ok &= _usb_ptz_cmd(device, 'pan_absolute', int(pan))
    if tilt is not None: ok &= _usb_ptz_cmd(device, 'tilt_absolute', int(tilt))
    if pan_speed is not None: ok &= _usb_ptz_cmd(device, 'pan_speed', int(pan_speed))
    if tilt_speed is not None: ok &= _usb_ptz_cmd(device, 'tilt_speed', int(tilt_speed))
    return jsonify({"ok": ok})


@app.route("/api/ptz/usb/zoom", methods=["POST"])
def api_ptz_usb_zoom():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    zoom = d.get("zoom")
    if zoom is not None:
        return jsonify({"ok": _usb_ptz_cmd(device, 'zoom_absolute', int(zoom))})
    return jsonify({"error": "zoom required"}), 400


@app.route("/api/ptz/usb/home", methods=["POST"])
def api_ptz_usb_home():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    _usb_ptz_cmd(device, 'pan_absolute', 0)
    _usb_ptz_cmd(device, 'tilt_absolute', 0)
    _usb_ptz_cmd(device, 'zoom_absolute', 0)
    return jsonify({"ok": True})


@app.route("/api/ptz/usb/get", methods=["POST"])
def api_ptz_usb_get():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    return jsonify({
        "pan": _usb_ptz_get(device, 'pan_absolute'),
        "tilt": _usb_ptz_get(device, 'tilt_absolute'),
        "zoom": _usb_ptz_get(device, 'zoom_absolute'),
    })


@app.route("/api/ptz/usb/preset/store", methods=["POST"])
def api_ptz_usb_preset_store():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    preset = int(d.get("preset", 0))
    pan = _usb_ptz_get(device, 'pan_absolute')
    tilt = _usb_ptz_get(device, 'tilt_absolute')
    zoom = _usb_ptz_get(device, 'zoom_absolute')
    if pan is None or tilt is None or zoom is None:
        return jsonify({"error": "Cannot read current position"}), 400
    cfg = load_config()
    presets = cfg.get("ptz_usb_presets", {})
    presets[str(preset)] = {"pan": pan, "tilt": tilt, "zoom": zoom}
    cfg["ptz_usb_presets"] = presets
    save_config(cfg)
    return jsonify({"ok": True, "preset": preset, "pan": pan, "tilt": tilt, "zoom": zoom})


@app.route("/api/ptz/usb/preset/recall", methods=["POST"])
def api_ptz_usb_preset_recall():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    preset = str(int(d.get("preset", 0)))
    cfg = load_config()
    presets = cfg.get("ptz_usb_presets", {})
    p = presets.get(preset)
    if not p:
        return jsonify({"error": f"Preset {preset} not stored"}), 400
    _usb_ptz_cmd(device, 'pan_absolute', p["pan"])
    _usb_ptz_cmd(device, 'tilt_absolute', p["tilt"])
    _usb_ptz_cmd(device, 'zoom_absolute', p["zoom"])
    return jsonify({"ok": True, "preset": int(preset), "pan": p["pan"], "tilt": p["tilt"], "zoom": p["zoom"]})


@app.route("/api/ptz/usb/focus", methods=["POST"])
def api_ptz_usb_focus():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    if d.get("auto") is not None:
        _usb_ptz_cmd(device, 'focus_auto', 1 if d["auto"] else 0)
        return jsonify({"ok": True, "auto": d["auto"]})
    if "focus" in d:
        # Disable auto-focus first, then set manual
        _usb_ptz_cmd(device, 'focus_auto', 0)
        _usb_ptz_cmd(device, 'focus_absolute', int(d["focus"]))
        return jsonify({"ok": True, "focus": int(d["focus"])})
    return jsonify({"error": "focus or auto required"}), 400


@app.route("/api/ptz/usb/focus/get", methods=["POST"])
def api_ptz_usb_focus_get():
    d = request.json or {}
    device = d.get("device", "/dev/video0")
    return jsonify({
        "focus": _usb_ptz_get(device, 'focus_absolute'),
        "auto": _usb_ptz_get(device, 'focus_auto'),
    })


@app.route("/api/ptz/ndi/connect", methods=["POST"])
def api_ptz_ndi_connect():
    d = request.json or {}
    source_name = d.get("source_name", "")
    if not source_name:
        return jsonify({"error": "source_name required"}), 400
    recv = _ndi_ptz_connect(source_name)
    if not recv:
        return jsonify({"error": f"Cannot connect to {source_name}"}), 400
    supported = ndi.recv_ptz_is_supported(recv)
    return jsonify({"ok": True, "supported": supported, "source": source_name})


@app.route("/api/ptz/ndi/disconnect", methods=["POST"])
def api_ptz_ndi_disconnect():
    _ndi_ptz_disconnect()
    return jsonify({"ok": True})


@app.route("/api/ptz/ndi/move", methods=["POST"])
def api_ptz_ndi_move():
    d = request.json or {}
    with _ptz_lock:
        if not _ptz_receiver: return jsonify({"error": "Not connected"}), 400
        mode = d.get("mode", "speed")
        if mode == "speed":
            ndi.recv_ptz_pan_tilt_speed(_ptz_receiver, d.get("pan_speed", 0.0), d.get("tilt_speed", 0.0))
        else:
            ndi.recv_ptz_pan_tilt(_ptz_receiver, d.get("pan", 0.0), d.get("tilt", 0.0))
    return jsonify({"ok": True})


@app.route("/api/ptz/ndi/zoom", methods=["POST"])
def api_ptz_ndi_zoom():
    d = request.json or {}
    with _ptz_lock:
        if not _ptz_receiver: return jsonify({"error": "Not connected"}), 400
        ndi.recv_ptz_zoom(_ptz_receiver, d.get("zoom", 0.0))
    return jsonify({"ok": True})


@app.route("/api/ptz/ndi/focus", methods=["POST"])
def api_ptz_ndi_focus():
    d = request.json or {}
    with _ptz_lock:
        if not _ptz_receiver: return jsonify({"error": "Not connected"}), 400
        if d.get("auto", False):
            ndi.recv_ptz_auto_focus(_ptz_receiver)
        elif "focus" in d:
            ndi.recv_ptz_focus(_ptz_receiver, d["focus"])
        elif "speed" in d:
            ndi.recv_ptz_focus_speed(_ptz_receiver, d["speed"])
    return jsonify({"ok": True})


@app.route("/api/ptz/ndi/preset/store", methods=["POST"])
def api_ptz_ndi_preset_store():
    d = request.json or {}
    with _ptz_lock:
        if not _ptz_receiver: return jsonify({"error": "Not connected"}), 400
        ndi.recv_ptz_store_preset(_ptz_receiver, d.get("preset", 0))
    return jsonify({"ok": True})


@app.route("/api/ptz/ndi/preset/recall", methods=["POST"])
def api_ptz_ndi_preset_recall():
    d = request.json or {}
    with _ptz_lock:
        if not _ptz_receiver: return jsonify({"error": "Not connected"}), 400
        ndi.recv_ptz_recall_preset(_ptz_receiver, d.get("preset", 0), d.get("speed", 1.0))
    return jsonify({"ok": True})

@app.route("/api/hdmi/outputs")
def api_hdmi_outputs(): return jsonify({"outputs": _get_hdmi_outputs()})

@app.route("/api/hdmi/start", methods=["POST"])
def api_hdmi_start():
    d = request.json or {}; sn = d.get("source_name", "")
    if not sn: return jsonify({"error": "source_name required"}), 400
    return jsonify(_hdmi_start(sn, int(d.get("hdmi_index", 0)), int(d.get("width", 0)), int(d.get("height", 0)), d.get("mode", "auto")))

@app.route("/api/hdmi/stop", methods=["POST"])
def api_hdmi_stop():
    return jsonify(_hdmi_stop(int((request.json or {}).get("hdmi_index", 0))))

@app.route("/api/multiview/start", methods=["POST"])
def api_multiview_start():
    d = request.json or {}
    layout_data = d.get("layout_data")
    sources = d.get("sources", [])
    return jsonify(_multiview_start(
        sources=sources, layout=d.get("layout", "2x2"), tally=d.get("tally", {}),
        labels=d.get("labels", True), mode=d.get("mode", "auto"),
        output_res=d.get("output_res", ""), layout_data=layout_data))

@app.route("/api/multiview/stop", methods=["POST"])
def api_multiview_stop():
    return jsonify(_multiview_stop())

@app.route("/api/multiview/tally", methods=["POST"])
def api_multiview_tally():
    d = request.json or {}
    tally = d.get("tally", {})
    # Convert string keys to int
    tally = {int(k): v for k, v in tally.items()}
    return jsonify(_multiview_update_tally(tally))

@app.route("/api/images/upload", methods=["POST"])
def api_image_upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({"error": "No filename"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return jsonify({"error": f"Invalid image type: {ext}"}), 400
    filename = secure_filename(f.filename)
    # Avoid collisions
    base, ext = os.path.splitext(filename)
    path = os.path.join(IMAGES_DIR, filename)
    counter = 1
    while os.path.exists(path):
        filename = f"{base}_{counter}{ext}"
        path = os.path.join(IMAGES_DIR, filename)
        counter += 1
    f.save(path)
    return jsonify({"filename": filename, "path": f"image://{filename}"})

@app.route("/api/images/list")
def api_image_list():
    files = []
    if os.path.isdir(IMAGES_DIR):
        for fn in sorted(os.listdir(IMAGES_DIR)):
            ext = os.path.splitext(fn)[1].lower()
            if ext in ALLOWED_IMAGE_EXT:
                files.append(fn)
    return jsonify({"images": files})

@app.route("/api/images/delete", methods=["POST"])
def api_image_delete():
    d = request.json or {}
    filename = d.get("filename", "")
    if not filename:
        return jsonify({"error": "No filename"}), 400
    path = os.path.join(IMAGES_DIR, secure_filename(filename))
    if os.path.exists(path):
        os.remove(path)
        return jsonify({"deleted": filename})
    return jsonify({"error": "File not found"}), 404

@app.route("/api/multiview/autostart/enable", methods=["POST"])
def api_mv_autostart_enable():
    d = request.json or {}
    sources = d.get("sources", [])
    layout_data = d.get("layout_data")
    # Validate: need either sources or layout_data with sources
    if not sources and not layout_data:
        return jsonify({"error": "No sources or layout provided"}), 400
    return jsonify(_mv_autostart_enable(
        sources=sources, layout=d.get("layout", "2x2"),
        tally=d.get("tally", {}), labels=d.get("labels", True),
        output_res=d.get("output_res", ""), layout_data=d.get("layout_data")))

@app.route("/api/multiview/autostart/disable", methods=["POST"])
def api_mv_autostart_disable():
    return jsonify(_mv_autostart_disable())

@app.route("/api/autostart/status")
def api_autostart_status(): return jsonify(_autostart_status())

@app.route("/api/autostart/enable", methods=["POST"])
def api_autostart_enable():
    d = request.json or {}; sn = d.get("source_name", "")
    if not sn: return jsonify({"error": "source_name required"}), 400
    return jsonify(_autostart_enable(int(d.get("hdmi_index", 0)), sn, int(d.get("width", 0)), int(d.get("height", 0)), d.get("mode", "auto")))

@app.route("/api/autostart/disable", methods=["POST"])
def api_autostart_disable():
    return jsonify(_autostart_disable(int((request.json or {}).get("hdmi_index", 0))))


# ═══════════════════════════════════════════════════════════════════════════
# System Settings
# ═══════════════════════════════════════════════════════════════════════════

_BOOT_MODES = {
    "B1": "CLI (no autologin)",
    "B2": "CLI + autologin",
    "B3": "Desktop (no autologin)",
    "B4": "Desktop + autologin",
}

def _get_boot_mode():
    """Detect current boot mode from raspi-config."""
    try:
        # Check if desktop is enabled
        r1 = subprocess.run(["sudo", "raspi-config", "nonint", "get_boot_cli"],
                            capture_output=True, text=True, timeout=5)
        r2 = subprocess.run(["sudo", "raspi-config", "nonint", "get_autologin"],
                            capture_output=True, text=True, timeout=5)
        is_cli = r1.stdout.strip() == "0"
        is_autologin = r2.stdout.strip() == "0"
        if is_cli and is_autologin:
            return "B2"
        elif is_cli and not is_autologin:
            return "B1"
        elif not is_cli and is_autologin:
            return "B4"
        else:
            return "B3"
    except:
        return "B4"  # default assumption

@app.route("/api/system/bootmode", methods=["GET"])
def api_get_bootmode():
    current = _get_boot_mode()
    return jsonify({"current": current, "current_label": _BOOT_MODES.get(current, "Unknown")})

@app.route("/api/system/bootmode", methods=["POST"])
def api_set_bootmode():
    d = request.json or {}
    mode = d.get("mode", "")
    if mode not in _BOOT_MODES:
        return jsonify({"error": f"Invalid mode: {mode}"}), 400
    try:
        r = subprocess.run(["sudo", "raspi-config", "nonint", "do_boot_behaviour", mode],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return jsonify({"error": r.stderr.strip() or "raspi-config failed"})
        return jsonify({"ok": True, "mode": mode, "label": _BOOT_MODES[mode]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/system/reboot", methods=["POST"])
def api_reboot():
    try:
        subprocess.Popen(["sudo", "reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/system/shutdown", methods=["POST"])
def api_shutdown():
    try:
        subprocess.Popen(["sudo", "shutdown", "-h", "now"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_WEBPANEL_SVC = "ndi-webpanel"
_WEBPANEL_SVC_PATH = f"/etc/systemd/system/{_WEBPANEL_SVC}.service"

@app.route("/api/system/webpanel", methods=["GET"])
def api_webpanel_status():
    enabled = False
    if os.path.exists(_WEBPANEL_SVC_PATH):
        try:
            r = subprocess.run(["systemctl", "is-enabled", _WEBPANEL_SVC],
                               capture_output=True, text=True, timeout=5)
            enabled = r.stdout.strip() == "enabled"
        except:
            pass
    return jsonify({"enabled": enabled})

@app.route("/api/system/webpanel", methods=["POST"])
def api_webpanel_set():
    d = request.json or {}
    enable = d.get("enabled", False)

    if enable:
        user = os.environ.get("SUDO_USER", os.environ.get("USER", "pi"))
        home = os.path.expanduser(f"~{user}") if user != "root" else os.path.expanduser("~")
        venv_python = os.path.join(home, "ndi-env", "bin", "python3")
        python_cmd = venv_python if os.path.exists(venv_python) else sys.executable
        content = f"""[Unit]
Description=NDI Web Control Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={SCRIPT_DIR}
Environment=LD_LIBRARY_PATH=/usr/local/lib
ExecStart={python_cmd} {SCRIPT_DIR}/ndi_web.py --port 5000
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
        try:
            r = subprocess.run(["sudo", "tee", _WEBPANEL_SVC_PATH],
                               input=content.encode(), capture_output=True)
            if r.returncode != 0:
                return jsonify({"error": r.stderr.decode()})
            subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
            subprocess.run(["sudo", "systemctl", "enable", _WEBPANEL_SVC], capture_output=True)
            subprocess.run(["sudo", "systemctl", "start", _WEBPANEL_SVC], capture_output=True)
            return jsonify({"enabled": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        try:
            subprocess.run(["sudo", "systemctl", "disable", _WEBPANEL_SVC], capture_output=True)
            subprocess.run(["sudo", "rm", "-f", _WEBPANEL_SVC_PATH], capture_output=True)
            subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
            return jsonify({"enabled": False})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/system/show-cli", methods=["GET"])
def api_show_cli_get():
    cfg = load_config()
    return jsonify({"enabled": cfg.get("show_cli", False)})

@app.route("/api/system/show-cli", methods=["POST"])
def api_show_cli_set():
    d = request.json or {}
    enabled = bool(d.get("enabled", False))
    cfg = load_config()
    cfg["show_cli"] = enabled
    save_config(cfg)
    # Apply immediately
    if enabled:
        _screen_restore_cli()
    else:
        _screen_blank()
    return jsonify({"enabled": enabled})


@app.route("/api/system/ndi-extra-ips", methods=["GET"])
def api_ndi_extra_ips_get():
    cfg = load_config()
    return jsonify({"ips": cfg.get("ndi_extra_ips", "")})

@app.route("/api/system/ndi-extra-ips", methods=["POST"])
def api_ndi_extra_ips_set():
    global _ndi_extra_ips
    d = request.json or {}
    ips = d.get("ips", "").strip()
    cfg = load_config()
    cfg["ndi_extra_ips"] = ips
    save_config(cfg)
    _ndi_extra_ips = _get_ndi_extra_ips()
    return jsonify({"ips": ips, "resolved": _ndi_extra_ips or ""})

@app.route("/api/system/ndi-scan", methods=["POST"])
def api_ndi_scan():
    """Scan local subnet for NDI devices via mDNS and return their IPs."""
    found_ips = set()
    try:
        # Use avahi-browse to find all NDI services
        r = subprocess.run(
            ['avahi-browse', '-tpr', '_ndi._tcp'],
            capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.split('\n'):
            # Lines starting with '=' have resolved addresses
            if line.startswith('=') and 'IPv4' in line:
                parts = line.split(';')
                if len(parts) >= 8:
                    ip = parts[7]
                    if ip and not ip.startswith('127.'):
                        found_ips.add(ip)
        # Remove our own IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            my_ip = s.getsockname()[0]
            s.close()
            found_ips.discard(my_ip)
        except:
            pass
    except:
        pass

    # Now check which IPs have sources not already found by standard discovery
    already_known = set()
    with _sources_lock:
        for src in _known_sources:
            url = src.get("url", "")
            if ':' in url:
                already_known.add(url.split(':')[0])

    new_ips = sorted(found_ips - already_known)
    return jsonify({"found": new_ips, "all_ndi_ips": sorted(found_ips)})



# ═══════════════════════════════════════════════════════════════════════════
# Video Wall — worker + controller
# ═══════════════════════════════════════════════════════════════════════════

WALL_STATS_PATH = "/tmp/ndi-wall-stats.json"
WALL_CONFIG_FILE = os.path.join(SCRIPT_DIR, "wall_config.json")
_wall_process = None          # Local ndi_wall.py subprocess
_wall_nodes = {}              # hostname -> {hostname, ip, port, col, row, last_seen, status, stats}
_wall_nodes_lock = threading.Lock()
_wall_avahi_proc = None       # avahi-publish-service subprocess
_wall_running = False         # Is the wall actively running?

def _wall_load_config():
    try:
        with open(WALL_CONFIG_FILE) as f: return json.load(f)
    except: return {"source": "", "cols": 2, "rows": 2, "output_res": "1920x1080",
                    "buffer_frames": 3, "bezel_mm": 0, "nodes": []}

def _wall_save_config(cfg):
    try:
        with open(WALL_CONFIG_FILE, "w") as f: json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[wall] Config save error: {e}")


# ── mDNS registration ──

def _wall_mdns_register():
    """Register this Pi as a wall node via avahi."""
    global _wall_avahi_proc
    if _wall_avahi_proc:
        return
    try:
        _wall_avahi_proc = subprocess.Popen(
            ["avahi-publish-service", f"{HOSTNAME}-ndi-wall", "_ndi-wall._tcp", "5000",
             f"hostname={HOSTNAME}", "version=2.0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[wall] mDNS registered: {HOSTNAME}-ndi-wall._ndi-wall._tcp")
    except Exception as e:
        print(f"[wall] mDNS registration failed: {e}")

def _wall_mdns_unregister():
    global _wall_avahi_proc
    if _wall_avahi_proc:
        try: _wall_avahi_proc.terminate()
        except: pass
        _wall_avahi_proc = None


# ── mDNS discovery ──

def _wall_discover_nodes():
    """Discover wall nodes via avahi-browse. Returns list of {hostname, ip, port}."""
    nodes = []
    try:
        result = subprocess.run(
            ["avahi-browse", "-tpr", "_ndi-wall._tcp"],
            capture_output=True, text=True, timeout=6)
        for line in result.stdout.strip().split("\n"):
            if not line.startswith("="):
                continue
            parts = line.split(";")
            if len(parts) >= 9:
                hostname = parts[6].replace(".local", "")
                ip = parts[7]
                port = int(parts[8]) if parts[8].isdigit() else 5000
                if ip and not ip.startswith("169.254"):  # skip link-local
                    nodes.append({"hostname": hostname, "ip": ip, "port": port})
    except Exception as e:
        print(f"[wall] mDNS discovery error: {e}")
    return nodes


# ── Local wall worker process ──

def _wall_worker_start(source, cols, rows, col, row, output_res="", buffer_frames=3, bezel_mm=0, target_fps=30):
    """Start ndi_wall.py locally."""
    global _wall_process

    _wall_worker_stop()  # Kill any existing

    user = os.environ.get("SUDO_USER", os.environ.get("USER", "pi"))
    home = os.path.expanduser(f"~{user}") if user != "root" else os.path.expanduser("~")
    venv_python = os.path.join(home, "ndi-env", "bin", "python3")
    python_cmd = venv_python if os.path.exists(venv_python) else sys.executable
    script = os.path.join(SCRIPT_DIR, "ndi_wall.py")

    cmd = [python_cmd, script,
           "--source", source,
           "--cols", str(cols), "--rows", str(rows),
           "--col", str(col), "--row", str(row),
           "--buffer-frames", str(buffer_frames),
           "--target-fps", str(target_fps)]

    if output_res:
        cmd += ["--output-res", output_res]
    if bezel_mm > 0:
        cmd += ["--bezel-mm", str(bezel_mm)]

    child_env = os.environ.copy()
    child_env.setdefault("LD_LIBRARY_PATH", "/usr/local/lib")

    try:
        _wall_process = subprocess.Popen(
            cmd, stdout=None, stderr=None,
            cwd=SCRIPT_DIR, preexec_fn=os.setsid, env=child_env)
        print(f"[wall] Worker started: PID {_wall_process.pid}, "
              f"source={source}, grid={cols}x{rows}, pos=[{col},{row}]")
        return {"ok": True, "pid": _wall_process.pid}
    except Exception as e:
        return {"error": str(e)}

def _wall_worker_stop():
    """Stop local ndi_wall.py."""
    global _wall_process

    if _wall_process:
        try:
            pgid = os.getpgid(_wall_process.pid)
            os.killpg(pgid, signal.SIGKILL)
        except: pass
        try: _wall_process.kill()
        except: pass
        try: _wall_process.wait(timeout=2)
        except: pass

    _wall_process = None

    try:
        subprocess.run(["pkill", "-9", "-f", "ndi_wall.py"],
                       capture_output=True, timeout=3)
    except: pass

    try: os.remove(WALL_STATS_PATH)
    except: pass

    _post_stop_screen()
    return {"ok": True}

def _wall_worker_status():
    """Get local wall worker status."""
    is_running = _wall_process is not None and _wall_process.poll() is None
    stats = {}
    if is_running:
        try:
            with open(WALL_STATS_PATH) as f:
                stats = json.load(f)
        except: pass
    return {"running": is_running, "stats": stats}


# ── Controller: push commands to remote workers ──

def _wall_push_to_node(ip, port, endpoint, data=None, timeout=5):
    """Send HTTP request to a remote wall worker. Always POST for action endpoints."""
    import urllib.request
    url = f"http://{ip}:{port}/api/wall/worker/{endpoint}"
    try:
        if data is not None:
            body = json.dumps(data).encode()
        elif endpoint in ("start", "stop"):
            body = b"{}"  # POST with empty body for action endpoints
        else:
            body = None   # GET for status
        method = "POST" if body is not None else "GET"
        headers = {"Content-Type": "application/json"} if body else {}
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def _wall_start_all():
    """Controller: start wall on all assigned nodes."""
    global _wall_running
    cfg = _wall_load_config()
    source = cfg.get("source", "")
    if not source:
        return {"error": "No NDI source configured"}

    cols = cfg.get("cols", 2)
    rows = cfg.get("rows", 2)
    output_res = cfg.get("output_res", "")
    buffer_frames = cfg.get("buffer_frames", 3)
    bezel_mm = cfg.get("bezel_mm", 0)
    target_fps = cfg.get("target_fps", 30)
    nodes = cfg.get("nodes", [])

    if not nodes:
        return {"error": "No nodes assigned to grid positions"}

    results = {}
    for node in nodes:
        hostname = node.get("hostname", "")
        node_col = node.get("col", 0)
        node_row = node.get("row", 0)

        start_data = {
            "source": source, "cols": cols, "rows": rows,
            "col": node_col, "row": node_row,
            "output_res": output_res, "buffer_frames": buffer_frames,
            "bezel_mm": bezel_mm, "target_fps": target_fps
        }

        # Is this the local node?
        if hostname == HOSTNAME or hostname == HOSTNAME + ".local":
            result = _wall_worker_start(source, cols, rows, node_col, node_row,
                                        output_res, buffer_frames, bezel_mm, target_fps)
        else:
            # Find IP for this hostname
            ip = node.get("ip", "")
            port = node.get("port", 5000)
            if not ip:
                # Try resolving
                try:
                    import socket as _sock
                    ip = _sock.gethostbyname(hostname + ".local" if "." not in hostname else hostname)
                except:
                    result = {"error": f"Cannot resolve {hostname}"}
                    results[hostname] = result
                    continue
            result = _wall_push_to_node(ip, port, "start", start_data)

        results[hostname] = result
        print(f"[wall] Start {hostname} [{node_col},{node_row}]: {result}")

    _wall_running = True
    return {"ok": True, "results": results}

def _wall_stop_all():
    """Controller: stop wall on all nodes."""
    global _wall_running
    cfg = _wall_load_config()
    nodes = cfg.get("nodes", [])

    results = {}
    for node in nodes:
        hostname = node.get("hostname", "")
        if hostname == HOSTNAME or hostname == HOSTNAME + ".local":
            result = _wall_worker_stop()
        else:
            ip = node.get("ip", "")
            port = node.get("port", 5000)
            if ip:
                result = _wall_push_to_node(ip, port, "stop")
            else:
                result = {"error": "No IP"}
        results[hostname] = result

    # Also stop local process just in case
    _wall_worker_stop()
    _wall_running = False
    return {"ok": True, "results": results}

def _wall_poll_nodes():
    """Poll all known nodes for their status. Called periodically."""
    cfg = _wall_load_config()
    nodes = cfg.get("nodes", [])
    now = time.time()

    for node in nodes:
        hostname = node.get("hostname", "")
        ip = node.get("ip", "")
        port = node.get("port", 5000)

        if hostname == HOSTNAME or hostname == HOSTNAME + ".local":
            status = _wall_worker_status()
            with _wall_nodes_lock:
                _wall_nodes[hostname] = {
                    "hostname": hostname, "ip": "127.0.0.1", "port": 5000,
                    "col": node.get("col"), "row": node.get("row"),
                    "last_seen": now, "online": True,
                    "running": status.get("running", False),
                    "stats": status.get("stats", {}),
                    "is_self": True
                }
        elif ip:
            status = _wall_push_to_node(ip, port, "status")
            online = "error" not in status
            with _wall_nodes_lock:
                _wall_nodes[hostname] = {
                    "hostname": hostname, "ip": ip, "port": port,
                    "col": node.get("col"), "row": node.get("row"),
                    "last_seen": now if online else _wall_nodes.get(hostname, {}).get("last_seen", 0),
                    "online": online,
                    "running": status.get("running", False) if online else False,
                    "stats": status.get("stats", {}) if online else {},
                    "is_self": False
                }


# ── API endpoints: Worker ──

@app.route("/api/wall/worker/status")
def api_wall_worker_status():
    return jsonify(_wall_worker_status())

@app.route("/api/wall/worker/start", methods=["POST"])
def api_wall_worker_start():
    d = request.json or {}
    result = _wall_worker_start(
        source=d.get("source", ""),
        cols=d.get("cols", 2), rows=d.get("rows", 2),
        col=d.get("col", 0), row=d.get("row", 0),
        output_res=d.get("output_res", ""),
        buffer_frames=d.get("buffer_frames", 3),
        bezel_mm=d.get("bezel_mm", 0),
        target_fps=d.get("target_fps", 30))
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route("/api/wall/worker/stop", methods=["POST"])
def api_wall_worker_stop():
    return jsonify(_wall_worker_stop())


# ── API endpoints: Controller ──

@app.route("/api/wall/config", methods=["GET"])
def api_wall_config_get():
    return jsonify(_wall_load_config())

@app.route("/api/wall/config", methods=["POST"])
def api_wall_config_set():
    d = request.json or {}
    cfg = _wall_load_config()
    for key in ("source", "cols", "rows", "output_res", "buffer_frames", "bezel_mm", "target_fps", "nodes"):
        if key in d:
            cfg[key] = d[key]
    _wall_save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/wall/start", methods=["POST"])
def api_wall_start():
    result = _wall_start_all()
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)

@app.route("/api/wall/stop", methods=["POST"])
def api_wall_stop():
    return jsonify(_wall_stop_all())

@app.route("/api/wall/discover", methods=["POST"])
def api_wall_discover():
    """Discover wall nodes via mDNS."""
    nodes = _wall_discover_nodes()
    # Merge discovered nodes into known nodes
    now = time.time()
    with _wall_nodes_lock:
        for n in nodes:
            hn = n["hostname"]
            if hn not in _wall_nodes:
                _wall_nodes[hn] = {
                    "hostname": hn, "ip": n["ip"], "port": n["port"],
                    "col": None, "row": None,
                    "last_seen": now, "online": True,
                    "running": False, "stats": {}, "is_self": hn == HOSTNAME
                }
            else:
                _wall_nodes[hn]["ip"] = n["ip"]
                _wall_nodes[hn]["port"] = n["port"]
                _wall_nodes[hn]["last_seen"] = now
                _wall_nodes[hn]["online"] = True
    return jsonify({"nodes": nodes})

@app.route("/api/wall/nodes")
def api_wall_nodes():
    with _wall_nodes_lock:
        return jsonify({"nodes": list(_wall_nodes.values())})

@app.route("/api/wall/status")
def api_wall_status():
    """Aggregate wall status — polls local + remote workers."""
    cfg = _wall_load_config()
    config_nodes = cfg.get("nodes", [])

    # Always update local worker status
    local_status = _wall_worker_status()
    now = time.time()

    with _wall_nodes_lock:
        # Ensure config nodes exist in _wall_nodes
        for cn in config_nodes:
            hn = cn.get("hostname", "")
            if hn and hn not in _wall_nodes:
                _wall_nodes[hn] = {
                    "hostname": hn, "ip": cn.get("ip", ""), "port": cn.get("port", 5000),
                    "col": cn.get("col"), "row": cn.get("row"),
                    "last_seen": 0, "online": False,
                    "running": False, "stats": {}, "is_self": hn == HOSTNAME
                }
            elif hn in _wall_nodes:
                _wall_nodes[hn]["col"] = cn.get("col")
                _wall_nodes[hn]["row"] = cn.get("row")

        # Update self node
        for hn, node in _wall_nodes.items():
            if hn == HOSTNAME or node.get("is_self"):
                node["online"] = True
                node["running"] = local_status.get("running", False)
                node["stats"] = local_status.get("stats", {})
                node["last_seen"] = now

        # Collect remote nodes to poll (release lock before HTTP calls)
        remote_to_poll = []
        for hn, node in _wall_nodes.items():
            if not node.get("is_self") and hn != HOSTNAME and node.get("ip"):
                remote_to_poll.append((hn, node.get("ip"), node.get("port", 5000)))

    # Poll remote nodes outside lock (short timeout)
    remote_results = {}
    for hn, ip, port in remote_to_poll:
        remote = _wall_push_to_node(ip, port, "status", timeout=2)
        remote_results[hn] = remote

    # Apply results under lock
    with _wall_nodes_lock:
        for hn, remote in remote_results.items():
            if hn in _wall_nodes:
                if "error" not in remote:
                    _wall_nodes[hn]["online"] = True
                    _wall_nodes[hn]["running"] = remote.get("running", False)
                    _wall_nodes[hn]["stats"] = remote.get("stats", {})
                    _wall_nodes[hn]["last_seen"] = now
                else:
                    _wall_nodes[hn]["online"] = False

        nodes = list(_wall_nodes.values())

    assigned = [n for n in nodes if n.get("col") is not None]
    running = [n for n in assigned if n.get("running")]

    return jsonify({
        "config": cfg,
        "wall_running": _wall_running,
        "nodes": nodes,
        "assigned_count": len(assigned),
        "running_count": len(running),
        "total_discovered": len(nodes)
    })

@app.route("/api/wall/node/add", methods=["POST"])
def api_wall_node_add():
    """Manually add a node by hostname or IP."""
    d = request.json or {}
    hostname = d.get("hostname", "").strip()
    ip = d.get("ip", "").strip()
    if not hostname and not ip:
        return jsonify({"error": "Provide hostname or IP"}), 400

    # Try to resolve
    if not ip and hostname:
        try:
            import socket as _sock
            target = hostname + ".local" if "." not in hostname else hostname
            ip = _sock.gethostbyname(target)
        except:
            return jsonify({"error": f"Cannot resolve {hostname}"}), 400

    if not hostname and ip:
        hostname = ip

    port = d.get("port", 5000)

    # Test connectivity
    status = _wall_push_to_node(ip, port, "status")
    online = "error" not in status

    with _wall_nodes_lock:
        _wall_nodes[hostname] = {
            "hostname": hostname, "ip": ip, "port": port,
            "col": None, "row": None,
            "last_seen": time.time(), "online": online,
            "running": status.get("running", False) if online else False,
            "stats": {}, "is_self": hostname == HOSTNAME
        }

    return jsonify({"ok": True, "hostname": hostname, "ip": ip, "online": online})


# ── Register mDNS on startup ──
_wall_mdns_register()


@app.route("/api/benchmark", methods=["POST"])
def api_benchmark():
    """Run NDI receive benchmark — measures FPS, frame timing, bandwidth."""
    d = request.json or {}
    source_name = d.get("source_name", "")
    num_frames = min(int(d.get("num_frames", 200)), 2000)
    timeout_ms = int(d.get("timeout_ms", 50))

    if not source_name:
        return jsonify({"error": "No source name provided"}), 400

    log_lines = []
    def log(msg):
        log_lines.append(msg)
        print(f"[bench] {msg}")

    try:
        log(f"Searching for '{source_name}'...")
        sources = ndi.discover_sources(timeout_ms=3000, extra_ips=_ndi_extra_ips)
        target = None
        for s in sources:
            if source_name.lower() in s["name"].lower():
                target = s
                break
        if not target:
            return jsonify({"error": f"Source '{source_name}' not found", "log": "\n".join(log_lines)})

        log(f"Found: {target['name']}")
        receiver = ndi.recv_create(source_dict=target)
        if not receiver:
            return jsonify({"error": "Failed to create receiver", "log": "\n".join(log_lines)})
        ndi.recv_connect(receiver, target)

        # Warm up — wait for first frame
        log("Waiting for first frame...")
        warm_start = time.monotonic()
        got_first = False
        while time.monotonic() - warm_start < 10:
            ft, frame, w, h = ndi.recv_capture(receiver, timeout_ms=200)
            if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
                got_first = True
                log(f"First frame: {w}x{h}")
                break

        if not got_first:
            ndi.recv_destroy(receiver)
            return jsonify({"error": "No video frames received (10s timeout)", "log": "\n".join(log_lines)})

        # Benchmark
        log(f"Capturing {num_frames} frames...")
        frame_times = []
        missed = 0
        total_bytes = 0
        cap_w, cap_h = 0, 0
        start = time.monotonic()
        last_frame_time = start

        for i in range(num_frames * 3):  # allow 3x attempts for missed frames
            if len(frame_times) >= num_frames:
                break
            ft, frame, w, h = ndi.recv_capture(receiver, timeout_ms=timeout_ms)
            now = time.monotonic()
            if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
                cap_w, cap_h = w, h
                frame_times.append(now - last_frame_time)
                total_bytes += w * h * 4  # BGRA
                last_frame_time = now
            elif ft == ndi.FRAME_TYPE_NONE:
                missed += 1

        elapsed = time.monotonic() - start
        ndi.recv_destroy(receiver)

        captured = len(frame_times)
        if captured < 2:
            return jsonify({"error": "Not enough frames captured", "log": "\n".join(log_lines)})

        avg_fps = captured / elapsed
        avg_frame_ms = (sum(frame_times[1:]) / (captured - 1)) * 1000  # skip first
        min_frame_ms = min(frame_times[1:]) * 1000
        max_frame_ms = max(frame_times[1:]) * 1000
        data_rate_mbs = total_bytes / elapsed / (1024 * 1024)
        bandwidth_mbps = data_rate_mbs * 8

        # Frame time consistency (jitter)
        import statistics
        stdev_ms = statistics.stdev(frame_times[1:]) * 1000 if captured > 2 else 0

        log(f"Captured: {captured} frames in {elapsed:.2f}s")
        log(f"Avg FPS: {avg_fps:.1f}")
        log(f"Frame time: {avg_frame_ms:.1f}ms avg, {min_frame_ms:.1f}ms min, {max_frame_ms:.1f}ms max")
        log(f"Jitter (stdev): {stdev_ms:.1f}ms")
        log(f"Data rate: {data_rate_mbs:.1f} MB/s ({bandwidth_mbps:.1f} Mbps)")
        log(f"Missed captures: {missed}")
        log(f"Resolution: {cap_w}x{cap_h}")

        return jsonify({
            "avg_fps": round(avg_fps, 1),
            "avg_frame_ms": round(avg_frame_ms, 1),
            "min_frame_ms": round(min_frame_ms, 1),
            "max_frame_ms": round(max_frame_ms, 1),
            "jitter_ms": round(stdev_ms, 1),
            "width": cap_w,
            "height": cap_h,
            "frames_captured": captured,
            "missed_frames": missed,
            "elapsed_s": round(elapsed, 2),
            "data_rate_mbs": round(data_rate_mbs, 1),
            "bandwidth_mbps": round(bandwidth_mbps, 1),
            "total_bytes": total_bytes,
            "log": "\n".join(log_lines),
        })

    except Exception as e:
        log(f"Error: {e}")
        return jsonify({"error": str(e), "log": "\n".join(log_lines)}), 500


# ═══════════════════════════════════════════════════════════════════════════
# Viewport layout persistence
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/viewport", methods=["GET"])
def api_viewport_get():
    cfg = load_config()
    return jsonify(cfg.get("viewport", {}))

@app.route("/api/viewport", methods=["POST"])
def api_viewport_save():
    data = request.json or {}
    cfg = load_config()
    cfg["viewport"] = data
    save_config(cfg)
    return jsonify({"ok": True})


def main():
    p = argparse.ArgumentParser(); p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000); p.add_argument("--debug", action="store_true")
    a = p.parse_args()
    if not ndi.initialize(): print("[ERR] NDI init failed."); sys.exit(1)
    ver = "unknown"
    try: ver = ndi.version()
    except: pass
    print(f"\n  NDI Web Control Panel (NDI SDK {ver})")
    print(f"  http://{HOSTNAME}.local:{a.port}\n")
    # Start background NDI source finder
    threading.Thread(target=_source_finder_loop, daemon=True).start()
    app.run(host=a.host, port=a.port, debug=a.debug, threaded=True)

if __name__ == "__main__": main()
