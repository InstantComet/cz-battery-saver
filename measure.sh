#!/bin/bash
# Average battery current over N seconds (default 30) from the BQ27220 gauge.
# Unplug the charger first; positive output = mA drawn from the battery.
set -eu
N=${1:-30}
B=/sys/class/power_supply/bq27220-0
echo "status=$(cat $B/status) capacity=$(cat $B/capacity)% backlight=$(cat /sys/class/backlight/backlight/brightness)"
s=0
for _ in $(seq "$N"); do
  s=$((s + $(cat $B/current_now)))
  sleep 1
done
echo "average over ${N}s: $(( -s / N / 1000 )) mA"
