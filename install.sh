#!/bin/bash
# Build the .deb from this checkout and install it. Run: sudo bash install.sh
# (The same package is in the CardputerZero AppStore as "Battery Saver".)
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo'; exit 1; }
cd "$(dirname "$0")"

# Migrate from the old non-packaged install (cardzero-idle): keep its settings.
if [ -e /etc/systemd/system/cardzero-idle.service ] || [ -e /usr/local/sbin/cardzero-idle.py ]; then
  echo 'Removing the old cardzero-idle install...'
  systemctl disable --now cardzero-idle.service 2>/dev/null || true
  if [ -f /etc/default/cardzero-idle ]; then
    install -d -o 1000 -g 1000 -m 0755 /var/lib/cz-battery-saver
    { echo '# Migrated from /etc/default/cardzero-idle'
      grep -E '^(IDLE_DELAY|LOW_BATTERY_PERCENT|IDLE_AUDIO|IDLE_STOP_LAUNCHER|USB_AUTOSUSPEND|AUDIO_ALWAYS)=' /etc/default/cardzero-idle || true
    } > /var/lib/cz-battery-saver/settings
    chown 1000:1000 /var/lib/cz-battery-saver/settings
  fi
  rm -f /usr/local/sbin/cardzero-idle.py /etc/systemd/system/cardzero-idle.service /etc/default/cardzero-idle \
        /etc/sudoers.d/cardzero-idle /usr/share/APPLaunch/applications/cardzero-idle.desktop \
        /usr/share/APPLaunch/share/images/cardzero-idle.png
  rm -rf /usr/local/lib/cardzero-idle /usr/share/APPLaunch/apps/cardzero-idle
  systemctl daemon-reload
fi

bash build-deb.sh
apt-get install -y "./build/$(ls -t build | grep '^cz-battery-saver_.*_all\.deb$' | head -1)"
sleep 2
systemctl --no-pager status cz-battery-saver.service | head -12
echo 'INSTALLED. Logs: journalctl -u cz-battery-saver -f   Uninstall: sudo apt remove cz-battery-saver'
