# rindicator

Turns Corsair Vengeance RGB DDR5 RAM into a live GPU load indicator.

Samples the NVIDIA GPU every 250 ms (via NVML) and paints the RAM sticks through
[OpenRGB](https://openrgb.org)'s SDK. Two effects:

| Effect | Mapping |
| --- | --- |
| `gradient` | whole stick, blue at 0% → purple at 50% → red at 100% |
| `bar` | rising red level bar: 0% off, 100% full, fractional top LED fades in |

Load is exponentially smoothed (τ = 0.75 s), so the sticks breathe instead of
flickering with every sample.

## Requirements

- Python ≥ 3.11
- OpenRGB ≥ 1.0rc3 with the **Corsair DRAM** detector enabled
- An NVIDIA GPU whose NVML UUID you can read

## Setup

```bash
git clone https://github.com/satellitedown/rindicator
cd rindicator
python -m venv .venv
.venv/bin/python -m pip install -e .
```

### OpenRGB backend

Run a dedicated OpenRGB server with a config that enables *only* the RAM
detector. Omitting a detector leaves it **enabled**, so write every key
explicitly:

```bash
mkdir -p ~/.config/rindicator/openrgb
jq '{AutoStart: {enabled: false},
     Detectors: {detectors: (.Detectors.detectors | with_entries(.value = false)
                                                 | .["Corsair DRAM"] = true)}}' \
   ~/.config/OpenRGB/OpenRGB.json > ~/.config/rindicator/openrgb/OpenRGB.json

openrgb --server --server-host 127.0.0.1 --server-port 6742 --noautoconnect \
        --config ~/.config/rindicator/openrgb
```

The server needs read access to the SMBus I2C nodes. The packaged OpenRGB udev
rules already grant that to the logged-in user; do not run OpenRGB as root.

### Config

`.venv/bin/rindicator list` prints every GPU and SDK device with its exact
`name` and `location`. Put the ones you want into
`~/.config/rindicator/config.json`:

```json
{
  "gpu_uuid": "GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "effect": "gradient",
  "devices": [
    {
      "name": "Corsair Vengeance RGB DDR5",
      "location": "I2C: SMBus PIIX4 adapter port 0 at 0b00 (/dev/i2c-13), address 0x19",
      "reverse": false
    }
  ]
}
```

`reverse` flips the LED order for that stick. To calibrate, run the app in
`bar` mode, put some load on the GPU, and watch which end of the stick fills
first: bottom end first → `false`, top end first → `true`.

## Usage

```bash
.venv/bin/rindicator list                      # inventory, never writes
.venv/bin/rindicator run                       # drive the RAM continuously
.venv/bin/rindicator run --effect bar          # one-off effect override
.venv/bin/rindicator run --dry-run             # print samples/colors, write nothing
.venv/bin/rindicator set-effect bar            # persist the startup effect
.venv/bin/rindicator --config /path/config.json run
```

`run` takes a nonblocking `flock` on `$XDG_RUNTIME_DIR/rindicator.lock`, so only
one writer ever touches the RAM. It only ever sets `Direct` mode with
`save=False` and per-LED colors; no other device, profile, or saved mode is
touched.

Exit codes: `0` requested shutdown, `1` backend/telemetry unavailable, `2`
invalid config, unsupported device, or another writer is running.

## Running at login

Copy `systemd/*.service` to `~/.config/systemd/user/`, adjust the paths if the
repo is not at `~/Projects/rindicator`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now rindicator-openrgb.service rindicator.service
```

The backend service starts the OpenRGB server; the indicator service waits up
to 30 s for the configured sticks to appear, so ordering races are harmless.

Switch effects:

```bash
.venv/bin/rindicator set-effect bar
systemctl --user restart rindicator.service
```

Stop (clears the RAM LEDs) and remove:

```bash
systemctl --user stop rindicator.service
systemctl --user disable --now rindicator.service rindicator-openrgb.service
```

## Caveats

- On a graceful stop the app writes black to its own sticks. If the backend dies
  while it runs, the sticks simply keep their last color — clearing is
  best-effort, not a hardware failsafe.
- Only Corsair Vengeance DDR5 is exercised here. Any device that OpenRGB exposes
  as `DRAM` with Corsair Vengeance naming, a single linear zone, and a per-LED
  `Direct` mode will work.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
