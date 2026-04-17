#!/usr/bin/env python3

import argparse
import socket
import json
import struct
import subprocess
import psutil
import rich

from queue import Queue, Empty
from threading import Thread
from time import time, sleep
from pathlib import Path

from nicegui import ui, app

from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.launcher import Launcher
from p3lib.boot_manager import BootManager
from p3lib.helper import get_program_version, getHomePath
from p3lib.netif import NetIF


# ─────────────────────────────────────────────────────────────────────────────
# Network discovery (unchanged from original, except queue injection)
# ─────────────────────────────────────────────────────────────────────────────

class LocalYViewCollector(object):
    """Collects data from YView devices on the local LAN only."""

    PRODUCT_ID   = "PRODUCT_ID"
    IP_ADDRESS   = "IP_ADDRESS"
    RX_TIME_SECS = "RX_TIME_SECS"

    def __init__(self, uio, options, discovery_port: int, queue: Queue, poll_period_list):
        self._uio                 = uio
        self._options             = options
        self._discovery_port      = discovery_port
        self._queue               = queue
        self._poll_period_list    = poll_period_list
        self._running             = False
        self._devListenerList     = []
        self._validProuctIDList   = []
        self._areYouThereThread   = None
        self._deviceIPAddressList = []

    def close(self, halt=False):
        if self._areYouThereThread:
            self._areYouThereThread.stop()
            self._areYouThereThread = None
        if halt:
            self._running = False

    def start(self, net_if=None):
        thread = Thread(target=self._start_listening, args=(net_if,), daemon=True)
        thread.start()

    def _start_listening(self, net_if=None):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # PJA Bind error here should generate a notify msg on gui
            sock.bind(('', self._discovery_port))

            self._uio.info('Sending AYT messages.')
            self._areYouThereThread = AreYouThereThread(sock, self._discovery_port, net_if=net_if, poll_period_list=self._poll_period_list)
            self._areYouThereThread.start()

            self._uio.info("Listening on UDP port %d" % self._discovery_port)
            self._running = True
            while self._running:
                data = sock.recv(65536)
                rxTime = time()
                if data != AreYouThereThread.AreYouThereMessage:
                    try:
                        dataStr = data.decode()
                        rx_dict = json.loads(dataStr)
                        # If we receive just the AYT msg sent we will only have one eement in the dict, ignore these
                        if len(rx_dict) <= 1:
                            continue
                        rx_dict[LocalYViewCollector.RX_TIME_SECS] = rxTime
                        if LocalYViewCollector.IP_ADDRESS in rx_dict:
                            ipAddress = rx_dict[LocalYViewCollector.IP_ADDRESS]
                            if ipAddress not in self._deviceIPAddressList:
                                self._uio.info(f"Found device on {ipAddress}")
                                self._deviceIPAddressList.append(ipAddress)

                        if len(self._validProuctIDList) == 0:
                            self._updateListeners(rx_dict)

                        elif LocalYViewCollector.PRODUCT_ID in rx_dict:
                            prodID = rx_dict[LocalYViewCollector.PRODUCT_ID]
                            if len(self._validProuctIDList) == 0 or prodID in self._validProuctIDList:
                                rx_dict[LocalYViewCollector.RX_TIME_SECS] = rxTime
                                self._updateListeners(rx_dict)

                    except KeyboardInterrupt:
                        self.close()
                        break

                    except Exception as ex:
                        self._queue.put({DCon.ERROR_MSG: str(ex)})

        except KeyboardInterrupt:
            self.close()

        except Exception as ex:
            self._queue.put({DCon.ERROR_MSG: str(ex)})

    def addDevListener(self, devListener):
        self._devListenerList.append(devListener)

    def removeAllListeners(self):
        self._devListenerList = []

    def _updateListeners(self, devData):
        for devListener in self._devListenerList:
            startTime = time()
            try:
                devListener.hear(devData)
            except Exception:
                self._uio.errorException()
            exeSecs = time() - startTime
            self._uio.debug(f"EXET: devListener.hear(devData) Took {exeSecs:.6f} seconds to execute.")

    def setValidProductIDList(self, validProductIDList):
        self._validProuctIDList = validProductIDList


