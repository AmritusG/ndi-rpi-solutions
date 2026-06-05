#!/usr/bin/env python3
"""NDI Sender for Raspberry Pi — uses ndi_ctypes (no ndi-python needed)."""

import argparse, os, signal, socket, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndi_ctypes as ndi

running = True
def _sig(s, f): global running; running = False
signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)


class TestPatternSource:
    def __init__(self, w, h, fps):
        self.width, self.height, self.fps, self.n = w, h, fps, 0
        colors = [(192,192,192),(192,192,0),(0,192,192),(0,192,0),(192,0,192),(192,0,0),(0,0,192)]
        self.base = np.zeros((h, w, 4), dtype=np.uint8)
        bw = w // len(colors)
        for i, (r, g, b) in enumerate(colors):
            self.base[:, i*bw:((i+1)*bw if i < len(colors)-1 else w)] = [b, g, r, 255]

    def read(self):
        f = self.base.copy()
        y = int((self.n * 3) % self.height)
        f[y:min(y+4, self.height), :] = [255, 255, 255, 255]
        self.n += 1
        return True, f

    def release(self): pass


class WebcamSource:
    def __init__(self, dev, w, h, fps):
        import cv2; self.cv2 = cv2
        self.cap = cv2.VideoCapture(dev)
        if not self.cap.isOpened(): raise RuntimeError(f"Cannot open webcam {dev}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def read(self):
        ret, f = self.cap.read()
        if ret: f = self.cv2.cvtColor(f, self.cv2.COLOR_BGR2BGRA)
        return ret, f

    def release(self): self.cap.release()


class PiCameraSource:
    def __init__(self, w, h, fps):
        from picamera2 import Picamera2
        self.picam = Picamera2()
        cfg = self.picam.create_video_configuration(
            main={"size": (w, h), "format": "XRGB8888"}, controls={"FrameRate": fps})
        self.picam.configure(cfg); self.picam.start(); time.sleep(1)
        self.width, self.height = w, h

    def read(self):
        try:
            f = self.picam.capture_array()
            if f.shape[2] == 3:
                import cv2; f = cv2.cvtColor(f, cv2.COLOR_BGR2BGRA)
            return True, f
        except: return False, None

    def release(self):
        try: self.picam.stop()
        except: pass


def main():
    p = argparse.ArgumentParser(description="NDI Sender for Raspberry Pi")
    p.add_argument("--source", choices=["test","webcam","picamera"], default="test")
    p.add_argument("--device", type=int, default=0)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--name", type=str, default=None)
    a = p.parse_args()

    if not ndi.initialize():
        print("[ERR] NDI init failed. Run: sudo bash install_ndi_sdk.sh"); sys.exit(1)

    hostname = socket.gethostname()
    name = a.name or f"{hostname}-NDI"

    src = {"test": TestPatternSource, "webcam": WebcamSource, "picamera": PiCameraSource}
    if a.source == "webcam":
        source = src[a.source](a.device, a.width, a.height, a.fps)
    else:
        source = src[a.source](a.width, a.height, a.fps)

    sender = ndi.send_create(name)
    if not sender: print("[ERR] Failed to create sender"); sys.exit(1)

    print(f"[OK] Sending: {name} ({a.width}x{a.height} @ {a.fps}fps, source={a.source})")
    print("[i]  Press Ctrl+C to stop.\n")

    n, start = 0, time.monotonic()
    while running:
        ret, frame = source.read()
        if not ret: time.sleep(0.01); continue
        if frame.shape[1] != a.width or frame.shape[0] != a.height:
            import cv2; frame = cv2.resize(frame, (a.width, a.height))
        if not frame.flags["C_CONTIGUOUS"]: frame = np.ascontiguousarray(frame)
        ndi.send_video_v2(sender, frame, fps_n=a.fps*1000, fps_d=1000)
        n += 1
        if n % 150 == 0:
            fps = n / (time.monotonic() - start)
            conns = ndi.send_get_no_connections(sender, 0)
            print(f"  Frames: {n} | FPS: {fps:.1f} | Viewers: {conns}")

    source.release(); ndi.send_destroy(sender); ndi.destroy()
    print("\n[OK] Sender stopped.")

if __name__ == "__main__": main()
