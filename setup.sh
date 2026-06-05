#!/usr/bin/env bash
# ============================================================================
#  NDI HDMI Player — Full Setup for Raspberry Pi 4/5
#  Run on a clean Raspberry Pi OS install.
#  Usage:  sudo bash setup.sh
# ============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
log()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!!]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; }
info()  { echo -e "${BLUE}[i]${NC} $1"; }
header(){ echo -e "\n${CYAN}═══ $1 ═══${NC}\n"; }

# ── Pre-flight checks ──────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    error "Run with sudo:  sudo bash $0"
    exit 1
fi

ACTUAL_USER="${SUDO_USER:-$USER}"
if [[ "$ACTUAL_USER" == "root" ]]; then
    error "Run with sudo from a normal user, not as root directly."
    exit 1
fi

ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")
INSTALL_DIR="$ACTUAL_HOME/ndi-rpi-solutions"

# ── Quick fix: --fix-permissions ─────────────────────────────────────

if [[ "${1:-}" == "--fix-permissions" ]]; then
    header "Fixing permissions for $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR/static/images"
    for F in config.json multiview_layout.json start_multiview.sh; do
        [ ! -f "$INSTALL_DIR/$F" ] && touch "$INSTALL_DIR/$F"
    done
    chmod 755 "$INSTALL_DIR/start_multiview.sh" 2>/dev/null || true
    chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
    chown -R "$ACTUAL_USER:$ACTUAL_USER" "$INSTALL_DIR"
    log "All files in $INSTALL_DIR owned by $ACTUAL_USER"
    log "Permissions fixed!"
    exit 0
fi

HOME_DIR="/home/$ACTUAL_USER"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME_DIR/ndi-rpi-solutions"
VENV_DIR="$HOME_DIR/ndi-env"
ARCH=$(dpkg --print-architecture)

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   NDI HDMI Player — Raspberry Pi Installer   ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  User:         $ACTUAL_USER"
echo "  Architecture: $ARCH"
echo "  Source:       $SCRIPT_DIR"
echo "  Install to:   $INSTALL_DIR"
echo "  Python venv:  $VENV_DIR"
echo ""

# ── Step 1: Set hostname ───────────────────────────────────────────────

header "Step 1/9 — Hostname"

CURRENT_HOSTNAME=$(hostname)
info "Current hostname: $CURRENT_HOSTNAME"

read -p "  Enter hostname for this Pi (e.g. NDI-Feed1) [$CURRENT_HOSTNAME]: " NEW_HOSTNAME
NEW_HOSTNAME="${NEW_HOSTNAME:-$CURRENT_HOSTNAME}"

if [[ "$NEW_HOSTNAME" != "$CURRENT_HOSTNAME" ]]; then
    hostnamectl set-hostname "$NEW_HOSTNAME"
    sed -i "s/$CURRENT_HOSTNAME/$NEW_HOSTNAME/g" /etc/hosts 2>/dev/null || true
    log "Hostname set to: $NEW_HOSTNAME"
else
    log "Hostname unchanged: $CURRENT_HOSTNAME"
fi

# ── Step 2: System packages ───────────────────────────────────────────

header "Step 2/9 — System packages"

info "Updating package list..."
apt-get update -qq

info "Installing dependencies (this may take a few minutes)..."
apt-get install -y -qq \
    build-essential cmake pkg-config \
    python3 python3-pip python3-venv python3-dev python3-numpy \
    libavahi-client-dev avahi-daemon avahi-utils \
    ffmpeg libopencv-dev python3-opencv \
    libsdl2-2.0-0 libsdl2-dev \
    v4l-utils wget curl unzip dos2unix \
    > /dev/null 2>&1

log "System packages installed."

# ── Step 3: Avahi (mDNS / NDI discovery) ──────────────────────────────

header "Step 3/9 — Avahi mDNS"

systemctl enable avahi-daemon 2>/dev/null || true
systemctl start avahi-daemon 2>/dev/null || true
log "Avahi mDNS service active (required for NDI source discovery)."

# ── Step 4: NDI SDK ───────────────────────────────────────────────────

header "Step 4/9 — NDI SDK native library"

if ldconfig -p | grep -q libndi; then
    log "NDI library already installed:"
    ldconfig -p | grep libndi | head -2
else
    info "Installing NDI SDK..."
    if [[ -f "$SCRIPT_DIR/install_ndi_sdk.sh" ]]; then
        bash "$SCRIPT_DIR/install_ndi_sdk.sh"
    else
        error "install_ndi_sdk.sh not found in $SCRIPT_DIR"
        echo ""
        echo "  The NDI SDK must be installed manually:"
        echo "  1. Download from https://ndi.video/sdk/"
        echo "  2. Extract and copy libndi.so* to /usr/local/lib/"
        echo "  3. Run: sudo ldconfig"
        echo "  4. Re-run this setup script."
        exit 1
    fi
fi

# Ensure library path is configured
echo "/usr/local/lib" > /etc/ld.so.conf.d/ndi.conf
ldconfig

# ── Step 5: Python virtual environment ────────────────────────────────

header "Step 5/9 — Python environment"

info "Creating virtual environment at $VENV_DIR..."
sudo -u "$ACTUAL_USER" python3 -m venv "$VENV_DIR" --system-site-packages

info "Installing Python packages..."
sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/pip" install --upgrade pip > /dev/null 2>&1
sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/pip" install \
    numpy \
    opencv-python-headless \
    Pillow \
    flask \
    > /dev/null 2>&1

log "Python packages installed."

# ── Step 6: Copy project files ────────────────────────────────────────

header "Step 6/9 — Project files"

