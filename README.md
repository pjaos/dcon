# dcon — Device Connector

A browser-based GUI for discovering and connecting to devices and services on the local LAN. `dcon` listens for YView hardware that broadcasts UDP discovery messages, builds a live table of the services those devices expose, and lets you launch the right local application (browser, terminal, file manager, …) with a single double-click.

---

## Features

- **Automatic LAN discovery** — listens on UDP ports 2939, 29340 and 2934 simultaneously. Devices are found within seconds thanks to an exponential back-off AYT (Are You There) schedule that starts at 250 ms and settles at 30-second intervals once the network is stable.
- **Two-tab interface** — *Discovered Services* shows devices found automatically on the LAN; *Configured Services* holds manually-added bookmarks for any device or service regardless of whether it broadcasts.
- **Live discovered-services table** — every service advertised by a discovered device appears as a row showing the device name, IP address, port, and configured launch command.
- **Manual service bookmarks** — add, edit, and delete services by name, IP address, and port via the Configured Services tab. Entries are persisted immediately and survive restarts.
- **One-click connect** — double-click any row in the Discovered Services tab (or press the **✎ Edit** button and launch from there) to open the service in the appropriate local application.
- **Configurable launch commands** — right-click any row (or click **✎ Edit**) to customise the command used to open a service. Use `$h` as the host placeholder and `$p` as the port placeholder, e.g. `/usr/bin/firefox http://$h:$p`.
- **Custom device labels** — give discovered devices human-friendly names that are shown in the table instead of the raw `PRODUCT_ID` reported by the device. Labels are highlighted in green.
- **Discovery data inspector** — the edit dialog shows the full JSON payload last received from the device, making it easy to see exactly what hardware and services a particular entry represents.
- **Persistent configuration** — launch commands, custom labels, and configured services are all saved to `~/.config/dcon/` (or `~/.dcon/` on systems without an XDG config directory) and restored automatically on the next run.
- **Localhost-only** — the NiceGUI server binds exclusively to `127.0.0.1`, so the web UI is never reachable from other machines on the network.

---

## Requirements

- Python 3.10 or later
- The following Python packages:

| Package | Purpose |
|---------|---------|
| `nicegui` | Browser-based GUI framework |
| `psutil` | Network interface enumeration |
| `rich` | Pretty JSON debug output |
| `p3lib` | UIO, launcher, boot manager, network helpers |



---

## Installation

# Install using the bundled installer

```bash
python3 install.py linux/dcon-<version>-py3-none-any.whl
```

---

## Usage

```
dcon [options]
```

On startup dcon will open `http://127.0.0.1:8090` in your default browser automatically.

### Command-line options

| Option | Default | Description |
|--------|---------|-------------|
| `-d`, `--debug` | off | Enable debug logging, including pretty-printed JSON for every received device message. |
| `-s`, `--seconds` | `10` | Steady-state device poll interval in seconds. |
| `-p`, `--port` | `8090` | TCP port the NiceGUI web server listens on. |

### Example

```bash
# Run with default settings
dcon

# Use a different port and enable debug output
dcon --port 9000 --debug
```

---

## Using the interface

The interface is split into two tabs.

### Discovered Services tab

Shows all services found automatically on the LAN via UDP discovery. Each row displays the device name, IP address, port, and configured launch command. The **Service type** column (WEB, SSH, etc.) is not shown here — type information is visible in the edit dialog if needed.

Double-click any row (or click **✎ Edit** and then launch from there) to open the service using the configured command. If no command has been set yet for that service type you will be prompted to configure one first.

Right-click any row, or click the **✎ Edit** button, to open the edit dialog, which lets you:

- **Set a device label** — a human-readable name shown in the *Device Name* column. Leave blank to revert to the device's own `PRODUCT_ID`. Custom labels are shown in green.
- **Set the launch command** — the shell command used when you double-click the row. Use `$h` for the host IP and `$p` for the port. For example:
  - Web: `/usr/bin/firefox http://$h:$p`
  - SSH: `x-terminal-emulator -e ssh $h -p $p`
  - FTP: `nautilus ftp://$h:$p`
