<p align="center"><img src="app/icon.png" width="100" alt="Idle Saver icon"></p>

<h1 align="center">cardzero-idle</h1>

**M5Stack CardputerZero 熄屏深度空闲 + 低电量自动关机**

[English](README.md)

CardputerZero 用的是树莓派 CM0（BCM2837），不支持挂起到内存。启动器熄屏后，耗电和亮屏时差不多：电池电流约 395 mA，1750 mAh 的电池大约只能撑 4.4 小时。

`cardzero-idle` 是一个以 root 运行的小守护进程。背光熄灭几秒后，它会把熄屏时白白浪费的电关掉；按任意键就全部恢复，屏幕照常亮起。在作者的设备上，熄屏电流从约 **395 mA 降到约 231 mA**；WiFi 保持连接的情况下，熄屏续航从约 4.4 小时延长到约 7.5 小时。

## Idle Saver 设置 app

安装后，启动器里会多出一个 **Idle Saver** <img src="app/icon.png" width="20" alt=""> 图标，可以直接在机器上调整设置。

<p>
<img src="docs/screenshot-main.png" width="320" alt="Idle Saver 设置列表">
<img src="docs/screenshot-info.png" width="320" alt="Idle Saver 说明页">
</p>

- **Deep idle**：整个服务的总开关。
- **After screen off**：熄屏多少秒后进入深度空闲（3–60 秒）。
- **USB + Ethernet / Sound card / Launcher**：熄屏时让这一项休眠（或暂停），还是保持开着（Keep on）。每项旁边标着大约能省多少电流。
- **Auto shutdown**：低电量关机阈值（5–20%，或 Never 不关机）。
- 顶部显示服务是否在运行、实时电池电流和电量。按 `I` 查看简要说明。

按键和启动器一致：`F`/`X` 上下移动，`Z`/`C` 或回车修改，`I` 查看说明，`ESC` 回到启动器。修改立即生效：app 通过一个 root 辅助脚本写入 `/etc/default/cardzero-idle`，然后重启服务。

## 熄屏后做了什么

| 措施 | 实测省电 | 做法 |
|---|---|---|
| 停掉 USB 主控 | ~90 mA | 解绑 `dwc2`。板载的 GL852G hub 和 SR9900A 网卡随之挂起，每秒约 8000 次中断也停了。插着网线或接了其他 USB 设备时跳过这一项。 |
| 挪走启动器的音频流 | ~40 mA | 启动器一直开着一路音频流，导致 ES8389 音频芯片和 I2S DMA 从不停止。守护进程把这路流挪到 PipeWire 的 null sink，真实声卡随之休眠。 |
| 暂停启动器 | ~30 mA | 背光关了以后，启动器仍在通过 SPI 刷屏。守护进程用 `SIGSTOP` 暂停它。如果正在运行从启动器打开的应用，就跳过这一项。 |

**唤醒：** 守护进程监听键盘设备（`tca8418c` 对应的 evdev）。按任意键时，它先让启动器恢复运行，启动器读到缓冲区里的按键，照常点亮屏幕；随后守护进程把音频流挪回去，再重新绑定 USB。如果背光因为其他原因亮起，也会执行同样的恢复。

**低电量关机：** 放电状态下，电量连续 3 次（每次间隔 20 秒）都不高于 `LOW_BATTERY_PERCENT`（默认 8%）时，执行 `systemctl poweroff` 安全关机。

**状态恢复：** 所有改动在唤醒时、服务停止时都会撤销；服务启动时也会检查一遍，防止上次异常退出后留下残余状态。

## 实测数据

用板载电量计 BQ27220（`/sys/class/power_supply/bq27220-0/current_now`）测量，每项取 30–40 秒平均。电池电压约 3.8 V，熄屏，WiFi 已连接。下表每一行都在上一行的基础上再多关一项：

| 状态 | 电流 |
|---|---|
| 原厂熄屏 | 393 mA |
| + 暂停启动器 | 363 mA |
| + 关蓝牙 | 360 mA |
| + CPU 锁 600 MHz | 358 mA |
| + 解绑 USB 主控 | 268 mA |
| + 停 tailscaled | 260 mA |
| + 关 WiFi | 222 mA |
| + 关喇叭功放（GPIO24 拉低） | 213 mA |
| + LCD blank、关 HDMI、解绑摄像头驱动 | 212 mA |
| + 关 LED | 209 mA |
| + 停 PipeWire | 173 mA |

