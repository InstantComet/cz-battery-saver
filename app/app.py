#!/usr/bin/python3
"""Battery Saver: APPLaunch settings app for cz-battery-saver.service.

Draws straight to the LCD framebuffer with PIL and reads the internal
keyboard, like the other external APPLaunch apps. No root needed: the app
writes /var/lib/cz-battery-saver/settings (owned by the desktop user, created
by the service's StateDirectory=), and the service reloads it within a few
seconds. Package defaults live in /etc/default/cz-battery-saver.
"""
import argparse
import array
import ctypes
import fcntl
import json
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont

HOME = Path.home()
STATE_DIR = HOME / '.config/cz-battery-saver'
DEFAULTS_FILE = Path('/etc/default/cz-battery-saver')
SETTINGS_FILE = Path('/var/lib/cz-battery-saver/settings')
UNIT = 'cz-battery-saver.service'
BACKLIGHT = Path('/sys/class/backlight/backlight/brightness')
BATTERY = Path('/sys/class/power_supply/bq27220-0')
KEYBOARD = '/dev/input/cardputer-zero-internal'
DEJAVU = '/usr/share/fonts/truetype/dejavu/'
FONT, FONT_BOLD = DEJAVU + 'DejaVuSans.ttf', DEJAVU + 'DejaVuSans-Bold.ttf'
FONT_TITLE = '/usr/share/APPLaunch/share/font/Montserrat-Bold.ttf'  # launcher's own title face
# Palette matches the launcher (black) and the app icon (green battery, yellow moon).
BG = '#000000'
FG = '#ffffff'        # labels, plain values
MUTED = '#a7b0bb'     # secondary text
FAINT = '#7d8793'     # mA hints, footer keys
DIM = '#4b535d'       # rows that do nothing while the service is off
ROW = '#1c232b'       # selected row
RULE = '#2a3139'
GREEN = '#33e06a'     # saving power
YELLOW = '#ffcc1a'    # attention: low battery, applying
RED = '#ff5a52'       # error, very low battery

DEFAULTS = {'ENABLED': '1', 'IDLE_DELAY': '5', 'LOW_BATTERY_PERCENT': '8', 'IDLE_AUDIO': '1',
            'IDLE_STOP_LAUNCHER': '1', 'USB_AUTOSUSPEND': '1', 'AUDIO_ALWAYS': '0'}
# key, label, choices (None = on/off), hint shown after the label, indent
ROWS = [
    ('ENABLED', 'Power saving', None, '', 0),
    ('USB_AUTOSUSPEND', 'USB + Ethernet', None, '~105 mA', 0),
    ('AUDIO_ALWAYS', 'Key sounds', None, '~35 mA', 0),
    ('IDLE_DELAY', 'After screen off', ['3', '5', '10', '20', '30', '60'], '', 0),
    ('IDLE_AUDIO', 'Sound card', None, '~40 mA', 1),
    ('IDLE_STOP_LAUNCHER', 'Launcher', None, '~30 mA', 1),
    ('LOW_BATTERY_PERCENT', 'Auto shutdown', ['0', '5', '8', '10', '15', '20'], '', 0),
]
# Value shown for each on/off setting: (when 1, when 0). Green = saving power.
ACTION = {'USB_AUTOSUSPEND': ('Auto sleep', 'Keep on'),
          'AUDIO_ALWAYS': ('Off', 'On'),
          'IDLE_AUDIO': ('Sleep', 'Keep on'),
          'IDLE_STOP_LAUNCHER': ('Pause', 'Keep on')}
VISIBLE = 5


def read(path, default='?'):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def command(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=40)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'Command failed').strip()[-180:])
    return result.stdout.strip()


def unit_property(name):
    try:
        return command(['/usr/bin/systemctl', 'show', UNIT, '-p', name, '--value'])
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return 'unknown'


