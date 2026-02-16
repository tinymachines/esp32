#!/bin/bash
# Install the esp-camera systemd service.
# Usage: sudo bash deploy/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE=esp-camera

# Create logs directory
mkdir -p "${PROJECT_DIR}/camera/logs"
chown bisenbek:bisenbek "${PROJECT_DIR}/camera/logs"

# Copy service file
cp "${SCRIPT_DIR}/${SERVICE}.service" /etc/systemd/system/

# Reload, enable, start
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo "Service ${SERVICE} installed and started."
echo "Check status: systemctl status ${SERVICE}"
echo "View logs:    tail -f ${PROJECT_DIR}/camera/logs/app.log"

# Install nginx config if nginx is present
NGINX_CONF="esp.tinymachines.ai"
if command -v nginx &>/dev/null; then
    cp "${SCRIPT_DIR}/${NGINX_CONF}.nginx" "/etc/nginx/sites-available/${NGINX_CONF}"
    ln -sf "/etc/nginx/sites-available/${NGINX_CONF}" "/etc/nginx/sites-enabled/${NGINX_CONF}"
    nginx -t && systemctl reload nginx
    echo "Nginx config installed: ${NGINX_CONF}"
fi