其他数据：
- 亮屏不操作（亮度 30%）约 425 mA，其中背光本身只占约 30 mA；操作界面时 500–700 mA。
- 关蓝牙、降 CPU 频率、关 HDMI、关摄像头几乎测不出差别，所以守护进程不动这几项。
- 连 WiFi 也关掉的话，最低约 173 mA，大致就是 CM0 自身的耗电。

想测自己的设备，可以运行 `./measure.sh 30`（不需要 root）。

## 安装

在 CardputerZero 上运行：

```sh
git clone https://github.com/InstantComet/cardzero-idle.git
cd cardzero-idle
sudo bash install.sh
```

安装脚本会放置这几个文件：
- `/usr/local/sbin/cardzero-idle.py`
- `/etc/systemd/system/cardzero-idle.service`（会被启用并立即启动）
- `/etc/default/cardzero-idle`（配置文件，已存在则保留不覆盖）
- Idle Saver app：
  - `/usr/share/APPLaunch/apps/cardzero-idle/app.py`
  - `/usr/share/APPLaunch/applications/cardzero-idle.desktop`
  - `/usr/share/APPLaunch/share/images/cardzero-idle.png`
  - `/usr/local/lib/cardzero-idle/admin.py`（root 辅助脚本）
  - `/etc/sudoers.d/cardzero-idle`（只允许 `pi` 用户免密运行这个辅助脚本）

如果启动器里没有马上出现这个 app，重启一下启动器或整机即可。

用 `journalctl -u cardzero-idle -f` 查看运行情况，应该能看到 `idle: audio, usb, launcher` 和 `wake: key` 这样的日志。

卸载：`sudo bash uninstall.sh`

## 配置

可以用 Idle Saver app 修改，也可以直接编辑 `/etc/default/cardzero-idle`，再运行 `sudo systemctl restart cardzero-idle` 生效。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `IDLE_DELAY` | `5` | 背光熄灭后多少秒进入深度空闲 |
| `LOW_BATTERY_PERCENT` | `8` | 放电时电量不高于该值就关机，设为 `0` 关闭此功能 |
| `IDLE_AUDIO` | `1` | 是否挪走启动器的音频流 |
| `IDLE_STOP_LAUNCHER` | `1` | 是否暂停启动器 |
| `IDLE_USB` | `1` | 是否解绑 USB 主控 |

## 注意事项

- **测试环境：** Debian 13（trixie）、内核 `6.18.39+rpt-rpi-v8`、原厂启动器 `M5CardputerZero-APPLaunch`。默认设备用户是 `pi`（uid 1000）。
- **熄屏判定：** 读取 `/sys/class/backlight/backlight/brightness` 是否为 0。启动器的熄屏超时（设置 > 屏幕 > DarkTime）到了就会把它设为 0。
- **空闲时的网络：** USB 设备和 USB 有线网都会断开；WiFi 保持连接，SSH 和 Tailscale 照常可用。
- **音频播放：** 熄屏时其他应用的声音照常播放。守护进程只挪启动器自己的音频流，而且启动器有子应用在运行时不会被暂停。
- **sudo 权限范围：** 辅助脚本 `admin.py` 只接受启用、停用，以及 app 里能选到的那几个固定值，没法借这条 sudoers 规则写入任意配置。
- **启动顺序：** 服务不等待 `multi-user.target`。有些系统镜像设置成开机进命令行后，`userconfig.service`（tty8 上的首次开机改名向导）永远不会结束，排在 `multi-user.target` 之后的服务就都启动不了。如果 `systemctl list-jobs` 显示它卡住，而你的用户早已设置好，运行 `sudo systemctl disable userconfig.service` 即可。
- **不要在运行时解绑再重新绑定 `imx219` 摄像头驱动。** 测试时这么做导致内核在 `unicam_async_complete` 里崩溃（oops）。守护进程不会碰摄像头。
- **USB 物理开关：** 机身左侧的 USB Host/Slave 开关拨到 Slave 档也能断开 hub。这个守护进程用软件实现了同样的效果，不用去拨开关。

## 许可证

MIT
