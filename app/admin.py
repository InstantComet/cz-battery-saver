#!/usr/bin/python3
"""Root helper for the Idle Saver app. Accepts only exact, validated actions:

  admin.py enable | disable
  admin.py set KEY VALUE      (KEY/VALUE from the ALLOWED table below)

Installed root-owned at /usr/local/lib/cardzero-idle/admin.py and allowed for
user pi without a password by /etc/sudoers.d/cardzero-idle.
"""
import os
import re
import subprocess
import sys

CONFIG = '/etc/default/cardzero-idle'
UNIT = 'cardzero-idle.service'
ALLOWED = {
    'IDLE_DELAY': {'3', '5', '10', '20', '30', '60'},
    'LOW_BATTERY_PERCENT': {'0', '5', '8', '10', '15', '20'},
    'IDLE_AUDIO': {'0', '1'},
    'IDLE_STOP_LAUNCHER': {'0', '1'},
    'USB_AUTOSUSPEND': {'0', '1'},
    'AUDIO_ALWAYS': {'0', '1'},
}


def systemctl(*args):
    return subprocess.run(['/usr/bin/systemctl', *args], timeout=30,
                          env={'PATH': '/usr/bin:/usr/sbin', 'LANG': 'C'}).returncode


def set_value(key, value):
    try:
        with open(CONFIG) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, done = [], False
    for line in lines:
        if re.match(rf'\s*{key}\s*=', line):
            if not done:
                out.append(f'{key}={value}')
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f'{key}={value}')
    temporary = CONFIG + '.tmp'
    with open(temporary, 'w') as f:
        f.write('\n'.join(out) + '\n')
    os.chmod(temporary, 0o644)
    os.replace(temporary, CONFIG)
    return systemctl('try-restart', UNIT)


args = sys.argv[1:]
if args == ['enable']:
    rc = systemctl('enable', '--now', UNIT)
elif args == ['disable']:
    rc = systemctl('disable', '--now', UNIT)
elif len(args) == 3 and args[0] == 'set' and args[2] in ALLOWED.get(args[1], ()):
    rc = set_value(args[1], args[2])
else:
    sys.exit('Unsupported action')
sys.exit(rc)
