#!/bin/bash
# Install cardzero-idle and its "Idle Saver" launcher app. Run: sudo bash install.sh
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo'; exit 1; }
cd "$(dirname "$0")"
python3 -m py_compile cardzero-idle.py app/app.py app/admin.py

# Daemon
install -m 0755 cardzero-idle.py /usr/local/sbin/cardzero-idle.py
install -m 0644 cardzero-idle.service /etc/systemd/system/cardzero-idle.service
if [ -f /etc/default/cardzero-idle ]; then
  sed -i '/^IDLE_USB=/d' /etc/default/cardzero-idle   # replaced by USB_AUTOSUSPEND
else
  install -m 0644 cardzero-idle.default /etc/default/cardzero-idle
fi

# Launcher app (needs the stock APPLaunch launcher)
if [ -d /usr/share/APPLaunch/applications ]; then
  visudo -cf app/sudoers >/dev/null
  install -d -m 0755 /usr/local/lib/cardzero-idle /usr/share/APPLaunch/apps/cardzero-idle
  install -m 0755 app/admin.py /usr/local/lib/cardzero-idle/admin.py
  install -m 0644 app/app.py /usr/share/APPLaunch/apps/cardzero-idle/app.py
  install -m 0644 app/cardzero-idle.desktop /usr/share/APPLaunch/applications/cardzero-idle.desktop
  install -m 0644 app/icon.png /usr/share/APPLaunch/share/images/cardzero-idle.png
  install -m 0440 app/sudoers /etc/sudoers.d/cardzero-idle
  echo 'Idle Saver app installed (restart the launcher if it does not show up).'
fi

systemctl daemon-reload
systemctl enable cardzero-idle.service
systemctl restart cardzero-idle.service
sleep 2
systemctl --no-pager status cardzero-idle.service | head -12
echo 'INSTALLED. Logs: journalctl -u cardzero-idle -f   Uninstall: sudo bash uninstall.sh'
