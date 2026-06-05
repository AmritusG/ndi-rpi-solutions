#!/usr/bin/env python3
"""NDI Receiver for Raspberry Pi — uses ndi_ctypes (no ndi-python needed)."""

import argparse, os, signal, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ndi_ctypes as ndi

running = True
def _sig(s, f): global running; running = False
signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)


def list_sources():
    sources = ndi.discover_sources(timeout_ms=5000)
    if not sources:
        print("[!!] No NDI sources found."); return []
    print(f"\n[OK] Found {len(sources)} source(s):\n")
    for i, s in enumerate(sources):
        print(f"  [{i}] {s['name']}")
    print()
    return sources


def main():
    p = argparse.ArgumentParser(description="NDI Receiver for Raspberry Pi")
    p.add_argument("--list", action="store_true", help="List sources and exit")
    p.add_argument("--source", type=str, default=None, help="Source name (partial match)")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--output", type=str, default=None, help="Save frames to dir")
    p.add_argument("--save-fps", type=int, default=1)
    p.add_argument("--scale", type=float, default=1.0)
    a = p.parse_args()

    if not ndi.initialize():
        print("[ERR] NDI init failed. Run: sudo bash install_ndi_sdk.sh"); sys.exit(1)

    if a.list:
        list_sources(); ndi.destroy(); return

    sources = list_sources()
    if not sources: ndi.destroy(); sys.exit(1)

    # Select source
    target = None
    if a.source:
        for s in sources:
            if a.source.lower() in s["name"].lower():
                target = s; break
        if not target:
            print(f"[ERR] Source '{a.source}' not found."); ndi.destroy(); sys.exit(1)
    elif len(sources) == 1:
        target = sources[0]
    else:
        while True:
            try:
                idx = int(input(f"Select source [0-{len(sources)-1}]: "))
                if 0 <= idx < len(sources): target = sources[idx]; break
            except (ValueError, EOFError): pass

    print(f"[OK] Connecting to: {target['name']}")

    receiver = ndi.recv_create(source_dict=target)
    if not receiver: print("[ERR] Failed to create receiver"); sys.exit(1)
    ndi.recv_connect(receiver, target)

    if a.headless:
        _receive_headless(receiver, a)
    else:
        _receive_display(receiver, target, a)

    ndi.recv_destroy(receiver); ndi.destroy()
    print("[OK] Receiver stopped.")


def _receive_display(receiver, target, a):
    import cv2
    win = f"NDI: {target['name']}"
    n, start = 0, time.monotonic()
    global running
    while running:
        ft, frame, w, h = ndi.recv_capture(receiver, timeout_ms=100)
        if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
            n += 1
            disp = frame[:, :, :3]
            if a.scale != 1.0:
                disp = cv2.resize(disp, (int(w*a.scale), int(h*a.scale)))
            fps = n / (time.monotonic() - start) if time.monotonic() > start else 0
            cv2.putText(disp, f"FPS: {fps:.1f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.imshow(win, disp)
        if cv2.waitKey(1) & 0xFF in (ord('q'), 27): break
    cv2.destroyAllWindows()


def _receive_headless(receiver, a):
    if a.output: os.makedirs(a.output, exist_ok=True)
    n, start = 0, time.monotonic()
    global running
    while running:
        ft, frame, w, h = ndi.recv_capture(receiver, timeout_ms=500)
        if ft == ndi.FRAME_TYPE_VIDEO and frame is not None:
            n += 1
            if a.output and n % max(1, 30 // a.save_fps) == 0:
                import cv2
                cv2.imwrite(os.path.join(a.output, f"frame_{n:06d}.png"), frame[:, :, :3])
            if n % 150 == 0:
                print(f"  Frames: {n} | FPS: {n/(time.monotonic()-start):.1f}")
    print(f"[OK] Received {n} frames.")


if __name__ == "__main__": main()