class AreYouThereThread(Thread):
    AreYouThereMessage  = "{\"AYT\":\"-!#8[dkG^v's!dRznE}6}8sP9}QoIR#?O&pg)Qra\"}"
    MULTICAST_ADDRESS   = "255.255.255.255"

    def __init__(self, sock, discovery_port, net_if=None, poll_period_list=[10,]):
        Thread.__init__(self)
        self._running           = None
        self.daemon             = True
        self._sock              = sock
        self._discovery_port    = discovery_port
        self._net_if            = net_if
        self._poll_period_list  = poll_period_list

    @staticmethod
    def UpdateMultiCastAddressList(subNetMultiCastAddressList, ipList, discovery_port):
        for elem in ipList:
            elems = elem.split("/")
            if len(elems) == 2:
                try:
                    ipAddress          = elems[0]
                    subNetMaskBitCount = int(elems[1])
                    intIP              = NetIF.IPStr2int(ipAddress)
                    subNetBits         = (1 << (32 - subNetMaskBitCount)) - 1
                    intMulticastAddress     = intIP | subNetBits
                    subNetMultiCastAddress  = NetIF.Int2IPStr(intMulticastAddress)
                    subNetMultiCastAddressList.append((subNetMultiCastAddress, discovery_port))
                except ValueError:
                    pass
        return subNetMultiCastAddressList

    @staticmethod
    def NetmaskToCIDR(netmask):
        return sum(bin(struct.unpack("!I", socket.inet_aton(netmask))[0]).count("1") for _ in range(1))

    @staticmethod
    def GetInterfaceDict():
        interfaces = psutil.net_if_addrs()
        if_dict = {}
        for iface, addrs in interfaces.items():
            ip_list = []
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip      = addr.address
                    netmask = addr.netmask
                    cidr    = AreYouThereThread.NetmaskToCIDR(netmask)
                    ip_list.append(f"{ip}/{cidr}")
            if ip_list:
                if_dict[iface] = ip_list
        return if_dict

    @staticmethod
    def GetSubnetMultiCastAddress(ifName, discovery_port):
        subNetMultiCastAddressList = []
        while len(subNetMultiCastAddressList) == 0:
            ifDict = AreYouThereThread.GetInterfaceDict()
            if ifName is None or len(ifName) == 0:
                for _ifName in ifDict:
                    ipList = ifDict[_ifName]
                    AreYouThereThread.UpdateMultiCastAddressList(subNetMultiCastAddressList, ipList, discovery_port)
            if ifName in ifDict:
                ipList = ifDict[ifName]
                AreYouThereThread.UpdateMultiCastAddressList(subNetMultiCastAddressList, ipList, discovery_port)
            if len(subNetMultiCastAddressList) == 0:
                sleep(1)
        return tuple(subNetMultiCastAddressList)

    def run(self):
        self._running = True
        addressList   = AreYouThereThread.GetSubnetMultiCastAddress(self._net_if, self._discovery_port)
        poll_period_index = 0
        while self._running:
            try:
                for address in addressList:
                    self._sock.sendto(AreYouThereThread.AreYouThereMessage.encode(), address)
            except OSError:
                pass
            poll_seconds = self._poll_period_list[poll_period_index]
            sleep(poll_seconds)
            if poll_period_index < len(self._poll_period_list)-1:
                poll_period_index += 1

    def stop(self):
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# Service command persistence
# ─────────────────────────────────────────────────────────────────────────────

class ServiceCommandStore:
    """Persists user-defined launch commands keyed by service type (e.g. 'WEB')."""

    # Built-in sensible defaults for common service types
    DEFAULTS = {
        "WEB":  "/usr/bin/firefox http://$h:$p",
        "SSH":  "x-terminal-emulator -e ssh $h -p $p",
        "FTP":  "nautilus ftp://$h:$p",
        "SFTP": "nautilus sftp://$h:$p",
    }

    def __init__(self, app_data_path: Path):
        self._path = app_data_path / "service_commands.json"
        self._data: dict[str, str] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception:
                self._data = {}

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2))

    def get(self, service_type: str) -> str:
        """Return the stored command, falling back to the built-in default."""
        if service_type in self._data:
            return self._data[service_type]
        return self.DEFAULTS.get(service_type, "")

    def set(self, service_type: str, command: str):
        self._data[service_type] = command
        self._save()


class ServiceLabelStore:
    """Persists user-defined friendly labels keyed by ServiceEntry.key."""

    def __init__(self, app_data_path: Path):
        self._path = app_data_path / "service_labels.json"
        self._data: dict = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception:
                self._data = {}

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2))

    def get(self, key: str) -> str:
        return self._data.get(key, "")

    def set(self, key: str, label: str):
        self._data[key] = label
        self._save()

    def delete(self, key: str):
        self._data.pop(key, None)
        self._save()




class ConfiguredServiceStore:
    """Persists manually-configured services (name, IP, port, command) added by the user."""

    def __init__(self, app_data_path: Path):
        self._path = app_data_path / "configured_services.json"
        self._data: list[dict] = []   # list of {"id": str, "name": str, "ip": str, "port": int, "command": str}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception:
                self._data = []

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2))

    def all(self) -> list[dict]:
        """Return a copy of all configured service records."""
        return list(self._data)

    def add(self, name: str, ip: str, port: int, command: str = "") -> dict:
        """Create and persist a new record; returns it."""
        record = {
            "id":      f"{ip}:{port}:{name}",
            "name":    name,
            "ip":      ip,
            "port":    port,
            "command": command,
        }
        self._data.append(record)
        self._save()
        return record

    def update(self, record_id: str, name: str, ip: str, port: int, command: str = ""):
        """Update an existing record in-place."""
        for rec in self._data:
            if rec["id"] == record_id:
                rec["name"]    = name
                rec["ip"]      = ip
                rec["port"]    = port
                rec["command"] = command
                rec["id"]      = f"{ip}:{port}:{name}"
                self._save()
                return

    def delete(self, record_id: str):
        """Remove a record by id."""
        self._data = [r for r in self._data if r["id"] != record_id]
        self._save()


