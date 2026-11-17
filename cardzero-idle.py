#!/usr/bin/python3
"""CardputerZero power-saving daemon.

Always (screen on or off):
  * internal USB Ethernet chip -> runtime autosuspend (power/control=auto).
                              The kernel leaves it "on", which keeps the
                              whole USB bus awake (8000 IRQ/s). With it
                              suspended the hub and root hub follow; hot-plug
                              still works                          (~105 mA)
  * launcher audio stream  -> optionally parked on a null sink even with the
                              screen on (AUDIO_ALWAYS); costs the launcher's
                              key sounds                            (~35 mA)

When the launcher turns the backlight off (brightness 0) for IDLE_DELAY
seconds:
  * launcher audio stream  -> moved to a null sink, so the real sound card
                              (ES8389 + I2S DMA) suspends          (~40 mA)
  * launcher process       -> SIGSTOP, it keeps redrawing the LCD
                              over SPI even with the backlight off  (~30 mA)

Any key on the keyboard (or the backlight coming back on) restores
everything; the launcher then reads the queued key and wakes the screen.

Also shuts the system down cleanly when the battery is discharging and at
or below LOW_BATTERY_PERCENT.

Runs as root (sysfs power control needs it). Every step is best-effort and is undone
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
DO_AUTOSUSPEND = env_bool('USB_AUTOSUSPEND', True)
AUDIO_ALWAYS = env_bool('AUDIO_ALWAYS', False)

USER = 'pi'
UID = 1000
BACKLIGHT = '/sys/class/backlight/backlight/brightness'
BATTERY = '/sys/class/power_supply/bq27220-0'
LAUNCHER_PATH = '/usr/share/APPLaunch/bin/M5CardputerZero-APPLaunch'
KEYBOARD_NAMES = ('tca8418c',)
DWC2_DRIVER = '/sys/bus/platform/drivers/dwc2'
DWC2_DEV = '3f980000.usb'
# Internal USB devices the kernel leaves at power/control=on: SR9900A NIC (r8152).
AUTOSUSPEND_USB = {('0fe6', '9900')}
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
    kw = {}
    if as_user and os.geteuid() == 0:
        # Switch uid directly instead of runuser: no PAM session (and no journal
        # noise) for every pactl call.
        kw = dict(user=UID, group=UID, extra_groups=[],
                  env={'XDG_RUNTIME_DIR': f'/run/user/{UID}', 'HOME': f'/home/{USER}',
                       'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)
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


def usb_autosuspend(enable):
    """Set runtime PM on the internal NIC. Re-run often: it resets on re-enumeration."""
    want = 'auto' if enable else 'on'
    for d in glob.glob('/sys/bus/usb/devices/*'):
        if (read(d + '/idVendor'), read(d + '/idProduct')) in AUTOSUSPEND_USB:
            if read(d + '/power/control') not in ('', want):
                try:
                    write(d + '/power/control', want)
                    log(f'usb {os.path.basename(d)}: power/control={want}')
                except OSError as e:
                    log('usb autosuspend failed:', e)


def usb_on():
    """Only used to undo the dwc2 unbind of older versions."""
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

    def enter(self):
        pid = launcher_pid()
        busy = pid is None or has_children(pid)   # an app is in front: be gentle
        steps = []
        if DO_AUDIO and pid and self.park_audio(pid):
            steps.append('audio')
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
        if not AUDIO_ALWAYS or why == 'exit':
            self.restore_audio()
        self.active = False
        log('wake:', why)

    def park_audio(self, pid):
        """Park the launcher's streams (new ones too); True if any are parked."""
        try:
            self.moved.update(audio_park(pid))
        except Exception as e:
            log('audio park failed:', e)
        return bool(self.moved)

    def restore_audio(self):
        if self.moved:
            try:
                audio_restore(self.moved)
            except Exception as e:
                log('audio restore failed:', e)
            self.moved = {}


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
        f'audio={DO_AUDIO} audio_always={AUDIO_ALWAYS} launcher={DO_LAUNCHER} '
        f'usb_autosuspend={DO_AUTOSUSPEND} keyboards={len(keyboards)}')

    dark_since = None
    low_count = 0
    next_battery = 0.0
    next_keyboard_scan = time.monotonic() + 5
    next_upkeep = 0.0
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

            if now >= next_upkeep:
                # Re-applied periodically: re-plugged/re-enumerated devices and
                # restarted launchers come back with their defaults.
                next_upkeep = now + 10
                if DO_AUTOSUSPEND:
                    usb_autosuspend(True)
                if AUDIO_ALWAYS:
                    pid = launcher_pid()
                    if pid:
                        idle.park_audio(pid)

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
        idle.restore_audio()
        if DO_AUTOSUSPEND:
            usb_autosuspend(False)   # back to the kernel default


if __name__ == '__main__':
    sys.exit(main())
