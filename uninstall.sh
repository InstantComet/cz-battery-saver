#!/bin/bash
# Remove cardzero-idle. Run: sudo bash uninstall.sh
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo'; exit 1; }
# Stopping the service restores the launcher, audio and USB if it was idle.
systemctl disable --now cardzero-idle.service 2>/dev/null || true
rm -f /usr/local/sbin/cardzero-idle.py /etc/systemd/system/cardzero-idle.service /etc/default/cardzero-idle
systemctl daemon-reload
echo 'UNINSTALLED'
