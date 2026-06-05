#!/usr/bin/env bash
# Setup NDI sender/receiver as systemd services
# Usage: sudo ./setup_services.sh [sender|receiver|both]

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 $*"
    exit 1
fi

MODE="${1:-both}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$SCRIPT_DIR"
ACTUAL_USER="${SUDO_USER:-pi}"
HOME_DIR="/home/$ACTUAL_USER"

# Update paths in service files to match actual user
update_service() {
    local file="$1"
    local tmp="/tmp/$(basename "$file")"
    sed "s|/home/pi|$HOME_DIR|g; s|User=pi|User=$ACTUAL_USER|g; s|Group=pi|Group=$ACTUAL_USER|g" \
        "$file" > "$tmp"
    cp "$tmp" "/etc/systemd/system/$(basename "$file")"
    rm "$tmp"
}

install_sender() {
    echo "[i] Installing NDI sender service..."
    update_service "$SERVICE_DIR/ndi-sender.service"
    systemctl daemon-reload
    systemctl enable ndi-sender.service
    echo "[✓] NDI sender service installed."
    echo "    Start:  sudo systemctl start ndi-sender"
    echo "    Stop:   sudo systemctl stop ndi-sender"
    echo "    Logs:   journalctl -u ndi-sender -f"
    echo ""
}

install_receiver() {
    echo "[i] Installing NDI receiver service..."
    update_service "$SERVICE_DIR/ndi-receiver.service"
    systemctl daemon-reload
    systemctl enable ndi-receiver.service
    echo "[✓] NDI receiver service installed."
    echo "    Start:  sudo systemctl start ndi-receiver"
    echo "    Stop:   sudo systemctl stop ndi-receiver"
    echo "    Logs:   journalctl -u ndi-receiver -f"
    echo ""
}

install_web() {
    echo "[i] Installing NDI web control panel service..."
    update_service "$SERVICE_DIR/ndi-web.service"
    systemctl daemon-reload
    systemctl enable ndi-web.service
    echo "[✓] NDI web panel service installed."
    echo "    Start:  sudo systemctl start ndi-web"
    echo "    Stop:   sudo systemctl stop ndi-web"
    echo "    Logs:   journalctl -u ndi-web -f"
    echo "    URL:    http://$(hostname).local:5000"
    echo ""
}

case "$MODE" in
    sender)   install_sender ;;
    receiver) install_receiver ;;
    web)      install_web ;;
    both)     install_sender; install_receiver ;;
    all)      install_sender; install_receiver; install_web ;;
    *)
        echo "Usage: sudo $0 [sender|receiver|web|both|all]"
        exit 1
        ;;
esac

echo "[✓] Done. Edit the .service files in /etc/systemd/system/ to customize."
echo "    Then: sudo systemctl daemon-reload && sudo systemctl restart <service>"
