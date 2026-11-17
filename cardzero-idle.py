#!/usr/bin/python3
"""CardputerZero deep-idle daemon.

When the launcher turns the backlight off (brightness 0) for IDLE_DELAY
seconds, cut the power the device keeps burning with the screen off:

  * launcher audio stream  -> moved to a null sink, so the real sound card
                              (ES8389 + I2S DMA) suspends          (~40 mA)
  * launcher process       -> SIGSTOP, it keeps redrawing the LCD
                              over SPI even with the backlight off  (~30 mA)
  * USB host (dwc2)        -> unbound, hub + SR9900A suspend and the
                              8000 IRQ/s SOF storm stops            (~90 mA)

Any key on the keyboard (or the backlight coming back on) restores
everything; the launcher then reads the queued key and wakes the screen.

Also shuts the system down cleanly when the battery is discharging and at
or below LOW_BATTERY_PERCENT.

Runs as root (dwc2 unbind needs it). Every step is best-effort and is undone
on wake, on exit and at startup (in case a previous run crashed).
"""

import glob
import json
import os
import select
import signal
import struct
import subprocess
import sys
import time

def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

def env_bool(name, default):
    return os.environ.get(name, '1' if default else '0').strip().lower() in ('1', 'yes', 'true', 'on')

IDLE_DELAY = env_int('IDLE_DELAY', 5)                 # s of backlight 0 before idling
LOW_BATTERY_PERCENT = env_int('LOW_BATTERY_PERCENT', 8)  # 0 disables
LOW_BATTERY_CONFIRM = env_int('LOW_BATTERY_CONFIRM', 3)  # consecutive 20 s checks
DO_AUDIO = env_bool('IDLE_AUDIO', True)
DO_LAUNCHER = env_bool('IDLE_STOP_LAUNCHER', True)
DO_USB = env_bool('IDLE_USB', True)

USER = 'pi'
UID = 1000
BACKLIGHT = '/sys/class/backlight/backlight/brightness'
BATTERY = '/sys/class/power_supply/bq27220-0'
LAUNCHER_PATH = '/usr/share/APPLaunch/bin/M5CardputerZero-APPLaunch'
KEYBOARD_NAMES = ('tca8418c',)
DWC2_DRIVER = '/sys/bus/platform/drivers/dwc2'
DWC2_DEV = '3f980000.usb'
# Devices that are always on the internal bus: root hub, GL852G hub, SR9900A NIC.
KNOWN_USB = {('1d6b', '0002'), ('05e3', '0610'), ('0fe6', '9900')}
NULL_SINK = 'cardzero_idle_null'

EVENT_FMT = 'llHHi'          # struct input_event on 64-bit
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY = 1


def log(*a):
    print(*a, flush=True)


def read(path, default=''):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def write(path, value):
    with open(path, 'w') as f:
        f.write(value)


