#!/usr/bin/env python3
"""
ndi_camera.py — Send camera video as NDI (uncompressed)

Supports USB cameras, CSI HDMI capture adapters (TC358743), and Pi Camera.
Send camera video as NDI (uncompressed).
"""

import argparse, ctypes, os, signal, subprocess, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndi_ctypes as ndi

running = True
def _sig(s, f):
    global running; running = False
signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


# ═══════════════════════════════════════════════════════════════════════════
# Camera Detection
# ═══════════════════════════════════════════════════════════════════════════

def detect_cameras():
    """Detect available V4L2 cameras, their types and supported formats."""
    cameras = []
    try:
        r = subprocess.run(['v4l2-ctl', '--list-devices'], capture_output=True, text=True, timeout=5)
    except:
        return cameras

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
                # Skip Pi internal devices
                if any(skip in cam_name.lower() for skip in ['bcm2835', 'rpi-hevc', 'pispbe']):
                    continue
                dev = devices[0]
                cam_type = 'csi' if any(x in cam_name.lower() for x in ['unicam', 'rp1-cfe']) else 'usb'
                formats = _get_v4l2_formats(dev)
                cameras.append({
                    'name': cam_name,
                    'device': dev,
                    'type': cam_type,
                    'formats': formats,
                })
        else:
            i += 1
    return cameras


def _get_v4l2_formats(device):
    """Get supported pixel formats for a V4L2 device."""
    formats = []
    try:
        r = subprocess.run(['v4l2-ctl', '-d', device, '--list-formats'],
                           capture_output=True, text=True, timeout=5)
        fmt_map = {
            'YUYV': 'yuyv', 'MJPG': 'mjpeg', 'NV12': 'nv12',
            'H264': 'h264', 'BGR3': 'bgr24', 'RGB3': 'rgb24',
            'UYVY': 'uyvy',
        }
        for line in r.stdout.split('\n'):
            for code, name in fmt_map.items():
                if f"'{code}'" in line and name not in formats:
                    formats.append(name)
    except:
        pass
    return formats


# ═══════════════════════════════════════════════════════════════════════════
# CSI HDMI capture helpers (TC358743 etc.)
# ═══════════════════════════════════════════════════════════════════════════

def _setup_csi_hdmi(device):
    """Set EDID and apply DV timings for HDMI capture adapter."""
    print(f"[cam] Setting up CSI HDMI on {device}...")
    subprocess.run(
        ['v4l2-ctl', '-d', device, '--set-edid=pad=0,type=hdmi'],
        capture_output=True, timeout=5
    )
    time.sleep(2)
    subprocess.run(
        ['v4l2-ctl', '-d', device, '--set-dv-bt-timings', 'query'],
        capture_output=True, timeout=5
    )
    time.sleep(1)
    print(f"[cam] CSI HDMI setup complete")


def _get_dv_resolution(device):
    """Query DV timings to get actual HDMI input resolution."""
    try:
        r = subprocess.run(['v4l2-ctl', '-d', device, '--query-dv-timings'],
                           capture_output=True, text=True, timeout=5)
        w = h = fps = None
        for line in r.stdout.split('\n'):
            if 'Active width:' in line:
                w = int(line.split(':')[1].strip())
            elif 'Active height:' in line:
                h = int(line.split(':')[1].strip())
            elif 'Total height:' in line:
                total_h = int(line.split(':')[1].strip())
            elif 'Pixelclock:' in line:
                try:
                    pc = int(line.split(':')[1].strip().split()[0])
                    total_w_line = [l for l in r.stdout.split('\n') if 'Total width:' in l]
                    total_h_line = [l for l in r.stdout.split('\n') if 'Total height:' in l]
                    if total_w_line and total_h_line:
                        tw = int(total_w_line[0].split(':')[1].strip())
                        th = int(total_h_line[0].split(':')[1].strip())
                        if tw > 0 and th > 0:
                            fps = round(pc / (tw * th))
                except:
                    pass
        return w, h, fps
    except:
        return None, None, None


# ═══════════════════════════════════════════════════════════════════════════
# Raw Camera Source
# ═══════════════════════════════════════════════════════════════════════════

