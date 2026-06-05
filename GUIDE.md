# NDI HDMI Player for Raspberry Pi — v5

A complete NDI solution that turns a Raspberry Pi 4 or 5 into a network video player, sender, multiviewer, and video wall node. Receives and sends NDI streams over the network with full PTZ camera control — all managed through a web-based control panel.

---

## Table of Contents

1. [What This Does](#what-this-does)
2. [Hardware Requirements](#hardware-requirements)
3. [Raspberry Pi Initial Setup](#raspberry-pi-initial-setup)
4. [Software Installation](#software-installation)
5. [First Run](#first-run)
6. [Dedicated Player Setup (Headless)](#dedicated-player-setup-headless)
7. [Camera Sender](#camera-sender)
8. [PTZ Camera Control](#ptz-camera-control)
9. [CSI HDMI Capture](#csi-hdmi-capture)
10. [Web Panel Reference](#web-panel-reference)
11. [NDI Best Practices](#ndi-best-practices)
12. [Network Setup for NDI](#network-setup-for-ndi)
13. [Troubleshooting](#troubleshooting)
14. [File Reference](#file-reference)

---

## What This Does

This software receives an NDI video stream from any NDI sender on your local network (such as Resolume Arena, OBS, vMix, TriCaster, or any NDI-enabled camera) and displays it fullscreen on the Pi's HDMI output. It can also send video from USB cameras and CSI HDMI capture devices as NDI sources, with full PTZ control.

**Key features:**

- Web-based control panel accessible from any device on the network
- Two output presets (Output A / B) for quick switching between sources or resolutions
- Autostart on boot — the Pi can boot directly into video playback with no desktop
- Resolution control — output at stream native resolution or scale to a specific resolution
- Live FPS and frame count monitoring
- NDI source discovery (automatic network scanning)
- Works in both desktop mode (SDL2 hardware accelerated) and CLI/framebuffer mode
- NDI sender — USB webcam, Pi Camera, CSI HDMI capture, or test pattern
- PTZ camera control — pan, tilt, zoom, focus, presets for USB and NDI cameras
- Multiview compositor with flexible grid layout and overlays
- Multi-Pi video wall with drag-and-drop configuration
- Receive benchmark tool for diagnosing network performance

---

## Hardware Requirements

**Raspberry Pi:**
- Raspberry Pi 5 (recommended — better CPU for higher resolutions)
- Raspberry Pi 4 (works well for 1080p and below)

**Storage:**
- MicroSD card, 16 GB minimum (32 GB recommended)
- Class 10 / A1 rating minimum

**Network:**
- Ethernet connection (strongly recommended for reliable NDI)
- WiFi works for testing but is not reliable for production use
- Gigabit Ethernet switch (see Network Setup section)

**Display:**
- Any HDMI display/monitor/TV
- Micro-HDMI to HDMI cable (Pi 5) or Mini-HDMI to HDMI cable (Pi 4)

**Power:**
- Official Raspberry Pi USB-C power supply (5V/3A for Pi 4, 5V/5A for Pi 5)

---

## Raspberry Pi Initial Setup

### 1. Flash Raspberry Pi OS

Use the official **Raspberry Pi Imager** (download from raspberrypi.com):

1. Insert your MicroSD card into your computer
2. Open Raspberry Pi Imager
3. Choose OS: **Raspberry Pi OS (64-bit)** — the full desktop version
4. Choose your MicroSD card as the target
5. Click the gear icon (⚙) to configure:
   - **Hostname:** `YOURHOSTNAME` (or whatever you want this player to be called on the network)
   - **Enable SSH:** Yes, use password authentication
   - **Username:** `admin` (or your preferred username)
   - **Password:** Choose a strong password
   - **WiFi:** Configure if needed for initial setup (Ethernet is preferred for NDI)
   - **Locale:** Set your timezone and keyboard layout
6. Click **Write** and wait for it to finish

### 2. First Boot

1. Insert the MicroSD card into the Pi
2. Connect Ethernet cable, HDMI display, and power
3. The Pi will boot to the desktop (first boot takes a few minutes)
4. Connect via SSH from another computer:

```
ssh admin@YOURHOSTNAME.local
```

(Replace `admin` with your username and `YOURHOSTNAME` with your chosen hostname)

### 3. System Update

```bash
sudo apt update && sudo apt upgrade -y
sudo reboot
```

Wait for the Pi to reboot, then SSH back in.

---

## Software Installation

### 1. Transfer Files to the Pi

From your computer, copy the entire project folder to the Pi:

```bash
scp -r ndi-rpi-solutions/ admin@YOURHOSTNAME.local:~/ndi-rpi-solutions/
```

Or if you have the files on a USB drive:

```bash
cp -r /media/admin/USB/ndi-rpi-solutions/ ~/ndi-rpi-solutions/
```

### 2. Run the Setup Script

SSH into the Pi and run:

```bash
cd ~/ndi-rpi-solutions
sudo bash setup.sh
```

The setup script will:
1. Ask you to confirm/change the hostname
2. Install all system dependencies (build tools, Python, OpenCV, SDL2, Avahi)
3. Download and install the NDI SDK native library
4. Create a Python virtual environment with all required packages
5. Copy project files to the correct location
6. Configure sudoers for passwordless service management
7. Install and start the web panel service
8. Run verification tests

**This takes approximately 5–10 minutes** depending on your internet speed.

### 3. Verify Installation

At the end of setup, you should see all tests passing:

```
[OK] NDI library: OK
[OK] OpenCV: OK
[OK] Flask: OK
[OK] SDL2: OK
[OK] Web panel: OK (running on port 5000)
```

If any tests fail, check the Troubleshooting section.

### 4. Access the Web Panel

Open a browser on any device on the same network and navigate to:

```
http://YOURHOSTNAME.local:5000
```

You should see the NDI Control Panel.

---

## First Run

### Testing HDMI Output

1. **Open the web panel** at `http://YOURHOSTNAME.local:5000`
2. **Scan for sources:** Click the "Scan Network" button. Your NDI sources should appear within a few seconds.
3. **Start an HDMI output:**
   - In the "HDMI Output A" card, select your NDI source from the dropdown
   - Leave "Use stream resolution" toggled ON for the first test
   - Leave "Display Mode" on "Auto-detect"
   - Click "Start Output A"
4. **Check your HDMI display** — you should see the NDI stream fullscreen
5. **Monitor performance** — the stats bar shows FPS, frame count, and resolution

### Testing Resolution Scaling

1. Toggle OFF "Use stream resolution"
2. Select a different resolution from the dropdown (e.g., 1920x1080)
3. Click "Start Output A" again
4. Compare the FPS — CPU scaling adds some overhead, especially at higher resolutions

---

## Dedicated Player Setup (Headless)

For production use, you want the Pi to boot directly into video playback with no desktop environment — faster boot, lower resource usage, and no mouse cursor.

### 1. Enable Autostart

In the web panel:

1. Select your NDI source in HDMI Output A
2. Choose your desired output resolution (or keep "Use stream resolution" ON)
3. Toggle ON **"Autostart on boot"**
4. The description should update to show your saved configuration

### 2. Enable Web Panel Autostart

In the System Settings card at the bottom:

1. Toggle ON **"Web Panel autostart"**
2. This ensures you can always access the control panel after reboot

### 3. Switch to CLI Boot Mode

In the System Settings card:

1. Select **"CLI + autologin"** from the Boot Mode dropdown
2. Click **Apply**
3. You'll see a confirmation with a reminder about autostart

### 4. Reboot

Click the **"Reboot Pi"** button in System Settings.

After reboot (approximately 15–30 seconds):
- The Pi boots directly to CLI (no desktop)
- The web panel starts automatically
- The HDMI output starts automatically with your configured source
- The HDMI resolution is set to match your configuration
- No mouse cursor, no blinking terminal cursor — just clean video output

### What Happens at Boot

1. Kernel boots with custom HDMI resolution parameters (`video=HDMI-A-1:1920x1080@60`)
2. Framebuffer depth set to 32bpp for pixel-perfect color
3. Terminal cursor disabled at kernel level
4. System reaches `multi-user.target`
5. Web panel service starts (waits for network)
6. HDMI autostart service starts:
   - Detects CLI mode → skips display server wait
   - Waits for network (up to 30 seconds)
   - Hides remaining cursor artifacts
   - Launches NDI receiver in framebuffer mode
   - Stream appears on HDMI

### Recovering Access

If something goes wrong and you can't reach the web panel:

```bash
# SSH still works in CLI mode
ssh admin@YOURHOSTNAME.local

# Check service status
sudo systemctl status ndi-webpanel
sudo systemctl status ndi-hdmi-hdmi0

# View logs
journalctl -u ndi-hdmi-hdmi0 -b --no-pager

# Switch back to desktop mode
sudo raspi-config nonint do_boot_behaviour B4
sudo reboot
```

---

## Web Panel Reference

### Cards

**Sender:** Send a test pattern, USB webcam, or Pi Camera as an NDI source. Useful for testing.

**Receiver Preview:** Scan the network for NDI sources and preview them in the browser. This is independent of HDMI output.

**HDMI Output A / B:** Two output presets. Each has its own source, resolution, mode, and autostart configuration. Only one can be active at a time (they share the same framebuffer/display).

**Diagnostics:** Run a receive benchmark on any NDI source to measure FPS, frame timing, bandwidth, and missed frames. Useful for diagnosing network issues.

**System Settings:** Boot mode, web panel autostart, reboot, and shutdown.

### Resolution Modes

**"Use stream resolution" ON (default):**
The HDMI output displays whatever the NDI source sends, with GPU letterboxing to fit the screen. No CPU scaling overhead. Best for performance.

**"Use stream resolution" OFF:**
The HDMI output scales every frame to your chosen resolution using CPU (cv2.resize). Useful when you need a specific output resolution. Adds some CPU overhead — check the FPS counter to measure impact.

### Display Modes

**Auto-detect (recommended):** Automatically uses SDL2 (desktop) or framebuffer (CLI) based on what's available.

**Desktop (SDL2):** Forces SDL2 hardware-accelerated rendering. Requires a running display server (Wayland/X11). GPU does the letterboxing.

**Framebuffer:** Direct framebuffer writes, no display server needed. Used in CLI/headless mode. The boot-time kernel parameters control the HDMI output resolution.

---

## Camera Sender

The Pi can send video from attached cameras as an NDI source, visible to any NDI receiver on the network.

### Supported Cameras

- **USB webcams** — any UVC-compatible USB camera
- **CSI HDMI capture** — HDMI-to-CSI adapters (TC358743-based, such as Geekworm C779)
- **Pi Camera Module** — via picamera2

### Sending from the Web Panel

1. Open the web panel at `http://YOURHOSTNAME.local:5000`
2. In the **Sender** card, select your source type:
   - **Test Pattern** — built-in color bars (no camera needed)
   - **USB Webcam** — select from auto-detected cameras in the Device dropdown
   - **CSI HDMI Capture** — for HDMI-to-CSI adapters
   - **Pi Camera** — for CSI camera modules
3. Set resolution and frame rate
4. Click **Start**

### Sending from CLI

```bash
# USB camera — uncompressed NDI
~/ndi-env/bin/python3 ~/ndi-rpi-solutions/ndi_camera.py --device /dev/video0

# USB camera — specify resolution
~/ndi-env/bin/python3 ~/ndi-rpi-solutions/ndi_camera.py --device /dev/video0 --res 1280x720 --fps 30

# Custom NDI source name
~/ndi-env/bin/python3 ~/ndi-rpi-solutions/ndi_camera.py --name "Studio-Cam-1"
```

The NDI source will appear on the network as `YOURHOSTNAME (Camera)` or with your custom name.

---

## PTZ Camera Control

Control pan, tilt, zoom, focus, and presets for USB cameras and NDI PTZ cameras directly from the web panel.

### USB PTZ

Works with any USB camera that supports V4L2 PTZ controls (pan_absolute, tilt_absolute, zoom_absolute). The web panel auto-detects available controls.

1. In the **PTZ Control** card, select **USB Camera** mode
2. Select your camera from the Device dropdown
3. Use the d-pad for pan/tilt, slider for zoom
4. **Focus:** Use the focus slider or click **AF** for auto-focus (if camera supports it)
5. **Presets:** Click **Store Preset…** then a preset number to save position. Click a preset number to recall it.

USB presets are stored in `config.json` and persist across reboots.

### NDI PTZ

Controls remote NDI PTZ cameras on the network (cameras that support the NDI PTZ protocol).

1. In the **PTZ Control** card, select **NDI Source** mode
2. Select the NDI source from the dropdown
3. Click **Connect**
4. Use the same controls — commands are sent over NDI to the remote camera
5. NDI presets are stored in the camera's own memory

### NDI Extra IPs

Some NDI HX cameras may not be found by auto-discovery. In **System Settings**, use the **NDI Extra IPs** field:

- Click **Scan** to auto-detect NDI devices via mDNS
- Or manually enter camera IP addresses (comma-separated)
- Click **Save** — takes effect immediately

---

## CSI HDMI Capture

Use an HDMI-to-CSI adapter (such as the Geekworm C779 with TC358743 chip) to capture HDMI input and send it as NDI.

### Hardware Setup

**Pi 4:** Connect the adapter to the CSI (CAMERA) port with a 15-pin ribbon cable. The CAMERA port is closer to the Ethernet/USB ports.

**Pi 5:** Connect to the CSI port using a 22-pin ribbon cable (Pi 5 uses a different connector).

### Enable the Device Tree Overlay

```bash
# Pi 4
echo "dtoverlay=tc358743" | sudo tee -a /boot/firmware/config.txt

# Pi 5
echo "dtoverlay=tc358743-pi5" | sudo tee -a /boot/firmware/config.txt

sudo reboot
```

### Verify Detection

After reboot, connect an HDMI source to the adapter, then:

```bash
v4l2-ctl --list-devices
# Should show "unicam" or "rp1-cfe" device

v4l2-ctl -d /dev/video0 --set-edid=pad=0,type=hdmi
sleep 2
v4l2-ctl -d /dev/video0 --query-dv-timings
# Should show the HDMI input resolution
```

### Sending CSI HDMI as NDI

From the web panel: select **CSI HDMI Capture** in the Sender, pick the device, and click Start.

From CLI:

```bash
~/ndi-env/bin/python3 ~/ndi-rpi-solutions/ndi_camera.py --device /dev/video0
```

The script auto-detects the HDMI signal resolution and frame rate.

---

## NDI Best Practices

### Naming Your Sources

Use descriptive, consistent names for your NDI sources:
- `STUDIO-A (Program)` — main program feed
- `STUDIO-A (Preview)` — preview/multiview
- `CAM-1 (Wide)` — camera identification
- `GFX-1 (Lower Thirds)` — graphics sources

The Pi's hostname becomes its NDI source name when using the sender feature (e.g., `YOURHOSTNAME (NDI)`).

### Resolution and Frame Rate

- **Match your pipeline:** If your production is 1080p/30, send NDI at 1080p/30
- **Don't upscale at the source:** Sending 4K when your content is 1080p wastes bandwidth
- **Frame rate consistency matters more than resolution:** A stable 30fps looks better than fluctuating 40-60fps
- **Pi 5 handles 1080p/60 comfortably** via framebuffer
- **Pi 4 is comfortable at 1080p/30** — 60fps is possible but leaves little headroom

### NDI Bandwidth

NDI uses adaptive compression. Typical bandwidth per stream:

| Resolution | Frame Rate | Typical Bandwidth |
|-----------|-----------|------------------|
| 720p | 30 fps | 60–80 Mbps |
| 1080p | 30 fps | 100–130 Mbps |
| 1080p | 60 fps | 150–200 Mbps |
| 4K | 30 fps | 200–300 Mbps |

NDI|HX (hardware compressed) uses significantly less bandwidth (5–20 Mbps) but adds latency.

### Latency

- NDI full-bandwidth: typically 1–3 frames of latency (16–50ms at 30fps)
- NDI|HX: 2–5 frames due to hardware encoding/decoding
- Network switches add minimal latency (microseconds)
- WiFi adds variable latency (10–100ms) — avoid for production

---

## Network Setup for NDI

NDI is a local network protocol. It relies on mDNS (Bonjour/Avahi) for discovery and uses TCP/UDP for video transport.

### Switch Requirements

**Minimum: Gigabit Ethernet (1 Gbps)**

A single 1080p/60 NDI stream uses approximately 150–200 Mbps. With a Gigabit switch:
- 1 stream: ~20% utilization — comfortable
- 2 streams: ~40% utilization — fine
- 3-4 streams: 60-80% — getting tight, monitor for drops

**Recommended: Managed Gigabit switch** with:
- Jumbo frame support (9000 MTU) — reduces CPU overhead for large frames
- IGMP snooping — prevents multicast flooding
- QoS / traffic prioritization — prioritize NDI traffic

**For multi-stream setups:** Consider 2.5GbE or 10GbE switches.

### Network Architecture

**Simple setup (1-2 Pi players):**
```
NDI Sender (PC/Mac) ──── Gigabit Switch ──── Pi Player 1
                              │
                              └──── Pi Player 2
```

**Production setup (multiple players):**
```
NDI Sender 1 ────┐
NDI Sender 2 ────┤
                 ├──── Managed Gigabit Switch ────┬── Pi Player 1
Control PC   ────┤         (with IGMP, QoS)       ├── Pi Player 2
                 └──── (isolated VLAN)             └── Pi Player 3
```

### Switch Configuration Recommendations

1. **Enable IGMP Snooping:** Prevents NDI multicast discovery packets from flooding all ports. Most managed switches have this.

2. **Enable Jumbo Frames (MTU 9000):**
   On the Pi: edit `/etc/dhcpcd.conf` or use NetworkManager:
   ```bash
   sudo ip link set eth0 mtu 9000
   ```
   All devices on the NDI network segment must support the same MTU.

3. **QoS Priority:** If your switch supports 802.1p priority queuing, prioritize traffic on the NDI ports.

4. **Spanning Tree:** Disable or set to RSTP (rapid) on NDI ports to avoid 30-second delays on link changes.

5. **Energy Efficient Ethernet (EEE):** Disable on NDI ports — EEE can add micro-latency as the port transitions between power states.

### Bandwidth Calculation

Formula for checking if your network can handle your NDI setup:

```
Total bandwidth = (Stream 1 Mbps) + (Stream 2 Mbps) + ... + 10% overhead

Switch capacity needed = Total bandwidth × number of destinations
```

Example: 2 senders each at 1080p/30 (130 Mbps), each going to 2 Pi players:
```
Sender bandwidth:  2 × 130 = 260 Mbps
Per destination:   260 × 2  = 520 Mbps total switch throughput
Gigabit switch:    1000 Mbps capacity
Utilization:       52% — comfortable
```

### Firewall / Ports

NDI uses these ports (opened automatically by the installer if ufw is active):

| Port | Protocol | Purpose |
|------|---------|---------|
| 5353 | UDP | mDNS (Avahi) — NDI source discovery |
| 5960-5990 | TCP+UDP | NDI video transport |
| 5000 | TCP | Web control panel |

### WiFi vs Ethernet

**Always use Ethernet for production NDI.** WiFi issues with NDI:

- Shared medium — all devices compete for airtime
- Half-duplex — can't send and receive simultaneously
- Variable latency — 10ms to 100ms+, unpredictable
- Interference — other WiFi devices, microwaves, Bluetooth
- Bandwidth: WiFi 5 (802.11ac) maxes at ~400 Mbps real-world, shared among all clients

WiFi is acceptable for: the web control panel, testing, monitoring.

---

## Troubleshooting

### Web panel not loading

```bash
# Check if the service is running
sudo systemctl status ndi-webpanel

# View logs
journalctl -u ndi-webpanel -b --no-pager

# Restart the service
sudo systemctl restart ndi-webpanel

# Run manually to see errors
cd ~/ndi-rpi-solutions
~/ndi-env/bin/python3 ndi_web.py
```

### No NDI sources found

```bash
# Check Avahi is running
sudo systemctl status avahi-daemon

# Test mDNS
avahi-browse -art | head -20

# Check NDI library loads
~/ndi-env/bin/python3 -c "
import sys; sys.path.insert(0, '/home/admin/ndi-rpi-solutions')
import ndi_ctypes as ndi
ndi.initialize()
sources = ndi.discover_sources(timeout_ms=5000)
for s in sources:
    print(s['name'])
ndi.destroy()
"
```

Common causes:
- Sender and Pi are on different subnets/VLANs
- Firewall blocking mDNS (port 5353/UDP)
- Avahi not running
- NDI sender not running

### HDMI output black screen

```bash
# Check the HDMI service
sudo systemctl status ndi-hdmi-hdmi0
journalctl -u ndi-hdmi-hdmi0 -b --no-pager

# Check framebuffer exists
ls -la /dev/fb*
fbset

# Test framebuffer directly (should show white screen)
dd if=/dev/urandom of=/dev/fb0 bs=1M count=10 2>/dev/null
```

### HDMI output wrong resolution (CLI mode)

```bash
# Check current kernel parameters
cat /boot/firmware/cmdline.txt

# Should contain something like:
# video=HDMI-A-1:1920x1080@60 vt.global_cursor_default=0 consoleblank=0 logo.nologo

# Check framebuffer info
fbset
cat /sys/class/graphics/fb0/virtual_size
```

### Low FPS

1. Check the source FPS in the web panel stats
2. Run the Diagnostics benchmark to measure raw receive performance
3. If using "Use stream resolution" OFF, try turning it ON — CPU scaling adds overhead
4. On Pi 4: 1080p/60 is the practical maximum
5. Check CPU usage: `htop`
6. Check temperature: `vcgencmd measure_temp` — thermal throttling starts at 80°C

### Terminal cursor visible through video

This should be handled automatically by the autostart wrapper. If it persists:

```bash
# Disable cursor manually
echo 0 | sudo tee /sys/class/graphics/fbcon/cursor_blink
sudo sh -c 'echo -e "\033[?25l" > /dev/tty0'

# Verify kernel parameters include cursor suppression
grep "vt.global_cursor_default=0" /boot/firmware/cmdline.txt
```

### Service won't stop / restart

```bash
# Force stop
sudo systemctl stop ndi-hdmi-hdmi0
sudo pkill -9 -f ndi_hdmi.py

# Clean up
sudo systemctl reset-failed ndi-hdmi-hdmi0 2>/dev/null
```

### Permission denied errors (Save failed, config.json, start_multiview.sh)

This happens when runtime files were created by `sudo` but the web panel runs as your normal user. Quick fix:

```bash
sudo bash setup.sh --fix-permissions
```

Or manually:
```bash
sudo chown -R $(whoami):$(whoami) ~/ndi-rpi-solutions/
```

The `setup.sh` installer pre-creates all runtime files with correct ownership during a fresh install. The `--fix-permissions` flag can be run anytime to repair ownership without re-installing.

### Switching back to desktop mode

```bash
# Via SSH
sudo raspi-config nonint do_boot_behaviour B4
sudo reboot

# Or via the web panel: System Settings → Boot Mode → Desktop + autologin → Apply → Reboot
```

---

## File Reference

### Project Files

| File | Purpose |
|------|---------|
| `setup.sh` | Full installer — run this on a clean Pi. Also supports `--fix-permissions` |
| `install_ndi_sdk.sh` | Downloads and installs the NDI SDK library |
| `ndi_web.py` | Web control panel (Flask server) |
| `ndi_hdmi.py` | HDMI output engine (SDL2 desktop + framebuffer) |
| `ndi_multiview.py` | Multiview compositor engine (flex grid, DRM 32bpp) |
| `drm_display.py` | DRM dumb buffer display (bypasses fbdev 16bpp) |
| `ndi_ctypes.py` | Python bindings for the NDI C library |
| `ndi_sender.py` | Standalone NDI sender (test pattern, webcam, Pi Camera) |
| `ndi_camera.py` | Camera-to-NDI sender (USB, CSI HDMI, auto-detect) |
| `ndi_receiver.py` | Standalone NDI receiver (preview/save frames) |
| `ndi_wall.py` | Video wall engine (multi-Pi synchronized display) |
| `ndi_monitor.py` | CLI NDI source monitor |
| `requirements.txt` | Python package dependencies |
| `templates/index.html` | Web panel frontend |
| `static/images/` | Uploaded images for tile backgrounds and overlays |

### Generated Files (created by the web panel at runtime)

| File | Purpose |
|------|---------|
| `config.json` | Runtime configuration (layouts, autostart settings) |
| `multiview_layout.json` | Current flex layout for multiview engine |
| `start_hdmi0.sh` | Auto-generated wrapper script for HDMI output 0 |
| `start_hdmi1.sh` | Auto-generated wrapper script for HDMI output 1 |
| `start_multiview.sh` | Auto-generated wrapper script for multiview autostart |
| `/etc/systemd/system/ndi-hdmi-hdmi0.service` | Systemd service for HDMI autostart |
| `/etc/systemd/system/ndi-multiview.service` | Systemd service for multiview autostart |
| `/etc/systemd/system/ndi-webpanel.service` | Systemd service for web panel |

### System Files Modified

| File | What Changes |
|------|-------------|
| `/boot/firmware/cmdline.txt` | HDMI resolution, cursor suppression (when autostart enabled) |
| `/boot/firmware/config.txt` | Framebuffer depth (when autostart enabled) |
| `/etc/sudoers.d/ndi-player` | Passwordless sudo for service management |
| `/etc/ld.so.conf.d/ndi.conf` | Library path for libndi.so |

---

## Quick Reference Commands

```bash
# SSH into the Pi
ssh admin@YOURHOSTNAME.local

# Web panel URL
http://YOURHOSTNAME.local:5000

# Service management
sudo systemctl status ndi-webpanel        # Web panel status
sudo systemctl status ndi-hdmi-hdmi0      # HDMI output status
sudo systemctl status ndi-multiview       # Multiview status
sudo systemctl restart ndi-webpanel       # Restart web panel
sudo systemctl stop ndi-hdmi-hdmi0        # Stop HDMI output
sudo systemctl stop ndi-multiview         # Stop multiview

# Logs
journalctl -u ndi-webpanel -f             # Follow web panel logs
journalctl -u ndi-hdmi-hdmi0 -f           # Follow HDMI output logs
journalctl -u ndi-multiview -f            # Follow multiview logs
journalctl -u ndi-multiview -b --no-pager # Full multiview boot log

# Fix permissions (if Save/config errors occur)
sudo bash ~/ndi-rpi-solutions/setup.sh --fix-permissions

# Diagnostics
~/ndi-env/bin/python3 ~/ndi-rpi-solutions/ndi_hdmi.py --list   # List NDI sources
~/ndi-env/bin/python3 ~/ndi-rpi-solutions/ndi_multiview.py --list  # List NDI sources
vcgencmd measure_temp                      # CPU temperature
htop                                       # CPU/memory usage
fbset                                      # Framebuffer info
cat /sys/class/graphics/fb0/bits_per_pixel # Framebuffer depth
```