def run(args, timeout=10, as_user=False):
    if as_user and os.geteuid() == 0:
        args = ['runuser', '-u', USER, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{UID}'] + args
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log('command failed:', ' '.join(args), e)
        return -1, ''


# ---------------------------------------------------------------- launcher

def launcher_pid():
    for exe in glob.glob('/proc/[0-9]*/exe'):
        try:
            if os.readlink(exe) == LAUNCHER_PATH:
                return int(exe.split('/')[2])
        except OSError:
            pass
    return None


def has_children(pid):
    for f in glob.glob(f'/proc/{pid}/task/*/children'):
        if read(f):
            return True
    return False


def proc_state(pid):
    stat = read(f'/proc/{pid}/stat')
    return stat.rsplit(')', 1)[1].split()[0] if ')' in stat else ''


# ------------------------------------------------------------------- audio

def pactl_json(kind):
    rc, out = run(['pactl', '-f', 'json', 'list', kind], as_user=True)
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def sink_names():
    return {s['index']: s['name'] for s in pactl_json('sinks')}


def audio_park(pid):
    """Move the launcher's streams to a null sink. Returns {stream: sink_name}."""
    streams = [s for s in pactl_json('sink-inputs')
               if str(s.get('properties', {}).get('application.process.id')) == str(pid)]
    if not streams:
        return {}
    names = sink_names()
    if NULL_SINK not in names.values():
        run(['pactl', 'load-module', 'module-null-sink', f'sink_name={NULL_SINK}'], as_user=True)
    moved = {}
    for s in streams:
        orig = names.get(s['sink'])
        if orig and orig != NULL_SINK:
            rc, _ = run(['pactl', 'move-sink-input', str(s['index']), NULL_SINK], as_user=True)
            if rc == 0:
                moved[s['index']] = orig
    return moved


def audio_restore(moved):
    names = sink_names()
    default = None
    for s in pactl_json('sinks'):
        if s['name'] != NULL_SINK:
            default = s['name']
            break
    for s in pactl_json('sink-inputs'):
        if names.get(s['sink']) == NULL_SINK:
            target = moved.get(s['index']) or default
            if target:
                run(['pactl', 'move-sink-input', str(s['index']), target], as_user=True)
    # pactl's JSON module list has no index, use the short format.
    _, out = run(['pactl', 'list', 'short', 'modules'], as_user=True)
    for line in out.splitlines():
        cols = line.split('\t')
        if len(cols) >= 3 and cols[1] == 'module-null-sink' and f'sink_name={NULL_SINK}' in cols[2]:
            run(['pactl', 'unload-module', cols[0]], as_user=True)


# --------------------------------------------------------------------- usb

def usb_bound():
    return os.path.exists(os.path.join(DWC2_DRIVER, DWC2_DEV))


def usb_idle_ok():
    """Only cut USB if nothing but the internal hub + NIC is there and no cable."""
    if read('/sys/class/net/eth0/carrier', '0') == '1':
        return False
    if not glob.glob('/sys/bus/usb/devices/usb*'):
        return False          # not in host mode (gadget / Slave), leave it alone
    for d in glob.glob('/sys/bus/usb/devices/*'):
        vid, pid = read(d + '/idVendor'), read(d + '/idProduct')
        if vid and (vid, pid) not in KNOWN_USB:
            return False
    return True


def usb_off():
    write(os.path.join(DWC2_DRIVER, 'unbind'), DWC2_DEV)


def usb_on():
    if not usb_bound():
        write(os.path.join(DWC2_DRIVER, 'bind'), DWC2_DEV)


# ---------------------------------------------------------------- keyboard

def open_keyboards():
    fds = []
    for d in glob.glob('/sys/class/input/event*'):
        if read(d + '/device/name') in KEYBOARD_NAMES:
            try:
                fds.append(os.open('/dev/input/' + os.path.basename(d), os.O_RDONLY | os.O_NONBLOCK))
            except OSError as e:
                log('cannot open keyboard', d, e)
    return fds


def drain_keys(fds, timeout):
    """Wait up to timeout s; return True if a key went down."""
    if not fds:
        time.sleep(timeout)
        return False
    r, _, _ = select.select(fds, [], [], timeout)
    pressed = False
    for fd in r:
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except OSError:
            continue
        for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, etype, _, value = struct.unpack_from(EVENT_FMT, data, i)
            if etype == EV_KEY and value == 1:
                pressed = True
    return pressed


# -------------------------------------------------------------------- main

class Idle:
    def __init__(self):
        self.active = False
        self.stopped_pid = None
        self.moved = {}
        self.usb_cut = False

    def enter(self):
        pid = launcher_pid()
        busy = pid is None or has_children(pid)   # an app is in front: be gentle
        steps = []
        if DO_AUDIO and pid:
            try:
                self.moved = audio_park(pid)
                if self.moved:
                    steps.append('audio')
            except Exception as e:
                log('audio park failed:', e)
        if DO_USB and usb_idle_ok():
            try:
                usb_off()
                self.usb_cut = True
                steps.append('usb')
            except OSError as e:
                log('usb off failed:', e)
        if DO_LAUNCHER and pid and not busy:
            try:
                os.kill(pid, signal.SIGSTOP)
                self.stopped_pid = pid
                steps.append('launcher')
            except OSError as e:
                log('stop launcher failed:', e)
        self.active = True
        log('idle: ' + (', '.join(steps) or 'nothing to do'))

    def leave(self, why):
        if self.stopped_pid:
            try:
                os.kill(self.stopped_pid, signal.SIGCONT)
            except OSError:
                pass
            self.stopped_pid = None
        if self.moved:
            try:
                audio_restore(self.moved)
            except Exception as e:
                log('audio restore failed:', e)
            self.moved = {}
        if self.usb_cut:
            try:
                usb_on()
            except OSError as e:
                log('usb on failed:', e)
            self.usb_cut = False
        self.active = False
        log('wake:', why)


def cleanup_leftovers():
    """Undo anything a crashed previous run may have left behind."""
    pid = launcher_pid()
    if pid and proc_state(pid) == 'T':
        os.kill(pid, signal.SIGCONT)
        log('resumed stopped launcher')
    try:
        audio_restore({})
    except Exception as e:
        log('audio cleanup failed:', e)
    try:
        if not usb_bound():
            usb_on()
            log('re-bound dwc2')
    except OSError as e:
        log('usb cleanup failed:', e)


def battery_low():
    if LOW_BATTERY_PERCENT <= 0:
        return False
    if read(BATTERY + '/status') != 'Discharging':
        return False
    try:
        return int(read(BATTERY + '/capacity', '100')) <= LOW_BATTERY_PERCENT
    except ValueError:
        return False


def main():
    idle = Idle()

    def on_term(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)

    cleanup_leftovers()
    keyboards = open_keyboards()
    log(f'started: delay={IDLE_DELAY}s low_battery={LOW_BATTERY_PERCENT}% '
        f'audio={DO_AUDIO} launcher={DO_LAUNCHER} usb={DO_USB} keyboards={len(keyboards)}')

    dark_since = None
    low_count = 0
    next_battery = 0.0
    next_keyboard_scan = time.monotonic() + 5
    try:
        while True:
            key = drain_keys(keyboards, 1.0)
            now = time.monotonic()
            if not keyboards and now >= next_keyboard_scan:
                # Early in boot the keyboard may not exist yet; keep looking.
                next_keyboard_scan = now + 5
                keyboards = open_keyboards()
                if keyboards:
                    log(f'keyboards={len(keyboards)}')
            bright = read(BACKLIGHT, '1') != '0'

            if idle.active:
                if key or bright:
                    idle.leave('key' if key else 'backlight on')
                    dark_since = None
            else:
                if bright or key:
                    dark_since = None
                elif dark_since is None:
                    dark_since = now
                elif now - dark_since >= IDLE_DELAY:
                    idle.enter()

            if now >= next_battery:
                next_battery = now + 20
                low_count = low_count + 1 if battery_low() else 0
                if low_count >= LOW_BATTERY_CONFIRM:
                    log(f'battery at {read(BATTERY + "/capacity")}%, shutting down')
                    if idle.active:
                        idle.leave('shutdown')
                    run(['systemctl', 'poweroff'])
                    low_count = -10**9   # don't repeat
    finally:
        if idle.active:
            idle.leave('exit')


if __name__ == '__main__':
    sys.exit(main())
