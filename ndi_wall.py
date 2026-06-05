#!/usr/bin/env python3
"""
ndi_wall.py - Video Wall Worker for Raspberry Pi.

Receives a single NDI source, crops a region based on grid position,
and displays the cropped region full-screen via DRM/framebuffer.

Phase 1: Single-node crop + display with frame buffer for future sync.
Phase 3 will add timecode-based synchronization across multiple nodes.

Usage:
  python3 ndi_wall.py --source "STUDIO (Arena - Comp)" --cols 2 --rows 2 --col 0 --row 0
  python3 ndi_wall.py --source "STUDIO" --cols 3 --rows 3 --col 1 --row 2 --bezel-mm 12

Exit: Ctrl+C or SIGTERM.
"""

import argparse, ctypes, json, os, signal, sys, threading, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndi_ctypes as ndi

running = True
exit_reason = ""
_frame_buf_ref = None  # Set at runtime so signal handler can wake the wait

def _sig(s, f):
    global running, exit_reason
    running = False
    exit_reason = "Ctrl+C" if s == signal.SIGINT else "SIGTERM"
    # Wake the display loop if blocked on Event.wait()
    if _frame_buf_ref is not None:
        _frame_buf_ref._new_frame.set()

signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


# ═══════════════════════════════════════════════════════════════════════════
# Stats (JSON to /tmp for web panel)
# ═══════════════════════════════════════════════════════════════════════════

STATS_PATH = "/tmp/ndi-wall-stats.json"

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
# NDI helpers
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
        print(f"  Scan {attempt + 1}/{max_attempts}...")
    return None


def recv_capture_with_timecode(receiver, vf, af, mf, timeout_ms=50):
    """Receive NDI frame with timecode. Returns (frame_type, frame, w, h, timecode)."""
    ft = ndi._ndi.NDIlib_recv_capture_v2(
        receiver, ctypes.byref(vf), ctypes.byref(af), ctypes.byref(mf), timeout_ms)

    if ft == ndi.FRAME_TYPE_VIDEO and vf.p_data:
        w, h = vf.xres, vf.yres
        timecode = vf.timecode
        stride = vf.line_stride_in_bytes if vf.line_stride_in_bytes > 0 else w * 4
        size = h * stride
        buf = (ctypes.c_uint8 * size).from_address(vf.p_data)
        if stride == w * 4:
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4)).copy()
        else:
            raw = np.frombuffer(buf, dtype=np.uint8).copy().reshape((h, stride))
            frame = raw[:, :w * 4].reshape((h, w, 4))
        ndi._ndi.NDIlib_recv_free_video_v2(receiver, ctypes.byref(vf))
        return ndi.FRAME_TYPE_VIDEO, frame, w, h, timecode

    elif ft == ndi.FRAME_TYPE_AUDIO:
        ndi._ndi.NDIlib_recv_free_audio_v2(receiver, ctypes.byref(af))
        return ndi.FRAME_TYPE_AUDIO, None, 0, 0, 0

    elif ft == ndi.FRAME_TYPE_METADATA:
        ndi._ndi.NDIlib_recv_free_metadata(receiver, ctypes.byref(mf))
        return ndi.FRAME_TYPE_METADATA, None, 0, 0, 0

    return ndi.FRAME_TYPE_NONE, None, 0, 0, 0


# ═══════════════════════════════════════════════════════════════════════════
# Frame ring buffer (prepares for Phase 3 timecode sync)
# ═══════════════════════════════════════════════════════════════════════════

