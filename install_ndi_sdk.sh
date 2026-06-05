#!/usr/bin/env bash
# ============================================================================
# Download and install the NDI SDK native library for Raspberry Pi
# This installs libndi.so which is required by all the Python scripts.
# ============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!!]${NC} $1"; }
info()  { echo -e "${BLUE}[i]${NC} $1"; }
error() { echo -e "${RED}[ERR]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
    error "Run with sudo: sudo bash $0"
    exit 1
fi

ARCH=$(dpkg --print-architecture)
info "Architecture: $ARCH"

WORK_DIR="/tmp/ndi-sdk-install"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# ── Step 1: Download the NDI SDK ──────────────────────────────────────

SDK_URL="https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz"

info "Downloading NDI SDK for Linux..."
if ! wget -q --show-progress -O ndi_sdk.tar.gz "$SDK_URL"; then
    error "Download failed. The NDI SDK URL may have changed."
    echo ""
    echo "  Please download it manually from: https://ndi.video/sdk/"
    echo "  Then extract libndi.so to /usr/local/lib/"
    echo ""
    exit 1
fi

log "Download complete."

# ── Step 2: Extract the SDK ───────────────────────────────────────────

info "Extracting..."
tar -xzf ndi_sdk.tar.gz

# The archive contains an installer shell script — run it silently
# It will ask to accept the license; we pass 'y'
INSTALLER=$(find . -maxdepth 1 -name "Install_NDI_SDK_*_Linux.sh" | head -1)

if [[ -z "$INSTALLER" ]]; then
    # Maybe it extracted directly
    INSTALLER=$(find . -maxdepth 1 -name "Install_NDI_SDK_*" -type f | head -1)
fi

if [[ -z "$INSTALLER" ]]; then
    error "Could not find the NDI installer script in the archive."
    echo "  Contents of $WORK_DIR:"
    ls -la
    exit 1
fi

chmod +x "$INSTALLER"
info "Running NDI SDK installer (auto-accepting license)..."
yes | PAGER="cat" bash "$INSTALLER" > /dev/null 2>&1 || true

# Find the extracted SDK directory
SDK_DIR=$(find . -maxdepth 2 -type d -name "NDI SDK for Linux" 2>/dev/null | head -1)
if [[ -z "$SDK_DIR" ]]; then
    SDK_DIR=$(find /tmp -maxdepth 3 -type d -name "NDI SDK for Linux" 2>/dev/null | head -1)
fi
if [[ -z "$SDK_DIR" ]]; then
    SDK_DIR=$(find "$HOME" -maxdepth 3 -type d -name "NDI SDK for Linux" 2>/dev/null | head -1)
fi

if [[ -z "$SDK_DIR" ]]; then
    warn "SDK directory not found in expected location. Searching..."
    SDK_DIR=$(find / -maxdepth 4 -type d -name "NDI SDK for Linux" 2>/dev/null | head -1)
fi

if [[ -z "$SDK_DIR" ]]; then
    error "Could not find extracted NDI SDK directory."
    exit 1
fi

info "Found SDK at: $SDK_DIR"

# ── Step 3: Install the native library ────────────────────────────────

# Find the correct library for our architecture
if [[ "$ARCH" == "arm64" ]]; then
    LIB_DIRS=(
        "$SDK_DIR/lib/aarch64-rpi4-linux-gnueabi"
        "$SDK_DIR/lib/aarch64-linux-gnu"
        "$SDK_DIR/lib/aarch64-rpi5-linux-gnueabi"
    )
elif [[ "$ARCH" == "armhf" ]]; then
    LIB_DIRS=(
        "$SDK_DIR/lib/arm-rpi4-linux-gnueabihf"
        "$SDK_DIR/lib/arm-linux-gnueabihf"
        "$SDK_DIR/lib/arm-rpi3-linux-gnueabihf"
    )
else
    LIB_DIRS=(
        "$SDK_DIR/lib/x86_64-linux-gnu"
    )
fi

NDI_LIB=""
for dir in "${LIB_DIRS[@]}"; do
    if [[ -d "$dir" ]]; then
        NDI_LIB=$(find "$dir" -name "libndi.so*" | head -1)
        if [[ -n "$NDI_LIB" ]]; then
            info "Found NDI library in: $dir"
            break
        fi
    fi
done

if [[ -z "$NDI_LIB" ]]; then
    warn "Could not find library for $ARCH in standard paths."
    info "Available library directories:"
    find "$SDK_DIR/lib" -maxdepth 1 -type d 2>/dev/null
    echo ""
    info "Searching for any libndi.so..."
    NDI_LIB=$(find "$SDK_DIR" -name "libndi.so*" | head -1)
fi

if [[ -z "$NDI_LIB" ]]; then
    error "No libndi.so found in the SDK. Cannot continue."
    exit 1
fi

LIB_DIR=$(dirname "$NDI_LIB")
info "Installing libraries from: $LIB_DIR"

# Copy all .so files
cp -v "$LIB_DIR"/libndi.so* /usr/local/lib/

# Create symlinks
cd /usr/local/lib
FULL_LIB=$(ls libndi.so.* 2>/dev/null | grep -v '.so$' | sort -V | tail -1)
if [[ -n "$FULL_LIB" ]]; then
    ln -sf "$FULL_LIB" libndi.so.6  2>/dev/null || true
    ln -sf libndi.so.6 libndi.so    2>/dev/null || true
fi

# ── Step 4: Install headers ──────────────────────────────────────────

if [[ -d "$SDK_DIR/include" ]]; then
    mkdir -p /usr/local/include/ndi
    cp "$SDK_DIR/include/"* /usr/local/include/ndi/ 2>/dev/null || true
    log "Headers installed to /usr/local/include/ndi/"
fi

# ── Step 5: Configure library path and update cache ──────────────────

# Ensure /usr/local/lib is in the ldconfig search path
echo "/usr/local/lib" > /etc/ld.so.conf.d/ndi.conf
ldconfig

# ── Step 6: Verify ───────────────────────────────────────────────────

echo ""
if ldconfig -p | grep -q libndi; then
    log "NDI SDK installed successfully!"
    ldconfig -p | grep libndi
else
    error "libndi not found in library cache. Something went wrong."
    exit 1
fi

# Store SDK path for environment
echo "NDI_SDK_DIR=/usr/local" > /etc/profile.d/ndi.sh
echo "export NDI_SDK_DIR" >> /etc/profile.d/ndi.sh

# ── Cleanup ──────────────────────────────────────────────────────────

cd /
rm -rf "$WORK_DIR"

echo ""
echo "============================================"
echo -e "  ${GREEN}NDI SDK installed!${NC}"
echo "============================================"
echo ""
echo "  Library: $(ldconfig -p | grep 'libndi.so ' | awk '{print $NF}')"
echo ""
log "Done."