class ServiceEntry:
    """Represents a single connectable service on a discovered device."""

    def __init__(self, product_id: str, ip: str, service_type: str, port: int,
                 dev_dict=None):
        self.product_id   = product_id
        self.ip           = ip
        self.service_type = service_type
        self.port         = port
        self.last_seen    = time()
        self.dev_dict     = dev_dict or {}   # full discovery payload for the edit dialog
        self.custom_label = None             # user-editable friendly name

    @property
    def key(self) -> str:
        return f"{self.ip}:{self.service_type}:{self.port}"

    def launch(self, command_template: str):
        """Substitute $h/$p and launch the external command."""
        cmd = command_template.replace("$h", self.ip).replace("$p", str(self.port))
        subprocess.Popen(cmd, shell=True)

    def __repr__(self):
        return f"<ServiceEntry {self.product_id} {self.ip} {self.service_type}:{self.port}>"


def parse_services(dev_dict: dict) -> list[ServiceEntry]:
    """Extract ServiceEntry objects from a device discovery dict.

    Devices advertise services in the SERVICES key as a comma-separated string,
    e.g.  "WEB:80,SSH:22"  or in a SERVICE key as a single entry.
    """
    entries   = []
    ip        = dev_dict.get("IP_ADDRESS", "")
    prod_id   = dev_dict.get("PRODUCT_ID", "UNKNOWN")
    raw       = dev_dict.get("SERVICE_LIST", dev_dict.get("SERVICE", ""))

    if not raw or not ip:
        return entries
    for token in str(raw).split(","):
        token = token.strip()
        if ":" in token:
            parts = token.split(":", 1)
            svc   = parts[0].strip().upper()
            try:
                port = int(parts[1].strip())
                entries.append(ServiceEntry(prod_id, ip, svc, port, dev_dict=dev_dict))
            except ValueError:
                pass
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Main application class
# ─────────────────────────────────────────────────────────────────────────────