class RawCameraSource:
    """Captures frames from camera via cv2.VideoCapture, returns BGRX numpy arrays."""

    def __init__(self, camera, width, height, fps):
        import cv2
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = 0

        if camera['type'] == 'csi':
            _setup_csi_hdmi(camera['device'])
            dv_w, dv_h, dv_fps = _get_dv_resolution(camera['device'])
            if dv_w and dv_h:
                width, height = dv_w, dv_h
                if dv_fps:
                    fps = dv_fps
                self.width = width
                self.height = height
                self.fps = fps
                print(f"[cam] HDMI signal detected: {width}x{height}@{fps}fps")

        dev_idx = camera['device']
        if isinstance(dev_idx, str) and dev_idx.startswith('/dev/video'):
            dev_idx = int(dev_idx.replace('/dev/video', ''))

        self.cap = cv2.VideoCapture(dev_idx)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera {camera['device']}")

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)

        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[cam] Raw capture: {actual_w}x{actual_h}@{fps}fps ({camera['device']})")
        self.width = actual_w
        self.height = actual_h

    def get_frame(self):
        """Get next BGRX frame. Returns numpy array (h, w, 4) or None."""
        import cv2
        ret, frame = self.cap.read()
        if not ret:
            return None
        self.frame_count += 1
        if frame.shape[2] == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
        frame[:, :, 3] = 255
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        return frame

    def close(self):
        self.cap.release()


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Send camera as NDI (uncompressed)')
    parser.add_argument('--device', help='V4L2 device (e.g. /dev/video0)')
    parser.add_argument('--name', default=None, help='NDI source name')
    parser.add_argument('--res', default='1920x1080', help='Resolution (e.g. 1920x1080)')
    parser.add_argument('--fps', type=int, default=30, help='Frame rate')
    args = parser.parse_args()

    width, height = [int(x) for x in args.res.split('x')]

    print(f"\n  NDI Camera Sender [Raw (uncompressed)]")
    print(f"  {'─' * 42}\n")

    cameras = detect_cameras()
    if not cameras:
        print("[ERR] No cameras found. Connect a USB or CSI camera.")
        return

    print(f"  Found {len(cameras)} camera(s):")
    for i, cam in enumerate(cameras):
        print(f"    {i}: {cam['name']} ({cam['type']}, {cam['device']})")
        print(f"       Formats: {', '.join(cam['formats'])}")
    print()

    # Select camera
    if args.device:
        cam = next((c for c in cameras if c['device'] == args.device), None)
        if not cam:
            print(f"[ERR] Device {args.device} not found")
            return
    else:
        cam = cameras[0]

    # Initialize NDI
    if not ndi.initialize():
        print("[ERR] NDI init failed")
        return
    ver = ndi.version()
    print(f"[OK] NDI SDK: {ver}")

    source_name = args.name or "Camera"
    sender = ndi.send_create(source_name)
    if not sender:
        print("[ERR] Failed to create NDI sender")
        return

    print(f"[OK] NDI source: '{source_name}'")

    # Start camera
    cam_src = RawCameraSource(cam, width, height, args.fps)
    width, height = cam_src.width, cam_src.height
    fps = cam_src.fps

    start_time = time.monotonic()
    sent_count = 0
    last_report = start_time
    last_report_count = 0

    print(f"[OK] Streaming {width}x{height}@{fps}fps... Ctrl+C to stop\n")

    try:
        while running:
            frame = cam_src.get_frame()
            if frame is None:
                time.sleep(0.001)
                continue

            ndi.send_video_v2(sender, frame, fps_n=fps * 1000, fps_d=1000)
            sent_count += 1

            now = time.monotonic()
            if now - last_report >= 2.0:
                interval = now - last_report
                interval_frames = sent_count - last_report_count
                fps_actual = interval_frames / interval if interval > 0 else 0
                conns = ndi.send_get_no_connections(sender, 0)
                data_size = frame.nbytes
                bw = data_size * interval_frames * 8 / interval / 1_000_000 if interval > 0 else 0
                print(f"  {fps_actual:.1f} fps | {sent_count} frames | "
                      f"conns: {conns} |    | {data_size}B | {bw:.1f}Mbps")
                last_report = now
                last_report_count = sent_count

    except KeyboardInterrupt:
        pass
    finally:
        cam_src.close()
        ndi.send_destroy(sender)
        ndi.destroy()
        print("[OK] Done.")


if __name__ == "__main__":
    main()
