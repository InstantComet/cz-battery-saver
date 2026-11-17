# cardzero-idle

**Deep idle for the M5Stack CardputerZero while the screen is off, plus a low-battery shutdown.**

[中文说明](README.zh-CN.md)

The CardputerZero (Raspberry Pi CM0, BCM2837) has no suspend-to-RAM. When the launcher turns the screen off, the device keeps drawing almost as much power as with the screen on: about 395 mA from the battery, or roughly 4.4 h from the 1750 mAh cell.

`cardzero-idle` is a small root daemon. It notices when the backlight has been off for a few seconds and then cuts the power that is wasted while the screen is off. Pressing any key restores everything, and the key still wakes the screen as usual. On the author's device this brought **screen-off current from ~395 mA down to ~231 mA**, roughly **4.4 h → 7.5 h** of screen-off runtime with WiFi kept on.

## What it does while the screen is off

| Measure | Saves (measured) | How |
|---|---|---|
| Stop the USB host controller | ~90 mA | Unbinds `dwc2`. The internal GL852G hub and the SR9900A Ethernet chip then suspend, and the ~8000 IRQ/s SOF interrupts stop. Skipped if an Ethernet cable or any extra USB device is attached. |
| Park the launcher's audio stream | ~40 mA | The launcher keeps an audio stream open at all times, so the ES8389 codec and the I2S DMA never stop. The daemon moves that stream to a PipeWire null sink, and the real sound card then suspends. |
| Pause the launcher | ~30 mA | The launcher keeps redrawing the LCD over SPI with the backlight off. It is paused with `SIGSTOP`. This is skipped while an app launched from it is running. |

Wake-up: the daemon watches the keyboard (`tca8418c` evdev device). On any key press it resumes the launcher, which reads the queued key and turns the screen back on. The daemon then moves the audio back and re-binds USB. If the backlight comes back on for any other reason, the same restore runs.

The daemon also shuts the system down cleanly (`systemctl poweroff`) when the battery is discharging and at or below `LOW_BATTERY_PERCENT` (default 8%, checked 3 times 20 s apart).

Every change is undone on wake, when the service stops, and at startup in case a previous run crashed.

## Measurements

Measured with the on-board BQ27220 fuel gauge (`/sys/class/power_supply/bq27220-0/current_now`), averaged over 30–40 s, battery at about 3.8 V, screen off, WiFi connected. Each row adds to the one above it.

| State | Current |
|---|---|
| Stock, screen off | 393 mA |
| + launcher paused | 363 mA |
| + Bluetooth off | 360 mA |
| + CPU capped at 600 MHz | 358 mA |
| + USB host unbound | 268 mA |
| + tailscaled stopped | 260 mA |
| + WiFi off | 222 mA |
| + speaker amp off (GPIO24 low) | 213 mA |
| + LCD blank, HDMI off, camera driver unbound | 212 mA |
| + LEDs off | 209 mA |
| + PipeWire stopped | 173 mA |

Other figures:
- Screen on and idle at 30% brightness: ~425 mA. The backlight itself costs only ~30 mA.
- Navigating the launcher UI: 500–700 mA.
- Bluetooth, CPU frequency, HDMI and the camera made no measurable difference, so the daemon leaves them alone.
- With WiFi off as well, the floor is ~173 mA, which is roughly what the CM0 itself draws.

To measure your own device, use `./measure.sh 30` (no root needed).

## Install

On the CardputerZero:

```sh
git clone https://github.com/InstantComet/cardzero-idle.git
cd cardzero-idle
sudo bash install.sh
```

This installs:
- `/usr/local/sbin/cardzero-idle.py`
- `/etc/systemd/system/cardzero-idle.service` (enabled and started)
- `/etc/default/cardzero-idle` (settings; an existing file is kept)

Check what it is doing with `journalctl -u cardzero-idle -f`. You should see lines like `idle: audio, usb, launcher` and `wake: key`.

To uninstall: `sudo bash uninstall.sh`

## Settings

Settings live in `/etc/default/cardzero-idle`. Run `sudo systemctl restart cardzero-idle` after changing them.

| Variable | Default | Meaning |
|---|---|---|
| `IDLE_DELAY` | `5` | Seconds the backlight must stay off before deep idle starts. |
| `LOW_BATTERY_PERCENT` | `8` | Shut down at or below this charge while discharging. `0` disables it. |
| `IDLE_AUDIO` | `1` | Park the launcher's audio stream. |
| `IDLE_STOP_LAUNCHER` | `1` | Pause the launcher. |
| `IDLE_USB` | `1` | Unbind the USB host controller. |

## Notes and caveats

- Tested on a CardputerZero running Debian 13 (trixie), kernel `6.18.39+rpt-rpi-v8`, and the stock `M5CardputerZero-APPLaunch` launcher. The device user is assumed to be `pi` (uid 1000).
- Screen-off detection reads `/sys/class/backlight/backlight/brightness == 0`, which is what the launcher's screen timeout (Settings > Screen > DarkTime) sets.
- While idle, USB devices and USB Ethernet are offline. WiFi stays up, so SSH and Tailscale keep working.
- Audio started by another app while the screen is off still plays: only the launcher's own stream is moved, and the launcher is not paused while one of its apps is running.
- Don't unbind and re-bind the `imx219` camera driver at runtime. While measuring, a re-bind oopsed the kernel in `unicam_async_complete`. The daemon does not touch the camera.
- The left-side USB Host/Slave switch also cuts the hub. The daemon does the same thing in software, so you don't have to move the switch.

## License

MIT
