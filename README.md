# OnStep Alpaca Driver

An [ASCOM Alpaca](https://ascom-standards.org/Developer/Alpaca.htm) telescope driver for
[OnStep](https://onstep.groups.io/) and OnStepX mounts, written in Python. It runs as a small
local server that NINA, PHD2 and other ASCOM/Alpaca clients can share.

- Finds the mount automatically over USB serial or WiFi (the ESP addon, ports 9998/9999).
- Works out what the mount can do from the firmware (German equatorial, fork, alt/az), so
  pier side and meridian behaviour are only advertised where they make sense.
- Supports goto, sync, park/unpark, home, tracking rates, axis moves and pulse guiding.
- Has a built-in simulator so you can try a client without hardware.

## Requirements

- Python 3.10 or newer
- An OnStep or OnStepX controller on USB or WiFi
- On Windows, for PHD2: the [ASCOM Platform](https://ascom-standards.org/) 6.6 or newer

## Install

```sh
git clone https://github.com/Pop-Vlad/ascom-onstep-driver.git
cd ascom-onstep-driver
python -m pip install -r requirements.txt
```

## Run

```sh
python -m onstep_alpaca
```

The server starts at once on `http://127.0.0.1:11111/` and answers Alpaca discovery. It does
not touch the mount until a client connects, so you can leave it running with the mount off.
Open `http://127.0.0.1:11111/setup` to see the status and current settings.

If the mount is not found, run a scan first. It lists the serial ports and any mounts that
answer, then exits:

```sh
python -m onstep_alpaca --scan
```

Useful options (`python -m onstep_alpaca --help` lists them all):

| Option | What it does |
| --- | --- |
| `--serial-port COM5 --baud 9600` | Use this serial port and skip auto-detection |
| `--host 192.168.1.50` | Try this mount address first (repeatable) |
| `--scan-subnet` | Search the local subnet if nothing else answers (slow) |
| `--bind 0.0.0.0` | Let clients on other computers connect (see [Security](#security)) |
| `--port 11111` | Alpaca API port |
| `--simulate` | Serve a simulated mount instead of real hardware |
| `--log-level DEBUG` | More logging |

Only one program can use the mount's serial port at a time. Close the OnStep app, the
Smart Hand Controller, or any other driver that is using it.

## Using with NINA

1. Start the driver.
2. In NINA, go to **Equipment → Mount**.
3. Open the device list and click the refresh button. **OnStep** shows up under Alpaca devices.
4. Choose it and click **Connect**.

If NINA runs on another computer, start the driver with `--bind 0.0.0.0`.

## Using with PHD2

PHD2 on Windows reaches mounts through ASCOM, so the Alpaca device has to be registered with
the ASCOM Platform once:

1. Start the driver.
2. In PHD2, open **Connect Equipment**. Under **Mount**, choose any ASCOM telescope entry to
   open the ASCOM Chooser (or open the Chooser from ASCOM Diagnostics).
3. In the Chooser, open the **Alpaca** menu, turn on discovery, and click **Discover Now**.
4. Pick **OnStep** from the list. The ASCOM Platform creates a driver for it, which then
   appears in PHD2's mount list.
5. Connect, then calibrate as usual.

Notes for guiding:

- The guide rate can be 0.25×, 0.5× or 1× sidereal. The driver uses the one closest to what
  PHD2 asks for.
- Classic OnStep rejects pulses longer than 16.4 seconds. OnStepX has no such limit, so the
  driver caps pulses at 60 seconds for safety.
- With NINA and PHD2 both connected, set PHD2 to get the declination and pier side from the
  mount, so you don't need to recalibrate after a meridian flip.

## Using NINA and PHD2 together

Both programs can connect at the same time. They share one connection to the mount.

**Be careful when disconnecting:** a disconnect from any client closes the mount connection
for every client. If you disconnect the mount in PHD2, NINA loses it too.

## Configuration

Settings are stored in a JSON file. It is created on the first run:

- Windows: `%LOCALAPPDATA%\onstep-alpaca\config.json`
- Linux/macOS: `$XDG_CONFIG_HOME/onstep-alpaca/config.json` or `~/.config/onstep-alpaca/config.json`

Command line options override the file for that run only. Fields you may want to change:

| Field | Default | Meaning |
| --- | --- | --- |
| `bind_address` | `127.0.0.1` | `0.0.0.0` to accept clients from other computers |
| `alpaca_port` | `11111` | Alpaca API port |
| `hosts` | `[]` | Mount IP addresses to try before `192.168.0.1` |
| `scan_subnet` | `false` | Search the local subnet when nothing else is found |
| `prefer_usb` | `true` | Try USB before the network |
| `poll_interval` | `0.5` | Seconds between mount status updates |
| `site_elevation` | `0.0` | Site elevation in metres (OnStep doesn't store one) |
| `allowed_hosts` | `[]` | Extra hostnames clients may use to reach the server |

## License

MIT. See [LICENSE](LICENSE).
