#!/usr/bin/env bash
# ============================================================================
# NDI Installation Script for Raspberry Pi OS
# Supports: Raspberry Pi 4/5, armhf and aarch64
# ============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!!]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; }
info()  { echo -e "${BLUE}[i]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
    exit 1
fi

ARCH=$(dpkg --print-architecture)
ACTUAL_USER="${SUDO_USER:-$USER}"
HOME_DIR="/home/$ACTUAL_USER"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME_DIR/ndi-env"

echo ""
echo "============================================"
echo "  NDI Installer for Raspberry Pi OS"
echo "  Architecture: $ARCH"
echo "============================================"
echo ""

# ── Step 1: System packages ──────────────────────────────────────────

info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    build-essential cmake pkg-config \
    python3 python3-pip python3-venv python3-dev python3-numpy \
    libavahi-client-dev avahi-daemon avahi-utils \
    ffmpeg libopencv-dev python3-opencv \
    v4l-utils wget curl unzip dos2unix \
    > /dev/null 2>&1
log "System packages installed."

# ── Step 2: Avahi (mDNS) ─────────────────────────────────────────────

systemctl enable avahi-daemon 2>/dev/null || true
systemctl start avahi-daemon 2>/dev/null || true
log "Avahi mDNS service active."

# ── Step 3: Install NDI SDK native library ────────────────────────────

if ldconfig -p | grep -q libndi; then
    log "NDI native library already installed."
    ldconfig -p | grep libndi
else
    info "Installing NDI SDK native library..."
    if [[ -f "$SCRIPT_DIR/install_ndi_sdk.sh" ]]; then
        bash "$SCRIPT_DIR/install_ndi_sdk.sh"
    else
        error "install_ndi_sdk.sh not found in $SCRIPT_DIR"
        error "Please run: sudo bash install_ndi_sdk.sh  first."
        exit 1
    fi
fi

# ── Step 4: Python virtual environment ────────────────────────────────

info "Creating Python virtual environment at $VENV_DIR..."
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

# ── Step 5: Fix line endings on project files ─────────────────────────

info "Fixing line endings..."
find "$SCRIPT_DIR" -name "*.py" -o -name "*.sh" -o -name "*.html" | xargs dos2unix -q 2>/dev/null || true
log "Line endings fixed."

# ── Step 6: Library path config ───────────────────────────────────────

echo "/usr/local/lib" > /etc/ld.so.conf.d/ndi.conf
ldconfig
log "Library paths configured."

# ── Step 7: Firewall (if active) ─────────────────────────────────────

if command -v ufw &> /dev/null && ufw status | grep -q "active"; then
    ufw allow 5353/udp comment "NDI mDNS" > /dev/null 2>&1 || true
    ufw allow 5960:5990/tcp comment "NDI streams" > /dev/null 2>&1 || true
    ufw allow 5960:5990/udp comment "NDI streams" > /dev/null 2>&1 || true
    ufw allow 5000/tcp comment "NDI Web Panel" > /dev/null 2>&1 || true
    log "Firewall rules added."
fi

# ── Step 8: Verify ───────────────────────────────────────────────────

echo ""
info "Verifying installation..."

# Test NDI library loads
if sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/python3" -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import ndi_ctypes as ndi
ndi.initialize()
print('NDI version:', ndi.version())
ndi.destroy()
" 2>/dev/null; then
    log "NDI library loads correctly!"
else
    warn "NDI library test failed. Check that install_ndi_sdk.sh completed."
fi

# Test OpenCV
if sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/python3" -c "import cv2; print('OpenCV:', cv2.__version__)" 2>/dev/null; then
    log "OpenCV working!"
fi

# Test Flask
if sudo -u "$ACTUAL_USER" "$VENV_DIR/bin/python3" -c "import flask; print('Flask:', flask.__version__)" 2>/dev/null; then
    log "Flask working!"
fi

# ── Done ──────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo -e "  ${GREEN}Installation Complete!${NC}"
echo "============================================"
echo ""
echo "  Virtual environment: $VENV_DIR"
echo "  Activate with:       source $VENV_DIR/bin/activate"
echo ""
echo "  Start the web panel:"
echo "    source $VENV_DIR/bin/activate"
echo "    cd $SCRIPT_DIR"
echo "    python3 ndi_web.py"
echo ""
echo "  Then open http://$(hostname).local:5000 in your browser"
echo ""
log "Done!"
