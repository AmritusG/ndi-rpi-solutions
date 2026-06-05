# CLAUDE.md

## Project Overview

NDI streaming solution for Raspberry Pi 4 and 5. Receives, sends, multiviews, and video-walls NDI streams with a web-based control panel. Python 3.9+, Flask, ctypes NDI bindings.

## Architecture

| File | Role |
|------|------|
| `ndi_web.py` | Flask web server — main entry point, all API routes, sender/receiver/PTZ threads |
| `ndi_ctypes.py` | Python ctypes wrapper for libndi.so (standard NDI SDK) |
| `ndi_camera.py` | Standalone camera-to-NDI sender (USB, CSI HDMI) |
| `ndi_hdmi.py` | NDI-to-HDMI output (SDL2 desktop / DRM framebuffer) |
| `ndi_multiview.py` | Multiview compositor (flex grid, DRM 32bpp) |
| `ndi_wall.py` | Video wall engine (multi-Pi synchronized display) |
| `ndi_sender.py` | Standalone NDI sender (test pattern, webcam, Pi Camera) |
| `ndi_receiver.py` | Standalone NDI receiver (preview, save frames) |
| `ndi_monitor.py` | NDI network monitor / diagnostics |
| `drm_display.py` | Low-level DRM/KMS display driver for framebuffer output |
| `templates/index.html` | Single-page web UI (vanilla JS, no framework) |
| `setup.sh` | Full installer — NDI SDK, Python venv, systemd services |

## Key Design Decisions

- **No compilation** — uses ctypes to call libndi.so directly, no C extensions
- **Single HTML file** — entire web UI in one template, no build step
- **Standard NDI SDK only** — no Advanced SDK dependencies
- **Pi 4 + Pi 5** — auto-detects hardware, adapts encoder/display paths
- **CSI HDMI** — supports TC358743 adapters, auto-detects DV timings

## Conventions

- Python 3.9+ with type hints on public functions
- f-strings, not .format()
- All API endpoints return JSON
- Web panel sections are draggable cards with collapse/expand
- Config stored in `config.json` (not in repo — gitignored)
- NDI source names never include hostname (NDI prepends it automatically)

## How to Run

```bash
# Install
sudo bash setup.sh

# Start web panel
~/ndi-env/bin/python3 ndi_web.py --port 5000

# Start camera sender
~/ndi-env/bin/python3 ndi_camera.py --device /dev/video0
```

## How to Test

```bash
# USB camera detection
v4l2-ctl --list-devices

# NDI source discovery
~/ndi-env/bin/python3 -c "
import ndi_ctypes as ndi
ndi.initialize()
sources = ndi.discover_sources(timeout_ms=5000)
for s in sources: print(s['name'])
ndi.destroy()
"

# Web panel API
curl -s http://localhost:5000/api/status | python3 -m json.tool
curl -s http://localhost:5000/api/sources | python3 -m json.tool
curl -s http://localhost:5000/api/cameras | python3 -m json.tool
```

## Standing Rules

- Never deliver files to `/tmp/` — use project directory
- Always validate Python with `ast.parse()` before committing
- Diagnose before fix — add logging first, never guess
- USB PTZ uses absolute positioning, not speed-based (speed doesn't update position registers)
- CSI HDMI 50fps input must be capped to 25fps encode on Pi 4
- NDI finder must be persistent (create once, poll repeatedly) — don't create/destroy every scan
