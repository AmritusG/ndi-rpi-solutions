#!/usr/bin/env python3
"""
Analyze a phone video to measure latency offset between two screens.

How to use:
1. Open ndi-latency-counter.html in browser, capture with Resolume, send to Pi(s)
2. Record a slow-mo video (120/240fps) with your phone showing both screens
3. Run: python3 analyze_latency.py video.mp4

The script detects the flashing white squares on each screen and cross-correlates
their brightness to find the time offset.
"""

import sys
import numpy as np

try:
    import cv2
except ImportError:
    print("Install opencv: pip install opencv-python")
    sys.exit(1)

def select_roi(frame, title):
    """Let user draw a rectangle on the frame."""
    print(f"\n  → Draw a rectangle around the {title} flashing square, then press ENTER")
    print(f"    (Press 'c' to cancel and redraw)")
    roi = cv2.selectROI(title, frame, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(title)
    if roi[2] == 0 or roi[3] == 0:
        print("  No region selected!")
        return None
    x, y, w, h = roi
    print(f"    Selected: x={x} y={y} w={w} h={h}")
    return (x, y, w, h)

def get_brightness(frame, roi):
    """Get average brightness of a region."""
    x, y, w, h = roi
    region = frame[y:y+h, x:x+w]
    return np.mean(region)

def find_offset(sig1, sig2, fps):
    """Cross-correlate two signals to find time offset."""
    # Normalize signals
    s1 = (sig1 - np.mean(sig1)) / (np.std(sig1) + 1e-10)
    s2 = (sig2 - np.mean(sig2)) / (np.std(sig2) + 1e-10)
    
    # Cross-correlate
    corr = np.correlate(s1, s2, mode='full')
    
    # Find peak
    mid = len(s1) - 1
    peak_idx = np.argmax(corr)
    offset_frames = peak_idx - mid
    offset_ms = offset_frames / fps * 1000
    
    # Confidence: correlation value at peak vs mean
    peak_val = corr[peak_idx]
    confidence = peak_val / len(s1)
    
    return offset_ms, offset_frames, confidence, corr

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_latency.py <video_file> [--fps 240]")
        print("  Record a slow-mo video showing both screens with the latency counter")
        print("  Use --fps to override detected FPS (iPhone slow-mo reports 30 instead of 240)")
        sys.exit(1)
    
    video_path = sys.argv[1]
    override_fps = None
    if '--fps' in sys.argv:
        idx = sys.argv.index('--fps')
        if idx + 1 < len(sys.argv):
            override_fps = float(sys.argv[idx + 1])
    
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Error: Cannot open {video_path}")
        sys.exit(1)
    
    fps = override_fps if override_fps else cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0
    detected_fps = cap.get(cv2.CAP_PROP_FPS)
    
    print(f"\n  NDI Latency Analyzer")
    print(f"  ────────────────────")
    print(f"  Video:    {video_path}")
    if override_fps:
        print(f"  FPS:      {fps:.1f} (override — detected {detected_fps:.1f})")
    else:
        print(f"  FPS:      {fps:.1f}")
    print(f"  Frames:   {total_frames}")
    print(f"  Duration: {duration:.1f}s")
    print(f"  Resolution per frame: {1000/fps:.1f}ms")
    print()
    
    # Read first frame for ROI selection
    ret, frame = cap.read()
    if not ret:
        print("Error: Cannot read video")
        sys.exit(1)
    
    # Scale down for display if too large
    h, w = frame.shape[:2]
    scale = 1.0
    if w > 1600:
        scale = 1600 / w
        frame_display = cv2.resize(frame, (int(w * scale), int(h * scale)))
    else:
        frame_display = frame.copy()
    
    print("  Select two regions: the flashing square on each screen.")
    print("  (Top-left white/black square from the latency counter)")
    
    roi1 = select_roi(frame_display, "SOURCE screen (Mac/Resolume)")
    if roi1 is None:
        sys.exit(1)
    
    roi2 = select_roi(frame_display, "PI screen (HDMI output)")
    if roi2 is None:
        sys.exit(1)
    
    # Scale ROIs back to original resolution
    if scale != 1.0:
        roi1 = tuple(int(v / scale) for v in roi1)
        roi2 = tuple(int(v / scale) for v in roi2)
    
    # Analyze all frames
    print(f"\n  Analyzing {total_frames} frames...")
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    
    brightness1 = []
    brightness2 = []
    frame_idx = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness1.append(get_brightness(gray, roi1))
        brightness2.append(get_brightness(gray, roi2))
        
        frame_idx += 1
        if frame_idx % 500 == 0:
            print(f"    {frame_idx}/{total_frames} frames...")
    
    cap.release()
    
    sig1 = np.array(brightness1)
    sig2 = np.array(brightness2)
    
    print(f"  Analyzed {len(sig1)} frames")
    print(f"  Source brightness: min={sig1.min():.0f} max={sig1.max():.0f} range={sig1.max()-sig1.min():.0f}")
    print(f"  Pi brightness:     min={sig2.min():.0f} max={sig2.max():.0f} range={sig2.max()-sig2.min():.0f}")
    
    if sig1.max() - sig1.min() < 20:
        print("\n  WARNING: Source region has low contrast — may not be on the flashing square")
    if sig2.max() - sig2.min() < 20:
        print("\n  WARNING: Pi region has low contrast — may not be on the flashing square")
    
    # Find offset
    offset_ms, offset_frames, confidence, corr = find_offset(sig1, sig2, fps)
    
    print(f"\n  ════════════════════════════════════")
    print(f"  RESULT: {abs(offset_ms):.1f}ms latency")
    print(f"  ════════════════════════════════════")
    print(f"  Offset:     {offset_frames:+d} frames ({offset_ms:+.1f}ms)")
    if offset_ms > 0:
        print(f"  Direction:  Pi is {abs(offset_ms):.1f}ms BEHIND source")
    elif offset_ms < 0:
        print(f"  Direction:  Pi is {abs(offset_ms):.1f}ms AHEAD of source (unlikely — check ROI order)")
    else:
        print(f"  Direction:  Perfectly synced")
    print(f"  Confidence: {confidence:.2f}")
    print(f"  Resolution: ±{1000/fps:.1f}ms (limited by {fps:.0f}fps video)")
    print()
    
    # Also try to detect offset between the two Pi screens (if both are Pi)
    # Save plot data
    try:
        plot_path = video_path.rsplit('.', 1)[0] + '_latency.png'
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        
        fig, axes = plt.subplots(3, 1, figsize=(14, 8))
        
        t = np.arange(len(sig1)) / fps * 1000  # time in ms
        
        axes[0].plot(t, sig1, 'b-', linewidth=0.5, label='Source')
        axes[0].plot(t, sig2, 'r-', linewidth=0.5, label='Pi')
        axes[0].set_ylabel('Brightness')
        axes[0].set_xlabel('Time (ms)')
        axes[0].legend()
        axes[0].set_title(f'Brightness over time — Offset: {abs(offset_ms):.1f}ms')
        
        # Zoomed view (first 2 seconds)
        zoom = min(len(t), int(2 * fps))
        axes[1].plot(t[:zoom], sig1[:zoom], 'b-', linewidth=1, label='Source')
        axes[1].plot(t[:zoom], sig2[:zoom], 'r-', linewidth=1, label='Pi')
        axes[1].set_ylabel('Brightness')
        axes[1].set_xlabel('Time (ms)')
        axes[1].legend()
        axes[1].set_title('Zoomed: first 2 seconds')
        
        # Cross-correlation
        corr_t = (np.arange(len(corr)) - (len(sig1) - 1)) / fps * 1000
        axes[2].plot(corr_t, corr, 'g-', linewidth=0.5)
        axes[2].axvline(x=offset_ms, color='r', linestyle='--', label=f'Peak: {offset_ms:+.1f}ms')
        axes[2].set_ylabel('Correlation')
        axes[2].set_xlabel('Offset (ms)')
        axes[2].legend()
        axes[2].set_title('Cross-correlation')
        axes[2].set_xlim(-500, 500)
        
        plt.tight_layout()
        plt.savefig(plot_path, dpi=150)
        print(f"  Plot saved: {plot_path}")
    except ImportError:
        print("  (Install matplotlib for visual plot: pip install matplotlib)")

if __name__ == "__main__":
    main()