class DCon(object):
    """GUI app that discovers local LAN devices and lets users connect to their services."""

    VERSION        = get_program_version('dcon')
    IP_ADDRESS_KEY = "IP_ADDRESS"
    ERROR_MSG      = "ERROR_MSG"
    PING_RESULT    = "PING_RESULT"   # queue message: {"PING_RESULT": {ip: bool, ...}}
    AYT_TX_SECS    = [0.25,0.5,1,2,5,10,20,30] # We send AYT msgs quickly (after 0.25 seconds) initially
                                               # then backoff to every 30 seconds. This should update the
                                               # GUI quickly with contactable devices.

    @staticmethod
    def GetAppDataPath(app_name: str) -> Path:
        home_path = Path(getHomePath())
        cfg_path  = home_path / ".config"
        if cfg_path.is_dir():
            app_cfg_path = cfg_path / app_name
        else:
            app_cfg_path = home_path / f".{app_name}"
        app_cfg_path.mkdir(parents=True, exist_ok=True)
        return app_cfg_path

    def __init__(self, uio, options):
        self._uio             = uio
        self._options         = options
        self._queue: Queue    = Queue()
        self._app_data_path   = DCon.GetAppDataPath("dcon")
        self._cmd_store       = ServiceCommandStore(self._app_data_path)
        self._label_store     = ServiceLabelStore(self._app_data_path)
        self._cfg_svc_store   = ConfiguredServiceStore(self._app_data_path)
        # Dict[str, ServiceEntry] keyed by ServiceEntry.key
        self._services: dict[str, ServiceEntry] = {}
        # Cache of last reachability results keyed by "ip:port": True=reachable, False=unreachable
        self._ping_cache: dict[str, bool] = {}

    # ── Discovery ──────────────────────────────────────────────────────────

    def run(self):
        #Start thread to discover devices and services in the background so that the GUI startup is not delayed
        thread = Thread(target=self._start_dev_listener, daemon=True)
        thread.start()
        # Start background thread to ping configured service IPs periodically
        ping_thread = Thread(target=self._ping_loop, daemon=True)
        ping_thread.start()
        self._build_gui()
        ui.run(title="DCON – Device Connector", reload=False, port=self._options.port, show=True)

    def _start_dev_listener(self):
        for port in (2939, 29340, 2934):
            collector = LocalYViewCollector(self._uio, self._options, port, self._queue, DCon.AYT_TX_SECS)
            collector.addDevListener(self)
            collector.start()
            sleep(.25)

    def hear(self, dev_dict: dict):
        """Called from worker threads – push data onto the queue; never touch the GUI here."""
        if self._uio.isDebugEnabled():
            rich.print_json(json.dumps(dev_dict))
        self._queue.put(dev_dict)

    @staticmethod
    def _is_reachable(ip: str, port: int, timeout: float = 2.0) -> bool:
        """Return True if a TCP connection to ip:port succeeds within timeout.

        Uses a plain socket connect rather than ICMP ping so no elevated
        privileges are needed and it works identically on Linux, macOS and
        Windows.  Because the configured service port is already known this
        also gives a more meaningful result — the service itself is up, not
        just the host.
        """
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _ping_loop(self):
        """Background thread: check reachability of every configured-service
        IP:port every 30 seconds and post results onto the queue so the GUI
        timer can update the table."""
        INTERVAL = 30
        while True:
            records = self._cfg_svc_store.all()
            # Key by "ip:port" so two services on the same host but different
            # ports can have independent reachability states.
            targets = {f"{rec['ip']}:{rec['port']}": (rec["ip"], rec["port"])
                       for rec in records if rec.get("ip") and rec.get("port")}
            if targets:
                results = {key: self._is_reachable(ip, port)
                           for key, (ip, port) in targets.items()}
                self._queue.put({DCon.PING_RESULT: results})
            sleep(INTERVAL)

    # ── GUI ────────────────────────────────────────────────────────────────

    def _build_gui(self):
        """Construct the NiceGUI layout."""
        # ── Styling ──────────────────────────────────────────────────────
        ui.add_head_html("""
        <style>
          @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap');

          :root {
            --bg:       #0d1117;
            --surface:  #161b22;
            --border:   #30363d;
            --accent:   #58a6ff;
            --accent2:  #3fb950;
            --text:     #e6edf3;
            --muted:    #8b949e;
            --danger:   #f85149;
            --radius:   6px;
            --font-ui:  'Inter', system-ui, sans-serif;
            --font-mono: 'Fira Code', 'Consolas', monospace;
          }

          body { background: var(--bg) !important; font-family: var(--font-ui); color: var(--text); font-size: 14px; line-height: 1.5; }

          .dcon-header {
            display: flex; align-items: center; gap: 12px;
            padding: 18px 24px;
            background: var(--surface);
            border-bottom: 1px solid var(--border);
          }
          .dcon-header h1 {
            font-size: 1.4rem; font-weight: 700; letter-spacing: 1px;
            color: var(--accent); margin: 0; font-family: var(--font-ui);
          }
          .dcon-header .version {
            font-size: 0.72rem; color: var(--muted);
            font-family: var(--font-mono);
          }
          .pulse {
            width: 10px; height: 10px; border-radius: 50%;
            background: var(--accent2);
            animation: pulse 2s infinite;
          }
          @keyframes pulse {
            0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(63,185,80,.5); }
            50%       { opacity: .7; box-shadow: 0 0 0 6px rgba(63,185,80,0); }
          }

          .services-table { width: 100%; border-collapse: collapse; font-family: var(--font-ui); font-size: .9rem; }
          .services-table th {
            text-align: left; padding: 10px 16px;
            background: var(--surface); color: var(--muted);
            font-weight: 600; letter-spacing: .4px; font-size: .75rem; text-transform: uppercase;
            border-bottom: 1px solid var(--border);
            position: sticky; top: 0; z-index: 1;
          }
          .services-table td { padding: 9px 16px; border-bottom: 1px solid var(--border); color: var(--text); vertical-align: middle; }
          .services-table td.mono { font-family: var(--font-mono); font-size: .85rem; }
          .services-table tr { transition: background .12s; }
          .services-table tr:hover td { background: rgba(88,166,255,.06); cursor: pointer; }

          .badge {
            display: inline-block; padding: 2px 8px;
            border-radius: 999px; font-size: .7rem; font-weight: 600; letter-spacing: .5px;
          }
          .badge-WEB  { background: rgba(88,166,255,.15);  color: var(--accent);  border: 1px solid rgba(88,166,255,.3); }
          .badge-SSH  { background: rgba(63,185,80,.15);   color: var(--accent2); border: 1px solid rgba(63,185,80,.3); }
          .badge-FTP  { background: rgba(248,81,73,.15);   color: var(--danger);  border: 1px solid rgba(248,81,73,.3); }
          .badge-SFTP { background: rgba(248,81,73,.12);   color: var(--danger);  border: 1px solid rgba(248,81,73,.2); }
          .badge-DEFAULT { background: rgba(139,148,158,.12); color: var(--muted); border: 1px solid rgba(139,148,158,.2); }

          .status-bar {
            padding: 6px 24px; font-size: .78rem; color: var(--muted);
            font-family: var(--font-ui);
            border-top: 1px solid var(--border); background: var(--surface);
          }

          /* Tab overrides */
          .q-tabs { background: var(--surface) !important; border-bottom: 1px solid var(--border); }
          .q-tab { color: var(--muted) !important; font-family: var(--font-ui) !important; font-size: .85rem !important; }
          .q-tab--active { color: var(--accent) !important; }
          .q-tab__indicator { background: var(--accent) !important; }
          .q-tab-panels { background: transparent !important; }
          .q-tab-panel { padding: 0 !important; }

          /* NiceGUI dialog overrides */
          .q-dialog__backdrop { background: rgba(0,0,0,.7) !important; }
          .q-card { background: var(--surface) !important; color: var(--text) !important; border: 1px solid var(--border) !important; border-radius: var(--radius) !important; font-family: var(--font-ui) !important; }
          .q-card__section { color: var(--text) !important; font-family: var(--font-ui) !important; }
          .q-field__native, .q-field__label { color: var(--text) !important; font-family: var(--font-ui) !important; font-size: 14px !important; }
          .q-field__control { background: var(--bg) !important; border: 1px solid var(--border) !important; border-radius: var(--radius) !important; }
          .q-btn { border-radius: var(--radius) !important; font-family: var(--font-ui) !important; }
        </style>
        """)

        # ── Header ───────────────────────────────────────────────────────
        with ui.element('div').classes('dcon-header'):
            ui.element('div').classes('pulse')
            with ui.element('div'):
                ui.html('<h1>DCON</h1>')
                ui.html(f'<div class="version">v{DCon.VERSION} &nbsp;·&nbsp; Device Connector</div>')

        # ── Tabs ──────────────────────────────────────────────────────────
        with ui.tabs().props('dense').style('background:var(--surface);border-bottom:1px solid var(--border);') as tabs:
            tab_discovered  = ui.tab('Discovered Services')
            tab_configured  = ui.tab('Configured Services')

        with ui.tab_panels(tabs, value=tab_discovered).style('flex:1; overflow:auto; background:var(--bg);'):

            # ── Discovered Services panel ─────────────────────────────────
            with ui.tab_panel(tab_discovered).style('padding:16px 24px;'):
                self._table_container = ui.element('div')
                with self._table_container:
                    self._render_discovered_table()

            # ── Configured Services panel ─────────────────────────────────
            with ui.tab_panel(tab_configured).style('padding:16px 24px;'):
                self._cfg_table_container = ui.element('div')
                with self._cfg_table_container:
                    self._render_configured_table()
                # Add button sits below the table
                with ui.row().style('margin-top:12px; align-items:center; gap:8px;'):
                    ui.button(
                        '＋ Add service',
                        on_click=lambda: self._on_cfg_add(),
                    ).props('unelevated').style(
                        'background:var(--accent);color:#000;font-size:.8rem;'
                    )

        # ── Bottom bar: status + quit ─────────────────────────────────────
        with ui.element('div').style(
            'display:flex; align-items:center; justify-content:space-between;'
            'border-top:1px solid var(--border); background:var(--surface);'
        ):
            self._status_el = ui.html('<div class="status-bar">Scanning LAN…</div>')
            ui.button(
                'Shutdown DCON',
                on_click=self._shutdown,
            ).props('flat').style(
                'color:var(--danger);font-family:var(--font-ui);'
                'font-size:.75rem;padding:4px 20px;letter-spacing:1px;'
            )

        # ── 100 ms timer that drains the queue and refreshes the GUI ──────
        ui.timer(0.1, self._poll_queue)

    def _shutdown(self):
        """Show a full-page shutdown overlay then stop the server after a short
        delay, giving the browser time to display the message."""
        ui.html("""
        <style>
          #shutdown-overlay {
            position: fixed; inset: 0; z-index: 9999;
            background: var(--bg);
            display: flex; flex-direction: column;
            align-items: center; justify-content: center;
            gap: 16px;
            font-family: var(--font-ui);
          }
          #shutdown-overlay .sd-title {
            font-size: 1.2rem; font-weight: 600; color: var(--danger);
            letter-spacing: .5px;
          }
          #shutdown-overlay .sd-sub {
            font-size: .85rem; color: var(--muted);
          }
          .sd-spinner {
            width: 32px; height: 32px;
            border: 3px solid var(--border);
            border-top-color: var(--danger);
            border-radius: 50%;
            animation: sd-spin .8s linear infinite;
          }
          @keyframes sd-spin { to { transform: rotate(360deg); } }
        </style>
        <div id="shutdown-overlay">
          <div class="sd-spinner"></div>
          <div class="sd-title">Shutting down DCON…</div>
          <div class="sd-sub">You can close this tab.</div>
        </div>
        """)
        ui.timer(1.5, app.shutdown, once=True)

    def _render_discovered_table(self):
        """(Re)render the discovered-services table inside _table_container."""
        self._table_container.clear()
        with self._table_container:
            if not self._services:
                ui.html('<div style="color:var(--muted);padding:32px;text-align:center;font-family:var(--font-ui);font-size:.9rem;">No devices found yet…</div>')
                return

            with ui.element('table').classes('services-table'):
                with ui.element('thead'):
                    with ui.element('tr'):
                        for col in ("Device Name", "Device Address", "Port", "Command", ""):
                            ui.element('th').text = col

                with ui.element('tbody'):
                    for svc in sorted(self._services.values(), key=lambda s: (s.custom_label or s.product_id).lower()):
                        cmd        = self._cmd_store.get(svc.service_type)
                        label      = svc.custom_label or svc.product_id
                        label_html = (f'<span style="color:#e3b341">{label}</span>'
                                      if svc.custom_label else label)
                        row        = ui.element('tr')
                        with row:
                            with ui.element('td').style('cursor:context-menu'):
                                ui.html(f'{label_html}')
                            with ui.element('td').classes('mono'):
                                ui.html(svc.ip)
                            with ui.element('td').classes('mono'):
                                ui.html(str(svc.port))
                            ui.element('td').text = cmd or "—"
                            with ui.element('td').style('padding:4px 8px; width:1px; white-space:nowrap'):
                                ui.button('✎ Edit', on_click=lambda e, s=svc: self._on_edit(s)) \
                                  .props('flat dense size=sm') \
                                  .style('color:var(--muted);font-family:var(--font-ui);font-size:.78rem;')

                        # Double-click → launch
                        row.on('dblclick', lambda e, s=svc: self._on_launch(s))
                        # Right-click → unified edit dialog
                        row.on('contextmenu', lambda e, s=svc: self._on_edit(s))

    def _render_configured_table(self):
        """(Re)render the manually-configured services table."""
        self._cfg_table_container.clear()
        with self._cfg_table_container:
            records = self._cfg_svc_store.all()
            if not records:
                ui.html('<div style="color:var(--muted);padding:32px;text-align:center;font-family:var(--font-ui);font-size:.9rem;">No configured services yet — click ＋ Add service to add one.</div>')
                return

            with ui.element('table').classes('services-table'):
                with ui.element('thead'):
                    with ui.element('tr'):
                        for col in ("Service Name", "IP Address", "Port", ""):
                            ui.element('th').text = col

                with ui.element('tbody'):
                    for rec in sorted(records, key=lambda r: r["name"].lower()):
                        ip        = rec["ip"]
                        cache_key = f"{ip}:{rec['port']}"
                        reachable = self._ping_cache.get(cache_key)   # None = not yet tested
                        if reachable is True:
                            row_color = 'color:var(--accent2)'   # green
                        elif reachable is False:
                            row_color = 'color:var(--danger)'    # red
                        else:
                            row_color = 'color:var(--text)'      # default (pending)

                        row = ui.element('tr')
                        with row:
                            with ui.element('td').classes('mono').style(row_color):
                                ui.html(rec["name"])
                            with ui.element('td').classes('mono').style(row_color):
                                ui.html(ip)
                            with ui.element('td').classes('mono').style(row_color):
                                ui.html(str(rec["port"]))
                            with ui.element('td').style('padding:4px 8px; width:1px; white-space:nowrap'):
                                with ui.row().style('gap:4px; flex-wrap:nowrap;'):
                                    ui.button('✎ Edit',
                                              on_click=lambda e, r=rec: self._on_cfg_edit(r)) \
                                      .props('flat dense size=sm') \
                                      .style('color:var(--muted);font-family:var(--font-ui);font-size:.78rem;')
                                    ui.button('✕',
                                              on_click=lambda e, r=rec: self._on_cfg_delete(r)) \
                                      .props('flat dense size=sm') \
                                      .style('color:var(--danger);font-family:var(--font-ui);font-size:.78rem;')

                        # Double-click → launch configured service command
                        row.on('dblclick', lambda e, r=rec: self._on_cfg_launch(r))

    def _poll_queue(self):
        """Drain the inter-thread queue and update the GUI."""
        changed      = False
        ping_changed = False
        try:
            while True:
                dev_dict = self._queue.get_nowait()
                if DCon.ERROR_MSG in dev_dict:
                    msg = dev_dict[DCon.ERROR_MSG]
                    if msg:
                        ui.notify(msg, position='center', type='negative')

                elif DCon.PING_RESULT in dev_dict:
                    results = dev_dict[DCon.PING_RESULT]
                    for ip, reachable in results.items():
                        if self._ping_cache.get(ip) != reachable:
                            self._ping_cache[ip] = reachable
                            ping_changed = True

                else:
                    for svc in parse_services(dev_dict):
                        # Restore any saved custom label
                        svc.custom_label = self._label_store.get(svc.key) or None
                        if svc.key not in self._services:
                            changed = True
                        self._services[svc.key] = svc

        except Empty:
            pass

        if changed:
            self._render_discovered_table()
            count = len(self._services)
            ip_count = len({s.ip for s in self._services.values()})
            self._status_el.set_content(
                f'<div class="status-bar">'
                f'{count} service{"s" if count != 1 else ""} on {ip_count} device{"s" if ip_count != 1 else ""}'
                f'</div>'
            )
        if ping_changed:
            self._render_configured_table()

    # ── Interactions ───────────────────────────────────────────────────────

    def _on_launch(self, svc: ServiceEntry):
        """Double-click: launch the external command for this service."""
        cmd = self._cmd_store.get(svc.service_type)
        if not cmd:
            ui.notify(f"No command configured for {svc.service_type}. Right-click to set one.", type="warning")
            return
        try:
            svc.launch(cmd)
            ui.notify(f"Launched {svc.service_type} → {svc.ip}:{svc.port}", type="positive")
        except Exception as ex:
            ui.notify(f"Launch failed: {ex}", type="negative")

    def _on_edit(self, svc: ServiceEntry):
        """Right-click: unified dialog to edit the device label, launch command,
        and inspect the raw discovery JSON for this service."""
        current_label = svc.custom_label or ""
        current_cmd   = self._cmd_store.get(svc.service_type)

        # Pretty-print the discovery dict, stripping the internal RX_TIME_SECS key
        display_dict  = {k: v for k, v in svc.dev_dict.items() if k != "RX_TIME_SECS"}
        pretty_json   = json.dumps(display_dict, indent=2)

        with ui.dialog() as dlg, ui.card().style('min-width:560px; max-width:720px'):
            # ── Title ────────────────────────────────────────────────────
            ui.html(
                '<div style="font-family:var(--font-ui);font-size:1rem;font-weight:700;'
                'color:var(--accent);margin-bottom:2px;">Edit service entry</div>'
            )
            badge_cls = svc.service_type if svc.service_type in ("WEB", "SSH", "FTP", "SFTP") else "DEFAULT"
            ui.html(
                f'<div style="font-size:.75rem;color:var(--muted);margin-bottom:12px;">'
                f'{svc.ip} &nbsp;·&nbsp; '
                f'<span class="badge badge-{badge_cls}">{svc.service_type}</span>'
                f'&nbsp;port {svc.port}'
                f'</div>'
            )

            # ── Device label ─────────────────────────────────────────────
            ui.html(
                '<div style="font-size:.7rem;font-weight:600;color:var(--muted);'
                'letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">'
                'Device label</div>'
            )
            label_input = ui.input(
                placeholder=svc.product_id,
                value=current_label
            ).style('width:100%;margin-bottom:12px;')

            # ── Launch command ────────────────────────────────────────────
            ui.html(
                '<div style="font-size:.7rem;font-weight:600;color:var(--muted);'
                'letter-spacing:1px;text-transform:uppercase;margin-bottom:2px;">'
                'Launch command &nbsp;<span style="font-weight:400;text-transform:none;color:var(--accent2);">'
                '($h = host, $p = port)</span></div>'
            )
            cmd_input = ui.input(
                label=f"Command for {svc.service_type}",
                value=current_cmd
            ).style('width:100%;margin-bottom:12px;')

            # ── Raw JSON ──────────────────────────────────────────────────
            ui.html(
                '<div style="font-size:.7rem;font-weight:600;color:var(--muted);'
                'letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">'
                'Discovery data</div>'
            )
            ui.html(
                f'<pre style="background:var(--bg);border:1px solid var(--border);'
                f'border-radius:var(--radius);padding:10px 14px;font-family:var(--font-mono);'
                f'font-size:.82rem;color:var(--text);overflow-x:auto;white-space:pre-wrap;'
                f'max-height:220px;overflow-y:auto;margin:0 0 12px 0;">'
                f'{pretty_json}</pre>'
            )

            # ── Action buttons ────────────────────────────────────────────
            with ui.row().style('justify-content:flex-end;gap:8px;'):
                ui.button("Cancel", on_click=dlg.close).props('flat').style('color:var(--muted)')

                def _save():
                    # Save label
                    new_label = label_input.value.strip()
                    if new_label:
                        svc.custom_label = new_label
                        self._label_store.set(svc.key, new_label)
                    else:
                        svc.custom_label = None
                        self._label_store.delete(svc.key)

                    # Save command
                    new_cmd = cmd_input.value.strip()
                    if new_cmd:
                        self._cmd_store.set(svc.service_type, new_cmd)

                    ui.notify("Changes saved", type="positive")
                    dlg.close()
                    self._render_discovered_table()

                ui.button("Save", on_click=_save).props('unelevated').style(
                    'background:var(--accent);color:#000'
                )

        dlg.open()

    # ── Configured Services interactions ───────────────────────────────────

    def _cfg_dialog(self, title: str, name: str = "", ip: str = "", port: str = "",
                    command: str = "", on_save=None):
        """Shared add/edit dialog for configured services."""
        with ui.dialog() as dlg, ui.card().style('min-width:480px'):
            ui.html(
                f'<div style="font-family:var(--font-ui);font-size:1rem;font-weight:700;'
                f'color:var(--accent);margin-bottom:12px;">{title}</div>'
            )
            name_input = ui.input(label="Service Name", value=name).style('width:100%;margin-bottom:8px;')
            ip_input   = ui.input(label="IP Address", value=ip).style('width:100%;margin-bottom:8px;')
            port_input = ui.input(label="Port", value=str(port)).style('width:100%;margin-bottom:8px;')

            # ── Launch command ────────────────────────────────────────────
            ui.html(
                '<div style="font-size:.7rem;font-weight:600;color:var(--muted);'
                'letter-spacing:1px;text-transform:uppercase;margin-bottom:2px;margin-top:4px;">'
                'Launch command &nbsp;<span style="font-weight:400;text-transform:none;color:var(--accent2);">'
                '($h = host, $p = port)</span></div>'
            )
            cmd_input = ui.input(
                label="Command",
                value=command,
            ).style('width:100%;margin-bottom:12px;')

            with ui.row().style('justify-content:flex-end;gap:8px;'):
                ui.button("Cancel", on_click=dlg.close).props('flat').style('color:var(--muted)')

                def _save():
                    n = name_input.value.strip()
                    i = ip_input.value.strip()
                    p = port_input.value.strip()
                    c = cmd_input.value.strip()
                    if not n or not i or not p:
                        ui.notify("Name, IP address and port are required.", type="warning")
                        return
                    try:
                        p = int(p)
                    except ValueError:
                        ui.notify("Port must be a number.", type="warning")
                        return
                    if on_save:
                        on_save(n, i, p, c)
                    dlg.close()
                    self._render_configured_table()

                ui.button("Save", on_click=_save).props('unelevated').style(
                    'background:var(--accent);color:#000'
                )

        dlg.open()

    def _on_cfg_add(self):
        """Open dialog to add a new configured service."""
        def _save(name, ip, port, command):
            self._cfg_svc_store.add(name, ip, port, command)
            ui.notify(f"Added '{name}'", type="positive")

        self._cfg_dialog("Add service", on_save=_save)

    def _on_cfg_edit(self, rec: dict):
        """Open dialog to edit an existing configured service."""
        def _save(name, ip, port, command):
            self._cfg_svc_store.update(rec["id"], name, ip, port, command)
            ui.notify(f"Updated '{name}'", type="positive")

        self._cfg_dialog(
            "Edit service",
            name=rec["name"],
            ip=rec["ip"],
            port=str(rec["port"]),
            command=rec.get("command", ""),
            on_save=_save,
        )

    def _on_cfg_launch(self, rec: dict):
        """Double-click: launch the configured service's external command."""
        cmd = rec.get("command", "").strip()
        if not cmd:
            ui.notify(
                f"No command configured for '{rec['name']}'. Click ✎ Edit to set one.",
                type="warning",
            )
            return
        try:
            resolved = cmd.replace("$h", rec["ip"]).replace("$p", str(rec["port"]))
            subprocess.Popen(resolved, shell=True)
            ui.notify(f"Launched '{rec['name']}' → {rec['ip']}:{rec['port']}", type="positive")
        except Exception as ex:
            ui.notify(f"Launch failed: {ex}", type="negative")

    def _on_cfg_delete(self, rec: dict):
        """Delete a configured service after confirmation."""
        with ui.dialog() as dlg, ui.card().style('min-width:320px'):
            ui.html(
                f'<div style="font-family:var(--font-ui);font-size:.95rem;color:var(--text);margin-bottom:16px;">'
                f'Delete <strong>{rec["name"]}</strong>?</div>'
            )
            with ui.row().style('justify-content:flex-end;gap:8px;'):
                ui.button("Cancel", on_click=dlg.close).props('flat').style('color:var(--muted)')

                def _confirm():
                    self._cfg_svc_store.delete(rec["id"])
                    ui.notify(f"Deleted '{rec['name']}'", type="positive")
                    dlg.close()
                    self._render_configured_table()

                ui.button("Delete", on_click=_confirm).props('unelevated').style(
                    'background:var(--danger);color:#fff'
                )
        dlg.open()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    uio          = UIO()
    prog_version = get_program_version('dcon')
    uio.info(f"dcon: V{prog_version}")

    options = None
    try:
        parser = argparse.ArgumentParser(
            description="Discover YView/CT6/temper hardware on the LAN and connect to their services.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("-d", "--debug",   action='store_true', help="Enable debugging.")
        parser.add_argument("-s", "--seconds", type=int, default=10, help="Device poll time in seconds (default=10).")
        parser.add_argument("-p", "--port",    type=int, default=8090, help="TCP port for the NiceGUI server (default=8090).")
        launcher = Launcher("icon.png", app_name="dcon")
        launcher.addLauncherArgs(parser)
        BootManager.AddCmdArgs(parser)

        options = parser.parse_args()
        uio.enableDebug(options.debug)
        uio.enableSyslog(True)

        handled = launcher.handleLauncherArgs(options, uio=uio)
        if not handled:
            handled = BootManager.HandleOptions(uio, options, False)
            if not handled:
                d_con = DCon(uio, options)
                d_con.run()

    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
    except Exception:
        logTraceBack(uio)
        if options and options.debug:
            raise
        else:
            uio.error("An unexpected error occurred.")


if __name__ == '__main__':
    main()
