#!/bin/bash
# Remove cz-battery-saver. Same as: sudo apt remove cz-battery-saver
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo'; exit 1; }
# Stopping the service (prerm) restores the launcher and audio if it was idle.
apt-get remove -y cz-battery-saver
echo 'UNINSTALLED. USB autosuspend stays on until the next reboot.'