if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    info "Copying project to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    # Copy all project files
    cp -r "$SCRIPT_DIR"/*.py "$INSTALL_DIR/" 2>/dev/null || true
    cp -r "$SCRIPT_DIR"/*.sh "$INSTALL_DIR/" 2>/dev/null || true
    cp -r "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/" 2>/dev/null || true
    cp -r "$SCRIPT_DIR"/templates "$INSTALL_DIR/" 2>/dev/null || true
    cp -r "$SCRIPT_DIR"/static "$INSTALL_DIR/" 2>/dev/null || true
    cp -r "$SCRIPT_DIR"/scripts "$INSTALL_DIR/" 2>/dev/null || true
    chown -R "$ACTUAL_USER:$ACTUAL_USER" "$INSTALL_DIR"
    log "Project files copied to $INSTALL_DIR"
else
    log "Project already in place at $INSTALL_DIR"
fi

# Fix line endings (in case files were edited on Windows)
find "$INSTALL_DIR" -name "*.py" -o -name "*.sh" -o -name "*.html" | xargs dos2unix -q 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
log "Line endings and permissions fixed."

# ── Step 6b: Runtime directories and file permissions ────────────────

header "Step 6b/9 — Runtime permissions"

# Create directories the web panel will write to
mkdir -p "$INSTALL_DIR/static/images"

# Pre-create runtime files so the web panel (running as user) can write to them
for RUNTIME_FILE in config.json multiview_layout.json start_multiview.sh; do
    FPATH="$INSTALL_DIR/$RUNTIME_FILE"
    if [ ! -f "$FPATH" ]; then
        touch "$FPATH"
        log "Created: $RUNTIME_FILE"
    fi
done
chmod 755 "$INSTALL_DIR/start_multiview.sh" 2>/dev/null || true

# Fix ownership of everything (handles files created by previous sudo runs)
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$INSTALL_DIR"
log "All runtime files owned by $ACTUAL_USER."

# ── Step 7: Sudoers for passwordless operations ──────────────────────

header "Step 7/9 — Sudoers"

SUDOERS_FILE="/etc/sudoers.d/ndi-player"
cat > "$SUDOERS_FILE" << EOF
# NDI HDMI Player — allow web panel to manage services and boot config
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/systemd/system/*
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /boot/firmware/cmdline.txt
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /boot/firmware/config.txt
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/cat /boot/firmware/cmdline.txt
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/cat /boot/firmware/config.txt
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/rm -f /etc/systemd/system/ndi-*
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/sbin/reboot
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/sbin/shutdown
$ACTUAL_USER ALL=(ALL) NOPASSWD: /usr/bin/raspi-config
EOF
chmod 440 "$SUDOERS_FILE"
log "Sudoers configured for passwordless service management."

# ── Step 8: Web panel service ─────────────────────────────────────────

header "Step 8/9 — Web panel service"

WEBPANEL_SVC="/etc/systemd/system/ndi-webpanel.service"
cat > "$WEBPANEL_SVC" << EOF
[Unit]
Description=NDI Web Control Panel
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
Group=$ACTUAL_USER
WorkingDirectory=$INSTALL_DIR
Environment=LD_LIBRARY_PATH=/usr/local/lib
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/ndi_web.py --port 5000
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ndi-webpanel.service
systemctl start ndi-webpanel.service
log "Web panel installed and started."

# ── Step 9: Verification ─────────────────────────────────────────────

header "Step 9/9 — Verification"

PASS=0; FAIL=0

# NDI library
if sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/python3" -c "
import sys; sys.path.insert(0, '$INSTALL_DIR')
import ndi_ctypes as ndi
ndi.initialize()
print('  NDI SDK version:', ndi.version())
ndi.destroy()
" 2>/dev/null; then
    log "NDI library: OK"; ((PASS++))
else
    error "NDI library: FAILED"; ((FAIL++))
fi

# OpenCV
if sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/python3" -c "import cv2; print('  OpenCV:', cv2.__version__)" 2>/dev/null; then
    log "OpenCV: OK"; ((PASS++))
else
    error "OpenCV: FAILED"; ((FAIL++))
fi

# Flask
if sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/python3" -c "import flask; print('  Flask:', flask.__version__)" 2>/dev/null; then
    log "Flask: OK"; ((PASS++))
else
    error "Flask: FAILED"; ((FAIL++))
fi

# SDL2
if sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/python3" -c "
import ctypes
sdl = ctypes.CDLL('libSDL2-2.0.so.0')
print('  SDL2: loaded')
" 2>/dev/null; then
    log "SDL2: OK"; ((PASS++))
else
    error "SDL2: FAILED"; ((FAIL++))
fi

# Web panel reachable
sleep 2
if curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/ | grep -q "200"; then
    log "Web panel: OK (running on port 5000)"; ((PASS++))
else
    warn "Web panel: not responding yet (may need a moment)"; ((FAIL++))
fi

# ── Summary ──────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║          Installation Complete!               ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  Hostname:    $NEW_HOSTNAME"
echo "  Install dir: $INSTALL_DIR"
echo "  Web panel:   http://$NEW_HOSTNAME.local:5000"
echo "  Tests:       $PASS passed, $FAIL failed"
echo ""
echo "  ── Next steps ──"
echo ""
echo "  1. Open http://$NEW_HOSTNAME.local:5000 in your browser"
echo "  2. Click 'Scan Network' to find NDI sources"
echo "  3. Select a source → set resolution → Start HDMI output"
echo "  4. Enable 'Autostart on boot' for dedicated playback"
echo "  5. In System Settings:"
echo "     - Set boot mode to 'CLI + autologin'"
echo "     - Enable 'Web Panel autostart'"
echo "  6. Reboot — the Pi becomes a dedicated NDI player"
echo ""
if [[ $FAIL -gt 0 ]]; then
    warn "Some tests failed. Check the output above."
fi
log "Done!"