- **Inspect discovery data** — the raw JSON last received from the device is shown at the bottom of the dialog, formatted for readability.

### Configured Services tab

Lists services you have added manually — useful for devices that do not support automatic discovery, or for services you want to keep as quick-access bookmarks regardless of whether they are currently broadcasting on the LAN.

The table shows **Name**, **IP Address**, **Port**, and action buttons per row.

- **＋ Add service** — opens a dialog to enter a name, IP address, and port for a new entry.
- **✎ Edit** — opens the same dialog pre-filled with the existing values to update them.
- **✕** — shows a confirmation prompt before permanently deleting the row.

All configured services are saved immediately to disk and restored on the next run.

### Built-in command defaults

dcon ships with default launch commands for the most common service types, applied automatically when a new service type is first seen:

| Service type | Default command |
|---|---|
| `WEB` | `/usr/bin/firefox http://$h:$p` |
| `SSH` | `x-terminal-emulator -e ssh $h -p $p` |
| `FTP` | `nautilus ftp://$h:$p` |
| `SFTP` | `nautilus sftp://$h:$p` |

---

## Device discovery protocol

dcon expects devices to respond to a UDP broadcast containing the AYT (Are You There) message with a JSON payload. The payload must include at minimum:

```json
{
  "PRODUCT_ID": "my-device",
  "IP_ADDRESS": "192.168.1.42",
  "SERVICE_LIST": "WEB:80,SSH:22"
}
```

The `SERVICE_LIST` key (or the legacy `SERVICE` key for single-service devices) is a comma-separated list of `TYPE:PORT` tokens. All other fields in the payload are preserved and displayed in the discovery data section of the edit dialog.

Discovery broadcasts are sent on three UDP ports simultaneously:

| Port | Device family |
|------|--------------|
| 2939 | Temper hardware |
| 29340 | CT6 devices |
| 2934 | General YView devices |

---

## Configuration files

All configuration is stored in JSON files under the application data directory:

- **Linux (XDG):** `~/.config/dcon/`
- **Linux (non-XDG) / fallback:** `~/.dcon/`

| File | Contents |
|------|----------|
| `service_commands.json` | Launch command templates, keyed by service type (e.g. `"WEB"`) |
| `service_labels.json` | Custom device labels, keyed by `IP:SERVICE_TYPE:PORT` |
| `configured_services.json` | Manually-configured services (name, IP, port) added via the Configured Services tab |

These files are plain JSON and can be edited by hand if needed.

---

## Architecture overview

```
main()
 └─ DCon.run()
     ├─ _start_dev_listener()              # background thread
     │   └─ LocalYViewCollector × 3       # one per UDP discovery port
     │       └─ AreYouThereThread          # sends periodic AYT broadcasts
     │
     └─ ui.run() / @ui.page('/')           # NiceGUI event loop (localhost only)
         └─ DCon._build_gui()
             ├─ ui.tabs
             │   ├─ Discovered Services
             │   │   └─ _render_discovered_table()
             │   └─ Configured Services
             │       └─ _render_configured_table()
             └─ ui.timer(100 ms)
                 └─ _poll_queue()          # drains Queue, updates discovered table
```

Worker threads never touch the GUI directly. All discovery data flows through a `queue.Queue`; a 100 ms NiceGUI timer drains it on the main thread and re-renders the Discovered Services table only when new services have been found. The Configured Services table is re-rendered immediately after any add, edit, or delete action.

## Author

Paul Austen — [pjaos@gmail.com](mailto:pjaos@gmail.com)

## Acknowledgements

Development of this project was assisted by [Claude](https://claude.ai) (Anthropic's AI assistant),
which contributed to code review, bug identification, test generation, and this documentation.
