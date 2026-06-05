# NDI Streaming Solution for Raspberry Pi OS — v5

A complete NDI (Network Device Interface) solution for Raspberry Pi 4 and 5. Send, receive, multiview, video wall, and PTZ camera control — all managed through a web-based control panel.

## Features

- **Web Control Panel** — Manage everything from any browser on your network
- **NDI Sender** — Stream from USB webcam, CSI HDMI capture, Pi Camera, or test pattern
- **NDI Receiver** — Discover and display NDI sources with live browser preview
- **Dual HDMI Output** — Two output presets for quick source/resolution switching
- **Multiview** — Flexible grid compositor with overlays, tally, and drag-and-drop layout
- **Video Wall** — Multi-Pi synchronized display with web-based configuration
- **PTZ Control** — Pan, tilt, zoom, focus, and 8 presets for USB and NDI cameras
- **CSI HDMI Capture** — Use HDMI-to-CSI adapters to capture HDMI input as NDI
- **Autostart on boot** — Boot directly into video playback with no desktop
- **No compilation needed** — Uses a ctypes wrapper to talk directly to libndi.so

## Requirements

- Raspberry Pi 4 or 5 (aarch64)
- Raspberry Pi OS (Bookworm, 64-bit)
- Python 3.9+
- Ethernet connection (strongly recommended)
- Optional: USB webcam, CSI HDMI capture adapter, Pi Camera Module

## Quick Start

```bash
# 1. Copy files to your Pi
scp -r ndi-rpi-solutions/ admin@YOURHOSTNAME.local:~/ndi-rpi-solutions/

# 2. SSH in and run setup
ssh admin@YOURHOSTNAME.local
cd ~/ndi-rpi-solutions
sudo bash setup.sh

# 3. Open in browser
# http://YOURHOSTNAME.local:5000
```

See **GUIDE.md** for detailed installation and usage instructions.

## Command-Line Tools

```bash
source ~/ndi-env/bin/activate

# Send from USB camera
python3 ndi_camera.py --device /dev/video0

# Send test pattern
python3 ndi_sender.py --source test

# List NDI sources
python3 ndi_receiver.py --list

# Monitor network
python3 ndi_monitor.py
```

## Web Panel

Open `http://YOURHOSTNAME.local:5000` from any device on the same network.

- **Sender** — Select source, resolution, FPS, start/stop
- **PTZ Control** — Pan/tilt/zoom/focus with presets, supports USB and NDI cameras
- **Receiver Preview** — Scan network, preview NDI sources in browser
- **HDMI Output A/B** — Route NDI sources to HDMI displays
- **Multiview** — Drag-and-drop grid with overlays and tally
- **Video Wall** — Multi-Pi coordinated display
- **System Settings** — Boot mode, layout, NDI device management

## License

This project uses the NDI SDK which is subject to the NDI SDK license agreement.
See https://ndi.video/sdk/ for details.
