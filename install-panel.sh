#!/bin/bash

# Slipstream-Rust Standalone Management Panel Installer
# This script installs the web-based user management panel for an existing Slipstream-Rust SOCKS server.

set -e

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo -e "\033[0;31m[ERROR]\033[0m This script must be run as root"
    exit 1
fi

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

CONFIG_DIR="/etc/slipstream-rust"
CONFIG_FILE="${CONFIG_DIR}/slipstream-rust-server.conf"
PANEL_DIR="/usr/local/share/slipstream-rust-panel"
SYSTEMD_DIR="/etc/systemd/system"

GITHUB_USER="${GITHUB_USER:-aliazading}"
GITHUB_REPO="${GITHUB_REPO:-slipstream-rust-deploy-pnl}"
GITHUB_BRANCH="${GITHUB_BRANCH:-fix/panel-installation}"
DEPLOY_REPO_URL="${DEPLOY_REPO_URL:-https://github.com/${GITHUB_USER}/${GITHUB_REPO}.git}"

VPN_GROUP="slipstream-users"

print_status() { echo -e "${GREEN}[INFO]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }

# 1. Check for existing installation
if [ ! -f "$CONFIG_FILE" ]; then
    print_error "Slipstream-Rust configuration not found at $CONFIG_FILE"
    print_error "Please install the Slipstream-Rust server first."
    exit 1
fi

# 2. Load existing config
# shellcheck source=/dev/null
. "$CONFIG_FILE"

if [ "$TUNNEL_MODE" != "socks" ]; then
    print_error "Management panel is currently only supported for SOCKS mode."
    exit 1
fi

if [[ "$SOCKS_AUTH_ENABLED" != "yes" ]]; then
    print_error "SOCKS authentication must be enabled to use the management panel."
    exit 1
fi

print_status "Existing Slipstream-Rust installation detected for domain: $DOMAIN"

# 3. Install dependencies
print_status "Installing dependencies..."
if command -v apt &> /dev/null; then
    apt update && apt install -y python3 python3-pip python3-venv git curl
elif command -v dnf &> /dev/null; then
    dnf install -y python3 python3-pip git curl
elif command -v yum &> /dev/null; then
    yum install -y python3 python3-pip git curl
fi

# 4. Create VPN group and ensure admin user is in it
if ! getent group "$VPN_GROUP" >/dev/null; then
    groupadd "$VPN_GROUP"
fi
if id "$SOCKS_USERNAME" &>/dev/null; then
    usermod -a -G "$VPN_GROUP" "$SOCKS_USERNAME"
fi

# 5. Download panel files
print_status "Downloading panel files..."
if [[ -d "./panel" ]]; then
    print_status "Using panel files from current directory."
    mkdir -p "$PANEL_DIR"
    cp -r "./panel"/* "$PANEL_DIR/"
else
    temp_dir="/tmp/slipstream-panel-install"
    rm -rf "$temp_dir"
    if ! git clone --depth 1 -b "$GITHUB_BRANCH" "$DEPLOY_REPO_URL" "$temp_dir" 2>/dev/null && \
       ! git clone --depth 1 "$DEPLOY_REPO_URL" "$temp_dir"; then
        print_error "Failed to clone repository from $DEPLOY_REPO_URL"
        exit 1
    fi

    if [[ -d "$temp_dir/panel" ]]; then
        mkdir -p "$PANEL_DIR"
        cp -r "$temp_dir/panel"/* "$PANEL_DIR/"
        rm -rf "$temp_dir"
    else
        print_error "Panel directory not found in the cloned repository!"
        rm -rf "$temp_dir"
        exit 1
    fi
fi

# 6. Setup Virtual Environment
print_status "Setting up Python virtual environment..."
if [ ! -d "$PANEL_DIR/venv" ]; then
    python3 -m venv "$PANEL_DIR/venv"
fi
"$PANEL_DIR/venv/bin/pip" install -r "$PANEL_DIR/requirements.txt"

# 7. Generate random credentials if not present
if [[ -z "${PANEL_PORT:-}" ]]; then
    PANEL_PORT=$(shuf -i 10000-65000 -n 1)
fi
if [[ -z "${PANEL_SECRET:-}" ]]; then
    PANEL_SECRET=$(openssl rand -hex 12)
fi

# 8. Create systemd service
print_status "Creating systemd service..."
cat > "${SYSTEMD_DIR}/slipstream-panel.service" << EOF
[Unit]
Description=slipstream-rust Management Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PANEL_DIR
Environment="PANEL_PORT=$PANEL_PORT"
Environment="PANEL_PATH=$PANEL_SECRET/panel"
Environment="ADMIN_USER=$SOCKS_USERNAME"
Environment="ADMIN_PASS=$SOCKS_PASSWORD"
ExecStart=$PANEL_DIR/venv/bin/python $PANEL_DIR/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable slipstream-panel
systemctl restart slipstream-panel

# 9. Update config file
print_status "Updating configuration file..."
# Remove existing panel vars if any
sed -i '/PANEL_PORT/d' "$CONFIG_FILE"
sed -i '/PANEL_SECRET/d' "$CONFIG_FILE"
# Append new ones
cat >> "$CONFIG_FILE" << EOF
PANEL_PORT="$PANEL_PORT"
PANEL_SECRET="$PANEL_SECRET"
EOF

# 10. Open firewall
print_status "Updating firewall rules..."
if command -v ufw &> /dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$PANEL_PORT"/tcp
elif command -v firewall-cmd &> /dev/null && systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-port="$PANEL_PORT"/tcp
    firewall-cmd --reload
fi

# 11. Final output
public_ip=$(curl -s https://ipinfo.io/ip || echo "YOUR_SERVER_IP")
echo -e "\n${GREEN}+================================================================================${NC}"
echo -e "${GREEN}|                 PANEL INSTALLATION COMPLETED SUCCESSFULLY!                  |${NC}"
echo -e "${GREEN}+================================================================================${NC}"
echo -e "\n${BLUE}Management Panel Details:${NC}"
echo -e "  URL:        ${YELLOW}http://${public_ip}:${PANEL_PORT}/${PANEL_SECRET}/panel/login${NC}"
echo -e "  Admin User: ${YELLOW}${SOCKS_USERNAME}${NC}"
echo -e "  Admin Pass: ${YELLOW}${SOCKS_PASSWORD}${NC}"
echo -e "\n${BLUE}Management Commands:${NC}"
echo -e "  Status:  ${YELLOW}systemctl status slipstream-panel${NC}"
echo -e "  Restart: ${YELLOW}systemctl restart slipstream-panel${NC}"
echo -e "  Logs:    ${YELLOW}journalctl -u slipstream-panel -f${NC}"
echo -e "\n${GREEN}+================================================================================${NC}\n"
