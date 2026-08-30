# Coarse presence sensor — ESP32

Counts 802.11 probe requests in promiscuous mode and reports a **coarse
presence signal**. Not a device count. Not a people count. Read
[CALIBRATION.md](CALIBRATION.md) before trusting any number this produces —
the short version is that the unique-hash-to-people ratio moves between 1.4×
and 10.1× depending on things the sensor cannot observe.

## Hardware

- Any ESP32 (target: ESP32-WROOM-32)
- USB cable
- Nothing else — the on-board antenna is sufficient

## Build and flash

Arduino IDE:

1. Boards Manager → install **esp32** by Espressif (v2.x or v3.x)
2. Select your ESP32 board, open `device_counter.ino`, upload
3. Serial Monitor at **115200**

`arduino-cli`:

```bash
arduino-cli core install esp32:esp32
arduino-cli compile --fqbn esp32:esp32:esp32 hardware/device_counter
arduino-cli upload --fqbn esp32:esp32:esp32 -p /dev/ttyUSB0 hardware/device_counter
arduino-cli monitor -p /dev/ttyUSB0 -c baudrate=115200
```

## Output

CSV on serial, one row per 60 s window:

```
window,elapsed_s,unique_hashes,total_probes,saturated
1,60,14,52,0
2,60,9,38,0
```

| Column | Meaning |
|---|---|
| `window` | Sequence number since boot |
| `elapsed_s` | Window length actually elapsed |
| `unique_hashes` | Distinct salted MAC hashes seen. **Not a device count.** |
| `total_probes` | Every probe frame including repeats |
| `saturated` | `1` means the 1024-slot set filled — `unique_hashes` is a **floor**, discard or treat as ≥1024 |

Capture to a file:

```bash
python3 -c "import serial,sys; s=serial.Serial('/dev/ttyUSB0',115200); [sys.stdout.write(s.readline().decode()) for _ in iter(int,1)]" | tee presence.csv
```

## Configuration

At the top of `device_counter.ino`:

| Constant | Default | Notes |
|---|---|---|
| `WINDOW_MS` | 60000 | Reporting window. Shorter = noisier. |
| `HASH_SLOTS` | 1024 | Set capacity. Raising it costs 8 bytes per slot. |
| `DWELL_MS` | 180 | Per-channel dwell. Lower catches more channels, less of each. |

## What it will not do

The firmware **cannot**:

- transmit anything (`WIFI_MODE_AP` is never set; no beacon or probe-response
  code path exists)
- impersonate a network — operating a rogue AP is a federal crime
- read frame payloads — intercepting communications content is a federal crime
- capture SSIDs from probe requests (these identify homes and workplaces)
- persist anything to flash, or link a device across two windows

MACs are salted-hashed on arrival and the salt is regenerated every window. An
*unsalted* MAC hash is not anonymisation — 48 bits is trivially enumerable —
which is why the rotating salt is the load-bearing part.

## Deploying it

Receive-only operation being lawful is not the same as being appropriate. Use
only on property you control or with the owner's permission, and post signage
where people would not expect to be sensed.