def parse_settings(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            out[key.strip()] = value.strip().strip('"\'')
    return out


def load_config():
    """Same precedence as the service: package defaults, then the app's file."""
    config = dict(DEFAULTS)
    for path in (DEFAULTS_FILE, SETTINGS_FILE):
        config.update({k: v for k, v in parse_settings(read(path, '')).items() if k in config})
    return config


def save_setting(key, value):
    """Rewrite the app's settings file with one value changed (atomic replace)."""
    current = {k: v for k, v in parse_settings(read(SETTINGS_FILE, '')).items() if k in DEFAULTS}
    current[key] = value
    body = '# Written by the Battery Saver app; overrides /etc/default/cz-battery-saver.\n'
    body += ''.join(f'{k}={v}\n' for k, v in sorted(current.items()))
    temporary = SETTINGS_FILE.with_name(SETTINGS_FILE.name + '.tmp')
    temporary.write_text(body)
    os.replace(temporary, SETTINGS_FILE)


def status():
    try:
        current = f"{-int(read(BATTERY / 'current_now', '0')) // 1000} mA now"
    except ValueError:
        current = '? mA now'
    if read(BATTERY / 'status') != 'Discharging':
        current = 'on charger'   # the gauge's current is meaningless then
    return {
        'config': load_config(),
        'active': unit_property('ActiveState'),
        'installed': unit_property('LoadState') == 'loaded',
        'battery': read(BATTERY / 'capacity'),
        'charging': read(BATTERY / 'status'),
        'current': current,
    }


class Bitfield(ctypes.Structure):
    _fields_ = [('offset', ctypes.c_uint32), ('length', ctypes.c_uint32), ('msb_right', ctypes.c_uint32)]


class VarInfo(ctypes.Structure):
    _fields_ = ([(n, ctypes.c_uint32) for n in ('xres', 'yres', 'xres_virtual', 'yres_virtual', 'xoffset', 'yoffset', 'bits_per_pixel', 'grayscale')]
                + [(n, Bitfield) for n in ('red', 'green', 'blue', 'transp')]
                + [(n, ctypes.c_uint32) for n in ('nonstd', 'activate', 'height', 'width', 'accel_flags', 'pixclock', 'left_margin', 'right_margin', 'upper_margin', 'lower_margin', 'hsync_len', 'vsync_len', 'sync', 'vmode', 'rotate', 'colorspace')]
                + [('reserved', ctypes.c_uint32 * 4)])


class FixInfo(ctypes.Structure):
    _fields_ = [('id', ctypes.c_char * 16), ('smem_start', ctypes.c_ulong), ('smem_len', ctypes.c_uint32), ('type', ctypes.c_uint32), ('type_aux', ctypes.c_uint32), ('visual', ctypes.c_uint32), ('xpanstep', ctypes.c_uint16), ('ypanstep', ctypes.c_uint16), ('ywrapstep', ctypes.c_uint16), ('line_length', ctypes.c_uint32), ('mmio_start', ctypes.c_ulong), ('mmio_len', ctypes.c_uint32), ('accel', ctypes.c_uint32), ('capabilities', ctypes.c_uint16), ('reserved', ctypes.c_uint16 * 2)]


class Framebuffer:
    def __init__(self):
        path = '/dev/fb_lcd' if os.path.exists('/dev/fb_lcd') else '/dev/fb0'
        self.fd = os.open(path, os.O_RDWR)
        self.var, self.fix = VarInfo(), FixInfo()
        fcntl.ioctl(self.fd, 0x4600, self.var)  # FBIOGET_VSCREENINFO
        fcntl.ioctl(self.fd, 0x4602, self.fix)  # FBIOGET_FSCREENINFO
        v = self.var
        if (v.xres, v.yres, v.bits_per_pixel, v.red.offset, v.green.offset, v.blue.offset) != (320, 170, 16, 11, 5, 0):
            os.close(self.fd)
            raise RuntimeError('Unsupported LCD framebuffer format')
        self.memory = mmap.mmap(self.fd, self.fix.smem_len, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        self.previous = None

    def draw(self, image):
        data = image.tobytes()
        if data == self.previous:
            return
        self.previous = data
        raw = array.array('H', (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3) for r, g, b in image.getdata())).tobytes()
        for y in range(170):
            start = (y + self.var.yoffset) * self.fix.line_length + self.var.xoffset * 2
            self.memory[start:start + 640] = raw[y * 640:(y + 1) * 640]

    def close(self):
        self.memory.close()
        os.close(self.fd)


class App:
    def __init__(self, state=None):
        self.state = state or status()
        self.selected = 0
        self.top = 0
        self.details = False
        self.running = True
        self.message = ''
        self.message_until = 0
        def load(path, size):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                return ImageFont.truetype(FONT, size)
        self.title = load(FONT_TITLE, 17)
        self.font = load(FONT, 14)
        self.bold = load(FONT_BOLD, 14)
        self.small = load(FONT, 11)

    def value(self, key, choices):
        """Text and colour for a row's value."""
        if key == 'ENABLED':
            if not self.state['installed']:
                return 'Missing', RED
            if self.state['active'] != 'active':
                return 'Not running', RED
            return ('On', GREEN) if self.on() else ('Off', MUTED)
        value = self.state['config'][key]
        if choices is None:
            saving, normal = ACTION[key]
            if value.lower() in ('1', 'yes', 'true', 'on'):
                return saving, GREEN
            return normal, MUTED
        if key == 'IDLE_DELAY':
            return value + ' s', FG
        return ('Never', MUTED) if value == '0' else ('≤ ' + value + '%', FG)

    def on(self):
        return (self.state['installed'] and self.state['active'] == 'active'
                and self.state['config']['ENABLED'].lower() in ('1', 'yes', 'true', 'on'))

    def render(self):
        im = Image.new('RGB', (320, 170), BG)
        d = ImageDraw.Draw(im)
        on = self.on()

        # Header: title, battery, status line
        d.text((10, 4), 'Battery Saver', font=self.title, fill=FG)
        try:
            level = int(self.state['battery'])
        except ValueError:
            level = 100
        battery = self.state['battery'] + '%'
        d.text((310 - d.textlength(battery, font=self.bold), 6), battery, font=self.bold,
               fill=RED if level <= 10 else YELLOW if level <= 20 else GREEN)
        d.ellipse((11, 29, 17, 35), fill=GREEN if on else MUTED)
        d.text((22, 25), ('Saving' if on else 'Off') + '  ·  ' + self.state['current'],
               font=self.small, fill=MUTED)

        if self.details:
            lines = [('Screen on: ~416 → ~311 mA', FG),
                     ('Screen off: ~395 → ~233 mA', FG),
                     ('USB sleeps only while unused; plugging', MUTED),
                     ('in a device or cable wakes it.', MUTED),
                     ('Any key wakes from screen-off idle.', MUTED)]
            for i, (line, colour) in enumerate(lines):
                d.text((12, 44 + i * 19 + (2 if i >= 2 else 0)), line, font=self.font if i < 2 else self.small, fill=colour)
            footer = [('ESC', 'Back')]
        else:
            for slot, i in enumerate(range(self.top, min(self.top + VISIBLE, len(ROWS)))):
                key, label, choices, hint, indent = ROWS[i]
                y = 43 + 20 * slot
                active = key == 'ENABLED' or on
                if i == self.selected:
                    d.rounded_rectangle((6, y - 1, 305, y + 18), radius=4, fill=ROW)
                    d.rounded_rectangle((6, y + 2, 8, y + 15), radius=1, fill=GREEN if active else DIM)
                x = 14 + 14 * indent
                if indent:
                    d.text((x - 12, y), '└', font=self.font, fill=RULE if not active else FAINT)
                d.text((x, y), label, font=self.font, fill=FG if active else DIM)
                if hint:
                    d.text((x + 6 + d.textlength(label, font=self.font), y + 3), hint, font=self.small,
                           fill=FAINT if active else DIM)
                text, colour = self.value(key, choices)
                d.text((298 - d.textlength(text, font=self.bold), y), text, font=self.bold,
                       fill=colour if active else DIM)
            if len(ROWS) > VISIBLE:  # scroll bar
                h = 100 * VISIBLE // len(ROWS)
                y0 = 42 + (100 - h) * self.top // (len(ROWS) - VISIBLE)
                d.rounded_rectangle((311, 42, 313, 142), radius=1, fill=RULE)
                d.rounded_rectangle((311, y0, 313, y0 + h), radius=1, fill=FAINT)
            footer = [('F/X', 'Move'), ('Z/C', 'Change'), ('I', 'Info'), ('ESC', 'Exit')]

        # Footer: key hints, or a status message in its place
        d.line((8, 146, 312, 146), fill=RULE)
        if self.message and time.monotonic() < self.message_until:
            d.text((10, 152), self.message, font=self.small,
                   fill=GREEN if self.message == 'Saved' else YELLOW if self.message.startswith('Saved') else RED)
        else:
            x = 10
            for keys, action in footer:
                d.text((x, 152), keys, font=self.small, fill=FG)
                x += d.textlength(keys + ' ', font=self.small)
                d.text((x, 152), action, font=self.small, fill=FAINT)
                x += d.textlength(action, font=self.small) + 14
        return im

    def apply(self, key, value):
        try:
            save_setting(key, value)
            # The udev rule reads the USB setting only when the device appears.
            self.message = 'Saved · applies after a reboot' if key == 'USB_AUTOSUSPEND' else 'Saved'
        except OSError as error:
            print(key, value, '->', error, flush=True)
            self.message = 'Failed. See ~/.config/cz-battery-saver/app.log'
        self.state = status()
        self.message_until = time.monotonic() + 4

    def change(self, step):
        key, _, choices, _, _ = ROWS[self.selected]
        if not self.state['installed']:
            self.message, self.message_until = 'cz-battery-saver is not installed', time.monotonic() + 4
            return
        current = self.state['config'][key]
        if choices is None:
            self.apply(key, '0' if current.lower() in ('1', 'yes', 'true', 'on') else '1')
            return
        index = choices.index(current) if current in choices else 0
        if step == 0:  # ENTER cycles forward with wrap-around
            index = (index + 1) % len(choices)
        else:
            index = max(0, min(len(choices) - 1, index + step))
        if choices[index] != current:
            self.apply(key, choices[index])

    def key(self, code):
        # APPLaunch navigation contract: F/X/Z/C act as arrows without Fn.
        code = {33: 103, 45: 108, 44: 105, 46: 106}.get(code, code)
        if code in (1, 41):  # Esc, or bare backtick on the compact keyboard
            if self.details:
                self.details = False
            else:
                self.running = False
            return
        if code == 23:  # I
            self.details = not self.details
            return
        if self.details:
            return
        if code in (103, 17, 39):  # Up, W, ;
            self.selected = (self.selected - 1) % len(ROWS)
        elif code in (108, 31, 52):  # Down, S, .
            self.selected = (self.selected + 1) % len(ROWS)
        elif code in (28, 57):  # Enter, Space
            self.change(0)
        elif code in (105, 30, 51):  # Left, A, ,
            self.change(-1)
        elif code in (106, 32, 53):  # Right, D, /
            self.change(1)
        self.top = max(min(self.top, self.selected), self.selected - VISIBLE + 1)


def run():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / 'app.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    app = App()
    frame = Framebuffer()
    keyboard = os.open(KEYBOARD, os.O_RDONLY | os.O_NONBLOCK)
    # No EVIOCGRAB: APPLaunch keeps its emergency return shortcut.
    event = struct.Struct('llHHi')
    last = 0.0
    signal.signal(signal.SIGTERM, lambda *_: setattr(app, 'running', False))
    signal.signal(signal.SIGINT, lambda *_: setattr(app, 'running', False))
    if int(read(BACKLIGHT, '0') or 0) == 0:
        BACKLIGHT.write_text(read(HOME / '.config/zero-control/brightness', '60'))
    try:
        while app.running:
            now = time.monotonic()
            if now - last >= 2:
                app.state = status()
                last = now
            frame.draw(app.render())
            ready, _, _ = select.select([keyboard], [], [], 0.5)
            if ready:
                data = os.read(keyboard, event.size * 64)
                for offset in range(0, len(data) - event.size + 1, event.size):
                    _, _, kind, code, value = event.unpack_from(data, offset)
                    if kind == 1 and value == 1:  # presses only; repeats would spam restarts
                        app.key(code)
    finally:
        os.close(keyboard)
        frame.close()
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true', help='print state as JSON')
    parser.add_argument('--preview', metavar='PNG', help='render one frame to a PNG')
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(), indent=2))
    elif args.preview:
        App().render().save(args.preview)
    else:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / 'app.log', 'w', buffering=1) as log:
            sys.stdout = sys.stderr = log
            try:
                run()
            except Exception:
                import traceback
                traceback.print_exc()
                raise SystemExit(1)
