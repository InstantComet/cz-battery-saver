#!/usr/bin/python3
"""CardputerZero power-saving daemon.

Always (screen on or off):
  * internal USB Ethernet chip -> runtime autosuspend (power/control=auto).
                              The kernel leaves it "on", which keeps the
                              whole USB bus awake (8000 IRQ/s). With it
                              suspended the hub and root hub follow; hot-plug
                              still works                          (~105 mA)
                              Done by the package's udev rule, which this
                              daemon switches off (USB_AUTOSUSPEND=0) with a
                              flag file; that takes effect after a reboot.
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

Runs as the desktop user (pi), not root: the launcher, PipeWire and the
keyboard all belong to that user, and pi may power the device off. Settings
are read from /etc/default/cz-battery-saver (package defaults) and
/var/lib/cz-battery-saver/settings (written by the Battery Saver app), and reloaded
whenever either file changes. Every step is best-effort and is undone on
wake, on exit and at startup (in case a previous run crashed).
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

DEFAULTS_FILE = '/etc/default/cz-battery-saver'
STATE_DIR = '/var/lib/cz-battery-saver'
SETTINGS_FILE = STATE_DIR + '/settings'
USB_KEEP_ON_FLAG = STATE_DIR + '/usb-keep-on'   # tested by the udev rule
DEFAULTS = {
    'ENABLED': '1',               # master switch, the app's "Power saving"
    'IDLE_DELAY': '5',            # s of backlight 0 before idling
    'LOW_BATTERY_PERCENT': '8',   # 0 disables
    'LOW_BATTERY_CONFIRM': '3',   # consecutive 20 s checks
    'IDLE_AUDIO': '1',
    'IDLE_STOP_LAUNCHER': '1',
    'USB_AUTOSUSPEND': '1',
    'AUDIO_ALWAYS': '0',
}


def parse_settings(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            out[key.strip()] = value.strip().strip('"' + "'")
    return out


class Config:
    def __init__(self):
        self.stamp = None
        self.load()

    @staticmethod
    def files_stamp():
        stamp = []
        for path in (DEFAULTS_FILE, SETTINGS_FILE):
            try:
                st = os.stat(path)
                stamp.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append(None)
        return stamp

    def changed(self):
        return self.files_stamp() != self.stamp

    def load(self):
        self.stamp = self.files_stamp()
        raw = dict(DEFAULTS)
        raw.update(parse_settings(read(DEFAULTS_FILE)))
        raw.update(parse_settings(read(SETTINGS_FILE)))   # the app's choices win

        def num(key):
            try:
                return int(raw[key])
            except ValueError:
                return int(DEFAULTS[key])

        def flag(key):
            return raw[key].lower() in ('1', 'yes', 'true', 'on')

        self.enabled = flag('ENABLED')
        self.idle_delay = num('IDLE_DELAY')
        self.low_battery = num('LOW_BATTERY_PERCENT')
        self.low_battery_confirm = num('LOW_BATTERY_CONFIRM')
        self.audio = flag('IDLE_AUDIO')
        self.stop_launcher = flag('IDLE_STOP_LAUNCHER')
        self.usb_autosuspend = flag('USB_AUTOSUSPEND')
        self.audio_always = flag('AUDIO_ALWAYS')

    def describe(self):
        return (f'enabled={self.enabled} delay={self.idle_delay}s low_battery={self.low_battery}% '
                f'audio={self.audio} audio_always={self.audio_always} '
                f'launcher={self.stop_launcher} usb_autosuspend={self.usb_autosuspend}')


BACKLIGHT = '/sys/class/backlight/backlight/brightness'
BATTERY = '/sys/class/power_supply/bq27220-0'
LAUNCHER_PATH = '/usr/share/APPLaunch/bin/M5CardputerZero-APPLaunch'
KEYBOARD_NAMES = ('tca8418c',)
NULL_SINK = 'cz_battery_saver_null'

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


PACTL_ENV = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}', LANG='C.UTF-8')


def run(args, timeout=10):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=PACTL_ENV)
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
    rc, out = run(['pactl', '-f', 'json', 'list', kind])
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
        run(['pactl', 'load-module', 'module-null-sink', f'sink_name={NULL_SINK}'])
    moved = {}
    for s in streams:
        orig = names.get(s['sink'])
        if orig and orig != NULL_SINK:
            rc, _ = run(['pactl', 'move-sink-input', str(s['index']), NULL_SINK])
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
                run(['pactl', 'move-sink-input', str(s['index']), target])
    # pactl's JSON module list has no index, use the short format.
    _, out = run(['pactl', 'list', 'short', 'modules'])
    for line in out.splitlines():
        cols = line.split('\t')
        if len(cols) >= 3 and cols[1] == 'module-null-sink' and f'sink_name={NULL_SINK}' in cols[2]:
            run(['pactl', 'unload-module', cols[0]])


# --------------------------------------------------------------------- usb

def sync_usb_flag(cfg):
    """USB autosuspend itself is applied by the package's udev rule (only root
    may write power/control); the rule skips the chip while this flag exists."""
    try:
        if cfg.enabled and cfg.usb_autosuspend:
            if os.path.exists(USB_KEEP_ON_FLAG):
                os.unlink(USB_KEEP_ON_FLAG)
        elif not os.path.exists(USB_KEEP_ON_FLAG):
            with open(USB_KEEP_ON_FLAG, 'w') as f:
                f.write('USB autosuspend is off; applies after a reboot.\n')
    except OSError as e:
        log('usb flag:', e)


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
    def __init__(self, cfg):
        self.cfg = cfg
        self.active = False
        self.stopped_pid = None
        self.moved = {}

    def enter(self):
        pid = launcher_pid()
        busy = pid is None or has_children(pid)   # an app is in front: be gentle
        steps = []
        if self.cfg.audio and pid and self.park_audio(pid):
            steps.append('audio')
        if self.cfg.stop_launcher and pid and not busy:
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
        if not (self.cfg.enabled and self.cfg.audio_always) or why == 'exit':
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


def battery_low(cfg):
    if not cfg.enabled or cfg.low_battery <= 0:
        return False
    if read(BATTERY + '/status') != 'Discharging':
        return False
    try:
        return int(read(BATTERY + '/capacity', '100')) <= cfg.low_battery
    except ValueError:
        return False


def main():
    cfg = Config()
    idle = Idle(cfg)

    def on_term(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)

    cleanup_leftovers()
    sync_usb_flag(cfg)
    keyboards = open_keyboards()
    log(f'started as uid {os.getuid()}: {cfg.describe()} keyboards={len(keyboards)}')

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
                if bright or key or not cfg.enabled:
                    dark_since = None
                elif dark_since is None:
                    dark_since = now
                elif now - dark_since >= cfg.idle_delay:
                    idle.enter()

            if now >= next_upkeep:
                next_upkeep = now + 3
                if cfg.changed():
                    cfg.load()
                    log('settings changed:', cfg.describe())
                    if idle.active:
                        idle.leave('settings changed')
                        dark_since = None
                    if not (cfg.enabled and cfg.audio_always):
                        idle.restore_audio()
                    sync_usb_flag(cfg)
                # Re-applied: a restarted launcher opens a new stream.
                if cfg.enabled and cfg.audio_always:
                    pid = launcher_pid()
                    if pid:
                        idle.park_audio(pid)

            if now >= next_battery:
                next_battery = now + 20
                low_count = low_count + 1 if battery_low(cfg) else 0
                if low_count >= cfg.low_battery_confirm:
                    log(f'battery at {read(BATTERY + "/capacity")}%, shutting down')
                    if idle.active:
                        idle.leave('shutdown')
                    # Allowed for the desktop user by polkit (the stock image
                    # lets the sudo group power off).
                    rc, _ = run(['systemctl', 'poweroff', '--check-inhibitors=no'], timeout=30)
                    if rc != 0:
                        log('poweroff failed, rc', rc)
                    low_count = -10**9   # don't repeat
    finally:
        if idle.active:
            idle.leave('exit')
        idle.restore_audio()


if __name__ == '__main__':
    sys.exit(main())