class FrameBuffer:
    """Thread-safe ring buffer for NDI frames with timecodes.

    Phase 1: Display thread just grabs the latest frame.
    Phase 3: Display thread will select frame by target timecode.
    """

    def __init__(self, capacity=3):
        self.capacity = capacity
        self.lock = threading.Lock()
        self.frames = []       # List of (frame, w, h, timecode)
        self.write_count = 0
        self.drop_count = 0
        self._new_frame = threading.Event()

    def push(self, frame, w, h, timecode):
        """Add a frame. Drops oldest if buffer is full."""
        with self.lock:
            if len(self.frames) >= self.capacity:
                self.frames.pop(0)
                self.drop_count += 1
            self.frames.append((frame, w, h, timecode))
            self.write_count += 1
        self._new_frame.set()

    def wait_for_new(self, timeout=0.05):
        """Block until a new frame arrives or timeout. Returns True if new frame available."""
        result = self._new_frame.wait(timeout=timeout)
        self._new_frame.clear()
        return result

    def get_latest(self):
        """Get the most recent frame. Returns (frame, w, h, timecode) or None."""
        with self.lock:
            if not self.frames:
                return None
            return self.frames[-1]

    def get_by_timecode(self, target_tc):
        """Get frame closest to target timecode (for Phase 3 sync).
        Returns (frame, w, h, timecode) or None."""
        with self.lock:
            if not self.frames:
                return None
            best = None
            best_diff = float('inf')
            for entry in self.frames:
                diff = abs(entry[3] - target_tc)
                if diff < best_diff:
                    best_diff = diff
                    best = entry
            return best

    def clear(self):
        with self.lock:
            self.frames.clear()

    @property
    def fill(self):
        with self.lock:
            return len(self.frames)


# ═══════════════════════════════════════════════════════════════════════════
# Crop calculation
# ═══════════════════════════════════════════════════════════════════════════

def calc_crop_region(source_w, source_h, cols, rows, col, row, bezel_px=0):
    """Calculate the crop region for this node's grid position.

    Returns (x, y, w, h) in source pixel coordinates.
    bezel_px: bezel compensation in source pixels (expand crop at shared edges).
    """
    tile_w = source_w / cols
    tile_h = source_h / rows

    x = tile_w * col
    y = tile_h * row
    w = tile_w
    h = tile_h

    # Bezel compensation: expand crop at shared edges
    if bezel_px > 0:
        half_bezel = bezel_px / 2
        if col > 0:
            x -= half_bezel
            w += half_bezel
        if col < cols - 1:
            w += half_bezel
        if row > 0:
            y -= half_bezel
            h += half_bezel
        if row < rows - 1:
            h += half_bezel

    # Clamp to source bounds
    x = max(0, int(x))
    y = max(0, int(y))
    w = min(int(w), source_w - x)
    h = min(int(h), source_h - y)

    return x, y, w, h


# ═══════════════════════════════════════════════════════════════════════════
# Receive thread
# ═══════════════════════════════════════════════════════════════════════════

def receiver_thread(source_name, frame_buf):
    """Continuously receive NDI frames and push to buffer."""
    global running

    vf = ndi.NDI_video_frame_v2_t()
    af = ndi.NDI_audio_frame_v2_t()
    mf = ndi.NDI_metadata_frame_t()

    print(f"[wall] Searching for '{source_name}'...")
    target = find_source(source_name)
    if not target:
        print(f"[ERR] Source '{source_name}' not found")
        running = False
        return

    print(f"[wall] Found: {target['name']}")
    recv = ndi.recv_create(source_dict=target)
    if not recv:
        print("[ERR] Failed to create NDI receiver")
        running = False
        return
    ndi.recv_connect(recv, target)

    frame_count = 0
    start = time.monotonic()
    last_report = start
    last_report_count = 0

    while running:
        try:
            ft, frame, w, h, tc = recv_capture_with_timecode(
                recv, vf, af, mf, timeout_ms=50)
        except Exception:
            time.sleep(0.001)
            continue

        if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
            frame_buf.push(frame, w, h, tc)
            frame_count += 1

            now = time.monotonic()
            if now - last_report >= 5.0:
                report_frames = frame_count - last_report_count
                report_elapsed = now - last_report
                fps = report_frames / report_elapsed if report_elapsed > 0 else 0
                last_report_count = frame_count
                print(f"[recv] {fps:.1f} fps | {frame_count} frames | "
                      f"buf: {frame_buf.fill}/{frame_buf.capacity} | "
                      f"drops: {frame_buf.drop_count} | tc: {tc}")
                last_report = now

    # Cleanup
    try:
        ndi.recv_destroy(recv)
    except:
        pass
    print("[recv] Stopped")


