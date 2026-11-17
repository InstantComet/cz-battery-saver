#!/bin/bash
# Install cardzero-idle. Run: sudo bash ~/cardzero-idle/install.sh
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo'; exit 1; }
cd "$(dirname "$0")"
python3 -m py_compile cardzero-idle.py
install -m 0755 cardzero-idle.py /usr/local/sbin/cardzero-idle.py
install -m 0644 cardzero-idle.service /etc/systemd/system/cardzero-idle.service
[ -f /etc/default/cardzero-idle ] || install -m 0644 cardzero-idle.default /etc/default/cardzero-idle
systemctl daemon-reload
systemctl enable --now cardzero-idle.service
systemctl restart cardzero-idle.service
sleep 2
systemctl --no-pager status cardzero-idle.service | head -12
echo 'INSTALLED. Logs: journalctl -u cardzero-idle -f   Uninstall: sudo bash uninstall.sh'