# ═══════════════════════════════════════════════════════════════════════════
# Display loop
# ═══════════════════════════════════════════════════════════════════════════

def run_wall_display(frame_buf, cols, rows, col, row, bezel_px=0,
                     output_w=0, output_h=0, target_fps=30):
    """Main display loop: crop frames from buffer, write to DRM/framebuffer."""
    global running, exit_reason
    import cv2

    # ── Take over VT ──
    try:
        from ndi_hdmi import vt_take_over, vt_restore
        vt_take_over()
    except Exception as e:
        print(f"[i] VT takeover skipped: {e}")

    # ── Open display (DRM preferred) ──
    use_drm = False
    disp = None
    try:
        from drm_display import DRMDisplay
        pref_w = output_w if output_w > 0 else 1920
        pref_h = output_h if output_h > 0 else 1080
        disp = DRMDisplay(preferred_w=pref_w, preferred_h=pref_h)
        screen_w, screen_h = disp.width, disp.height
        use_drm = True
        print(f"[OK] Display: DRM 32bpp {screen_w}x{screen_h}")
    except Exception as e:
        print(f"[i] DRM not available ({e}), trying fbdev...")

    fb = None
    if not use_drm:
        try:
            from ndi_hdmi import Framebuffer
            for fb_idx in range(4):
                dev = f"/dev/fb{fb_idx}"
                if os.path.exists(dev):
                    fb = Framebuffer(dev)
                    screen_w, screen_h = fb.xres, fb.yres
                    print(f"[OK] Display: fbdev {screen_w}x{screen_h} ({fb.bpp}bpp)")
                    break
            if not fb:
                print("[ERR] No display found (no DRM, no framebuffer)")
                running = False
                return
        except Exception as e:
            print(f"[ERR] Framebuffer init failed: {e}")
            running = False
            return

    # ── Clear display ──
    if use_drm:
        disp.clear()
    else:
        fb.clear()

    # ── Wait for first frame to know source resolution ──
    print(f"[wall] Waiting for first frame...")
    while running:
        entry = frame_buf.get_latest()
        if entry:
            _, src_w, src_h, _ = entry
            break
        time.sleep(0.01)

    if not running:
        return

    # ── Calculate crop region ──
    crop_x, crop_y, crop_w, crop_h = calc_crop_region(
        src_w, src_h, cols, rows, col, row, bezel_px)

    print(f"[wall] Source: {src_w}x{src_h}")
    print(f"[wall] Grid: {cols}x{rows}, position: col={col} row={row}")
    print(f"[wall] Crop: x={crop_x} y={crop_y} w={crop_w} h={crop_h}")
    print(f"[wall] Output: {screen_w}x{screen_h}")
    if bezel_px > 0:
        print(f"[wall] Bezel compensation: {bezel_px}px")

    # ── Pre-allocate output buffer ──
    output_buf = np.zeros((screen_h, screen_w, 4), dtype=np.uint8)

    # ── Display loop ──
    display_count = 0
    last_tc = 0
    start = time.monotonic()
    last_stats = start
    last_frame_count = frame_buf.write_count
    last_display_time = 0.0
    last_stats_display = 0

    # Use INTER_NEAREST for speed — at 2x upscale it's pixel doubling which is fast.
    # INTER_LINEAR is 5-10x slower and the quality gain is negligible on a video wall.
    interp = cv2.INTER_NEAREST

    # Frame pacing: limit display rate to reduce scanout collision (tearing).
    # NOTE: Some tearing is unavoidable on Pi 5 headless DRM — the vc4 driver doesn't
    # support vsync-aware userspace writes. Lower target_fps = less frequent tearing.
    # For tear-free output, use desktop mode (SDL2 with GPU compositing).
    min_frame_interval = 1.0 / max(1, target_fps)
    print(f"[wall] Display target: {target_fps} fps ({min_frame_interval*1000:.1f}ms interval)")

    while running:
        # Block until receiver pushes a new frame (no CPU spin)
        frame_buf.wait_for_new(timeout=0.05)

        entry = frame_buf.get_latest()
        if entry is None:
            continue

        frame, fw, fh, timecode = entry

        # Skip if same frame (already displayed this one)
        if timecode == last_tc and timecode != 0:
            continue
        last_tc = timecode

        # Frame pacing: don't write faster than target
        now = time.monotonic()
        elapsed_since_last = now - last_display_time
        if elapsed_since_last < min_frame_interval:
            time.sleep(min_frame_interval - elapsed_since_last)
        last_display_time = time.monotonic()

        # ── Source resolution change detection ──
        if fw != src_w or fh != src_h:
            src_w, src_h = fw, fh
            crop_x, crop_y, crop_w, crop_h = calc_crop_region(
                src_w, src_h, cols, rows, col, row, bezel_px)
            print(f"[wall] Source changed to {src_w}x{src_h}, "
                  f"new crop: x={crop_x} y={crop_y} w={crop_w} h={crop_h}")

        # ── Crop (numpy slice — zero copy) ──
        tile = frame[crop_y:crop_y + crop_h, crop_x:crop_x + crop_w]

        # ── Scale to output resolution ──
        if crop_w != screen_w or crop_h != screen_h:
            cv2.resize(tile, (screen_w, screen_h), dst=output_buf,
                       interpolation=interp)
            out = output_buf  # Pre-allocated, always contiguous
        else:
            out = np.ascontiguousarray(tile) if not tile.flags["C_CONTIGUOUS"] else tile

        # ── Write to display ──
        if use_drm:
            disp.write_frame(out)
        else:
            # Premultiply alpha for fbdev (DRM path handles it internally)
            oh, ow = out.shape[:2]
            needs_premul = (out[0, 0, 3] < 255 or out[oh-1, ow-1, 3] < 255 or
                            out[oh//2, ow//2, 3] < 255 or out[0, ow-1, 3] < 255 or
                            out[oh-1, 0, 3] < 255)
            if needs_premul:
                if not hasattr(run_wall_display, '_fb_premul'):
                    run_wall_display._fb_premul = np.empty_like(out)
                    run_wall_display._fb_ch = [np.empty(out.shape[:2], dtype=np.uint8) for _ in range(4)]
                    run_wall_display._fb_ch[3][:] = 255
                b, g, r, a_ch = cv2.split(out)
                cv2.multiply(b, a_ch, dst=run_wall_display._fb_ch[0], scale=1.0/255)
                cv2.multiply(g, a_ch, dst=run_wall_display._fb_ch[1], scale=1.0/255)
                cv2.multiply(r, a_ch, dst=run_wall_display._fb_ch[2], scale=1.0/255)
                cv2.merge(run_wall_display._fb_ch, dst=run_wall_display._fb_premul)
                out = run_wall_display._fb_premul
            bpp = fb.bpp
            ll = fb.line_length
            if bpp == 32:
                for y_line in range(screen_h):
                    off = y_line * ll
                    fb.mm[off:off + screen_w * 4] = out[y_line].data
            elif bpp == 16:
                b = out[:, :, 0].astype(np.uint16)
                g = out[:, :, 1].astype(np.uint16)
                r = out[:, :, 2].astype(np.uint16)
                rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                raw = rgb565.tobytes()
                row_bytes = screen_w * 2
                for y_line in range(screen_h):
                    off = y_line * ll
                    row_start = y_line * row_bytes
                    fb.mm[off:off + row_bytes] = raw[row_start:row_start + row_bytes]

        display_count += 1

        # ── Stats (every second) ──
        now = time.monotonic()
        if now - last_stats >= 1.0:
            stats_elapsed = now - last_stats
            stats_display = display_count - last_stats_display
            display_fps = stats_display / stats_elapsed if stats_elapsed > 0 else 0
            last_stats_display = display_count
            recv_count = frame_buf.write_count
            recv_fps = (recv_count - last_frame_count) / stats_elapsed
            last_frame_count = recv_count

            write_stats({
                "state": "running",
                "source_w": src_w,
                "source_h": src_h,
                "crop": {"x": crop_x, "y": crop_y, "w": crop_w, "h": crop_h},
                "grid": {"cols": cols, "rows": rows, "col": col, "row": row},
                "output": f"{screen_w}x{screen_h}",
                "display_fps": round(display_fps, 1),
                "recv_fps": round(recv_fps, 1),
                "display_frames": display_count,
                "recv_frames": recv_count,
                "buffer_fill": frame_buf.fill,
                "buffer_capacity": frame_buf.capacity,
                "buffer_drops": frame_buf.drop_count,
                "timecode": timecode,
                "bezel_px": bezel_px,
            })
            last_stats = now

            if display_count % 300 < 30:
                print(f"[wall] {display_fps:.1f} fps | "
                      f"recv {recv_fps:.1f} fps | "
                      f"buf {frame_buf.fill}/{frame_buf.capacity} | "
                      f"drops {frame_buf.drop_count}")

    # ── Cleanup ──
    print(f"\n[i] Shutting down ({exit_reason or 'stopped'})...")
    clear_stats()

    if use_drm and disp:
        try:
            disp.clear()
            disp.close()
        except:
            pass
    elif fb:
        try:
            fb.clear()
            fb.close()
        except:
            pass

    try:
        vt_restore()
    except:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global running
    parser = argparse.ArgumentParser(
        description="NDI Video Wall Worker — receive, crop, display",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 2x2 wall, this node is top-left (col=0, row=0)
  python3 ndi_wall.py --source "STUDIO" --cols 2 --rows 2 --col 0 --row 0

  # 3x3 wall, bottom-right corner with bezel compensation
  python3 ndi_wall.py --source "STUDIO" --cols 3 --rows 3 --col 2 --row 2 --bezel-mm 12

  # 2x1 side-by-side, right display
  python3 ndi_wall.py --source "STUDIO" --cols 2 --rows 1 --col 1 --row 0

  # List available NDI sources
  python3 ndi_wall.py --list
""")
    parser.add_argument("--source", help="NDI source name (substring match)")
    parser.add_argument("--cols", type=int, default=2, help="Wall columns (default: 2)")
    parser.add_argument("--rows", type=int, default=2, help="Wall rows (default: 2)")
    parser.add_argument("--col", type=int, default=0, help="This node's column (0-based)")
    parser.add_argument("--row", type=int, default=0, help="This node's row (0-based)")
    parser.add_argument("--output-res", default="",
                        help="Output resolution, e.g. 1920x1080 (default: display native)")
    parser.add_argument("--buffer-frames", type=int, default=3,
                        help="Frame buffer size (default: 3)")
    parser.add_argument("--bezel-mm", type=float, default=0,
                        help="Bezel width in mm for compensation (default: 0 = disabled)")
    parser.add_argument("--physical-width-mm", type=float, default=0,
                        help="Total physical wall width in mm (for bezel calc)")
    parser.add_argument("--target-fps", type=int, default=30,
                        help="Display frame rate cap (default: 30 — reduces tearing)")
    parser.add_argument("--list", action="store_true",
                        help="List available NDI sources and exit")
    args = parser.parse_args()

    # ── Init NDI ──
    if not ndi.initialize():
        print("[ERR] NDI init failed.")
        sys.exit(1)

    try:
        ver = ndi.version()
    except:
        ver = "unknown"
    print(f"\n  NDI Video Wall Worker (NDI SDK {ver})")
    print(f"  ─────────────────────────────────\n")

    # ── List mode ──
    if args.list:
        print("  Scanning for NDI sources (5s)...")
        sources = ndi.discover_sources(timeout_ms=5000)
        if not sources:
            print("  No sources found.")
        else:
            for s in sources:
                print(f"  • {s['name']}")
        ndi.destroy()
        return

    # ── Validate args ──
    if not args.source:
        parser.error("--source is required (use --list to find sources)")

    if args.col < 0 or args.col >= args.cols:
        parser.error(f"--col {args.col} out of range [0, {args.cols - 1}]")
    if args.row < 0 or args.row >= args.rows:
        parser.error(f"--row {args.row} out of range [0, {args.rows - 1}]")

    # ── Parse output resolution ──
    output_w, output_h = 0, 0
    if args.output_res:
        m = args.output_res.lower().split("x")
        if len(m) == 2:
            output_w, output_h = int(m[0]), int(m[1])

    # ── Calculate bezel compensation in pixels ──
    # Bezel in mm → pixels requires knowing total physical wall size and source resolution.
    # For now, we'll compute it once we know the source resolution.
    # Store mm for now, convert after first frame.
    bezel_mm = args.bezel_mm

    print(f"  Source:   {args.source}")
    print(f"  Grid:     {args.cols}x{args.rows}")
    print(f"  Position: col={args.col} row={args.row}")
    print(f"  Buffer:   {args.buffer_frames} frames")
    print(f"  Target:   {args.target_fps} fps")
    if bezel_mm > 0:
        print(f"  Bezel:    {bezel_mm} mm")
    print()

    # ── Create frame buffer ──
    frame_buf = FrameBuffer(capacity=args.buffer_frames)
    global _frame_buf_ref
    _frame_buf_ref = frame_buf

    # ── Start receiver thread ──
    recv_t = threading.Thread(
        target=receiver_thread,
        args=(args.source, frame_buf),
        daemon=True)
    recv_t.start()

    # ── Bezel: we'll convert mm to px after first frame arrives ──
    # For now, pass 0. We'll calculate once source resolution is known.
    bezel_px = 0
    if bezel_mm > 0:
        # We need source resolution to convert mm to px.
        # Wait for first frame, then recalculate.
        print("[wall] Waiting for source to calculate bezel compensation...")
        while running and frame_buf.fill == 0:
            time.sleep(0.01)
        if running:
            entry = frame_buf.get_latest()
            if entry:
                _, src_w, src_h, _ = entry
                # Assume standard 16:9 display dimensions if physical size not given
                # A common 55" display is ~1210mm wide
                if args.physical_width_mm > 0:
                    px_per_mm = src_w * args.cols / args.physical_width_mm
                else:
                    # Estimate: assume each display is ~600mm wide (24")
                    est_wall_mm = 600 * args.cols
                    px_per_mm = src_w / est_wall_mm
                    print(f"[wall] Estimating wall physical width: {est_wall_mm}mm "
                          f"({px_per_mm:.2f} px/mm)")
                bezel_px = int(round(bezel_mm * px_per_mm))
                print(f"[wall] Bezel compensation: {bezel_mm}mm = {bezel_px}px")

    # ── Run display loop (blocking) ──
    try:
        run_wall_display(
            frame_buf, args.cols, args.rows, args.col, args.row,
            bezel_px=bezel_px, output_w=output_w, output_h=output_h,
            target_fps=args.target_fps)
    except KeyboardInterrupt:
        pass

    # ── Cleanup ──
    running = False
    recv_t.join(timeout=2.0)
    ndi.destroy()
    print("[wall] Done.")


if __name__ == "__main__":
    main()
