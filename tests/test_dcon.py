"""
test_dcon.py — Unit tests for dcon.py

Run with:
    poetry run pytest -v

The tests deliberately avoid importing anything from nicegui or p3lib so that
the suite can run in a plain CI environment without a display or those optional
dependencies.  The classes under test (ServiceEntry, ServiceCommandStore,
ServiceLabelStore, parse_services, AreYouThereThread helpers) are extracted via
importlib after temporarily replacing the unavailable top-level imports with
lightweight stubs.

Project layout expected:
    <project root>/
        src/dcon/dcon.py
        tests/test_dcon.py
"""

import importlib
import importlib.util
import json
import socket
import struct
import sys
import types
import unittest
from pathlib import Path
from time import time
from unittest.mock import MagicMock, patch, call
import tempfile
import os


# ─────────────────────────────────────────────────────────────────────────────
# Stub out heavy / display-requiring dependencies before importing dcon
# ─────────────────────────────────────────────────────────────────────────────

def _make_stubs():
    """Insert minimal stubs for nicegui, p3lib and rich so dcon.py can be
    imported without a browser or the real packages installed."""

    # nicegui
    nicegui = types.ModuleType("nicegui")
    nicegui.ui  = MagicMock()
    nicegui.app = MagicMock()
    sys.modules.setdefault("nicegui", nicegui)

    # rich
    rich_mod = types.ModuleType("rich")
    rich_mod.print_json = MagicMock()
    sys.modules.setdefault("rich", rich_mod)

    # p3lib and its sub-modules
    for name in ("p3lib", "p3lib.uio", "p3lib.helper", "p3lib.launcher",
                 "p3lib.boot_manager", "p3lib.netif"):
        mod = types.ModuleType(name)
        sys.modules.setdefault(name, mod)

    # Specific symbols referenced at module level
    sys.modules["p3lib.helper"].get_program_version = lambda _: "0.0.0-test"
    sys.modules["p3lib.helper"].getHomePath         = lambda: str(Path.home())
    sys.modules["p3lib.helper"].logTraceBack        = MagicMock()
    sys.modules["p3lib.uio"].UIO                    = MagicMock
    sys.modules["p3lib.launcher"].Launcher          = MagicMock
    sys.modules["p3lib.boot_manager"].BootManager   = MagicMock

    # NetIF — only IPStr2int / Int2IPStr are used
    class _NetIF:
        @staticmethod
        def IPStr2int(ip):
            return struct.unpack("!I", socket.inet_aton(ip))[0]
        @staticmethod
        def Int2IPStr(n):
            return socket.inet_ntoa(struct.pack("!I", n))

    sys.modules["p3lib.netif"].NetIF = _NetIF


_make_stubs()

# ─────────────────────────────────────────────────────────────────────────────
# Locate dcon.py relative to the project root.
#
# When run via `poetry run pytest` from the project root, __file__ resolves to
# <project root>/tests/test_dcon.py, so the project root is one level up.
# The canonical location is src/dcon/dcon.py.  A same-directory fallback is
# kept so the file can still be run standalone during development.
# ─────────────────────────────────────────────────────────────────────────────

def _find_dcon() -> Path:
    here         = Path(__file__).resolve().parent   # .../tests/
    project_root = here.parent                       # .../

    candidates = [
        project_root / "src" / "dcon" / "dcon.py",  # Poetry src layout
        here / "dcon.py",                            # same-directory fallback
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Cannot locate dcon.py. Looked in:\n" +
        "\n".join(f"  {p}" for p in candidates)
    )


_DCON_PATH = _find_dcon()

_spec   = importlib.util.spec_from_file_location("dcon", _DCON_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

ServiceEntry           = _module.ServiceEntry
ServiceCommandStore    = _module.ServiceCommandStore
ServiceLabelStore      = _module.ServiceLabelStore
ConfiguredServiceStore = _module.ConfiguredServiceStore
parse_services         = _module.parse_services
AreYouThereThread      = _module.AreYouThereThread


# ─────────────────────────────────────────────────────────────────────────────
# ServiceEntry
# ─────────────────────────────────────────────────────────────────────────────

class TestServiceEntry(unittest.TestCase):

    def _make(self, product_id="MY-DEVICE", ip="192.168.1.10",
              service_type="WEB", port=80, dev_dict=None):
        return ServiceEntry(product_id, ip, service_type, port,
                            dev_dict=dev_dict or {})

    def test_key_format(self):
        svc = self._make(ip="10.0.0.1", service_type="SSH", port=22)
        self.assertEqual(svc.key, "10.0.0.1:SSH:22")

    def test_key_uniqueness(self):
        a = self._make(ip="10.0.0.1", service_type="WEB", port=80)
        b = self._make(ip="10.0.0.1", service_type="WEB", port=8080)
        c = self._make(ip="10.0.0.2", service_type="WEB", port=80)
        self.assertNotEqual(a.key, b.key)
        self.assertNotEqual(a.key, c.key)

    def test_default_custom_label_is_none(self):
        svc = self._make()
        self.assertIsNone(svc.custom_label)

    def test_dev_dict_stored(self):
        d = {"PRODUCT_ID": "X", "IP_ADDRESS": "1.2.3.4", "FOO": "bar"}
        svc = self._make(dev_dict=d)
        self.assertEqual(svc.dev_dict["FOO"], "bar")

    def test_dev_dict_defaults_to_empty(self):
        svc = ServiceEntry("P", "1.2.3.4", "WEB", 80)
        self.assertEqual(svc.dev_dict, {})

    def test_last_seen_is_recent(self):
        before = time()
        svc = self._make()
        after = time()
        self.assertGreaterEqual(svc.last_seen, before)
        self.assertLessEqual(svc.last_seen, after)

    def test_repr(self):
        svc = self._make(product_id="DEV", ip="1.2.3.4",
                         service_type="SSH", port=22)
        self.assertIn("DEV", repr(svc))
        self.assertIn("1.2.3.4", repr(svc))
        self.assertIn("SSH", repr(svc))
        self.assertIn("22", repr(svc))

    def test_launch_substitutes_host_and_port(self):
        svc = self._make(ip="192.168.1.5", port=8080)
        with patch("subprocess.Popen") as mock_popen:
            svc.launch("/usr/bin/firefox http://$h:$p")
            mock_popen.assert_called_once_with(
                "/usr/bin/firefox http://192.168.1.5:8080", shell=True
            )

    def test_launch_substitutes_ssh_style(self):
        svc = self._make(ip="10.0.0.1", port=22)
        with patch("subprocess.Popen") as mock_popen:
            svc.launch("ssh $h -p $p")
            mock_popen.assert_called_once_with("ssh 10.0.0.1 -p 22", shell=True)

    def test_launch_no_substitution_when_no_placeholders(self):
        svc = self._make(ip="10.0.0.1", port=22)
        with patch("subprocess.Popen") as mock_popen:
            svc.launch("echo hello")
            mock_popen.assert_called_once_with("echo hello", shell=True)


# ─────────────────────────────────────────────────────────────────────────────
# parse_services
# ─────────────────────────────────────────────────────────────────────────────

class TestParseServices(unittest.TestCase):

    def _base_dict(self, **kwargs):
        d = {
            "PRODUCT_ID":   "TEST-DEV",
            "IP_ADDRESS":   "192.168.0.1",
            "SERVICE_LIST": "WEB:80",
        }
        d.update(kwargs)
        return d

    def test_single_service_via_service_list(self):
        entries = parse_services(self._base_dict(SERVICE_LIST="WEB:80"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].service_type, "WEB")
        self.assertEqual(entries[0].port, 80)
        self.assertEqual(entries[0].ip, "192.168.0.1")

    def test_multiple_services(self):
        entries = parse_services(self._base_dict(SERVICE_LIST="WEB:80,SSH:22,FTP:21"))
        self.assertEqual(len(entries), 3)
        types_ = {e.service_type for e in entries}
        ports  = {e.port for e in entries}
        self.assertEqual(types_, {"WEB", "SSH", "FTP"})
        self.assertEqual(ports,  {80, 22, 21})

    def test_fallback_to_service_key(self):
        d = {"PRODUCT_ID": "X", "IP_ADDRESS": "10.0.0.1", "SERVICE": "SSH:22"}
        entries = parse_services(d)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].service_type, "SSH")
        self.assertEqual(entries[0].port, 22)

    def test_service_list_takes_priority_over_service(self):
        d = {
            "PRODUCT_ID":   "X",
            "IP_ADDRESS":   "10.0.0.1",
            "SERVICE_LIST": "WEB:80",
            "SERVICE":      "SSH:22",
        }
        entries = parse_services(d)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].service_type, "WEB")

    def test_service_type_uppercased(self):
        entries = parse_services(self._base_dict(SERVICE_LIST="web:80"))
        self.assertEqual(entries[0].service_type, "WEB")

    def test_returns_empty_when_no_ip(self):
        d = {"PRODUCT_ID": "X", "SERVICE_LIST": "WEB:80"}
        self.assertEqual(parse_services(d), [])

    def test_returns_empty_when_no_service_list(self):
        d = {"PRODUCT_ID": "X", "IP_ADDRESS": "10.0.0.1"}
        self.assertEqual(parse_services(d), [])

    def test_skips_malformed_token_no_colon(self):
        entries = parse_services(self._base_dict(SERVICE_LIST="WEB80,SSH:22"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].service_type, "SSH")

    def test_skips_malformed_token_non_numeric_port(self):
        entries = parse_services(self._base_dict(SERVICE_LIST="WEB:abc,SSH:22"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].service_type, "SSH")

    def test_product_id_defaults_to_unknown(self):
        d = {"IP_ADDRESS": "10.0.0.1", "SERVICE_LIST": "WEB:80"}
        entries = parse_services(d)
        self.assertEqual(entries[0].product_id, "UNKNOWN")

    def test_dev_dict_is_attached_to_entry(self):
        d = self._base_dict(SERVICE_LIST="WEB:80", EXTRA="value")
        entries = parse_services(d)
        self.assertEqual(entries[0].dev_dict["EXTRA"], "value")

    def test_whitespace_around_tokens_is_stripped(self):
        entries = parse_services(self._base_dict(SERVICE_LIST=" WEB : 80 , SSH : 22 "))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].service_type, "WEB")
        self.assertEqual(entries[0].port, 80)


# ─────────────────────────────────────────────────────────────────────────────
# ServiceCommandStore
# ─────────────────────────────────────────────────────────────────────────────

class TestServiceCommandStore(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path   = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _store(self):
        return ServiceCommandStore(self._path)

    def test_returns_default_for_known_service_type(self):
        store = self._store()
        cmd = store.get("WEB")
        self.assertIn("$h", cmd)
        self.assertIn("$p", cmd)

    def test_returns_empty_string_for_unknown_type(self):
        store = self._store()
        self.assertEqual(store.get("UNKNOWN_XYZ"), "")

    def test_set_and_get_roundtrip(self):
        store = self._store()
        store.set("WEB", "/usr/bin/chromium http://$h:$p")
        self.assertEqual(store.get("WEB"), "/usr/bin/chromium http://$h:$p")

    def test_persisted_to_disk(self):
        store = self._store()
        store.set("SSH", "ssh -p $p $h")
        # Re-load from same directory
        store2 = self._store()
        self.assertEqual(store2.get("SSH"), "ssh -p $p $h")

    def test_custom_overrides_default(self):
        store = self._store()
        default_cmd = store.get("WEB")
        store.set("WEB", "xdg-open http://$h:$p")
        self.assertNotEqual(store.get("WEB"), default_cmd)
        self.assertEqual(store.get("WEB"), "xdg-open http://$h:$p")

    def test_multiple_service_types_stored_independently(self):
        store = self._store()
        store.set("FOO", "foo $h $p")
        store.set("BAR", "bar $h $p")
        self.assertEqual(store.get("FOO"), "foo $h $p")
        self.assertEqual(store.get("BAR"), "bar $h $p")

    def test_file_is_valid_json(self):
        store = self._store()
        store.set("WEB", "test-cmd")
        data = json.loads((self._path / "service_commands.json").read_text())
        self.assertIn("WEB", data)

    def test_corrupt_file_handled_gracefully(self):
        (self._path / "service_commands.json").write_text("NOT JSON{{")
        store = self._store()
        # Should fall back to defaults without raising
        self.assertIn("$h", store.get("WEB"))


# ─────────────────────────────────────────────────────────────────────────────
# ServiceLabelStore
# ─────────────────────────────────────────────────────────────────────────────

class TestServiceLabelStore(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path   = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _store(self):
        return ServiceLabelStore(self._path)

    def test_returns_empty_string_for_unknown_key(self):
        store = self._store()
        self.assertEqual(store.get("10.0.0.1:WEB:80"), "")

    def test_set_and_get_roundtrip(self):
        store = self._store()
        store.set("10.0.0.1:WEB:80", "My Router")
        self.assertEqual(store.get("10.0.0.1:WEB:80"), "My Router")

    def test_persisted_to_disk(self):
        store = self._store()
        store.set("10.0.0.1:SSH:22", "Pi 4")
        store2 = self._store()
        self.assertEqual(store2.get("10.0.0.1:SSH:22"), "Pi 4")

    def test_delete_removes_label(self):
        store = self._store()
        store.set("10.0.0.1:WEB:80", "My Device")
        store.delete("10.0.0.1:WEB:80")
        self.assertEqual(store.get("10.0.0.1:WEB:80"), "")

    def test_delete_nonexistent_key_does_not_raise(self):
        store = self._store()
        store.delete("does:not:exist")  # should be silent

    def test_multiple_labels_stored_independently(self):
        store = self._store()
        store.set("1.2.3.4:WEB:80", "Device A")
        store.set("1.2.3.5:WEB:80", "Device B")
        self.assertEqual(store.get("1.2.3.4:WEB:80"), "Device A")
        self.assertEqual(store.get("1.2.3.5:WEB:80"), "Device B")

    def test_file_is_valid_json(self):
        store = self._store()
        store.set("1.1.1.1:WEB:80", "Label")
        data = json.loads((self._path / "service_labels.json").read_text())
        self.assertIn("1.1.1.1:WEB:80", data)

    def test_corrupt_file_handled_gracefully(self):
        (self._path / "service_labels.json").write_text("{{bad json")
        store = self._store()
        self.assertEqual(store.get("anything"), "")


# ─────────────────────────────────────────────────────────────────────────────
# AreYouThereThread — static helper methods
# ─────────────────────────────────────────────────────────────────────────────

class TestAreYouThereThreadHelpers(unittest.TestCase):

    def test_netmask_to_cidr_24(self):
        cidr = AreYouThereThread.NetmaskToCIDR("255.255.255.0")
        self.assertEqual(cidr, 24)

    def test_netmask_to_cidr_16(self):
        cidr = AreYouThereThread.NetmaskToCIDR("255.255.0.0")
        self.assertEqual(cidr, 16)

    def test_netmask_to_cidr_32(self):
        cidr = AreYouThereThread.NetmaskToCIDR("255.255.255.255")
        self.assertEqual(cidr, 32)

    def test_netmask_to_cidr_8(self):
        cidr = AreYouThereThread.NetmaskToCIDR("255.0.0.0")
        self.assertEqual(cidr, 8)

    def test_update_multicast_address_list_basic(self):
        result = []
        AreYouThereThread.UpdateMultiCastAddressList(result, ["192.168.1.50/24"], 2934)
        # Broadcast for 192.168.1.0/24 is 192.168.1.255
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], ("192.168.1.255", 2934))

    def test_update_multicast_address_list_16(self):
        result = []
        AreYouThereThread.UpdateMultiCastAddressList(result, ["10.0.5.1/16"], 2939)
        self.assertEqual(result[0], ("10.0.255.255", 2939))

    def test_update_multicast_address_list_ignores_bad_cidr(self):
        result = []
        AreYouThereThread.UpdateMultiCastAddressList(result, ["192.168.1.1/bad"], 2934)
        self.assertEqual(result, [])

    def test_update_multicast_address_list_ignores_missing_slash(self):
        result = []
        AreYouThereThread.UpdateMultiCastAddressList(result, ["192.168.1.1"], 2934)
        self.assertEqual(result, [])

    def test_update_multicast_address_list_multiple_interfaces(self):
        result = []
        AreYouThereThread.UpdateMultiCastAddressList(
            result,
            ["192.168.1.10/24", "10.0.0.5/8"],
            2934,
        )
        self.assertEqual(len(result), 2)
        addrs = {r[0] for r in result}
        self.assertIn("192.168.1.255", addrs)
        self.assertIn("10.255.255.255", addrs)

    def test_get_interface_dict_returns_dict(self):
        result = AreYouThereThread.GetInterfaceDict()
        self.assertIsInstance(result, dict)

    def test_get_interface_dict_values_are_cidr_strings(self):
        result = AreYouThereThread.GetInterfaceDict()
        for iface, ip_list in result.items():
            for entry in ip_list:
                self.assertIn("/", entry, f"Expected CIDR notation in {entry}")


# ─────────────────────────────────────────────────────────────────────────────
# DCon.GetAppDataPath
# ─────────────────────────────────────────────────────────────────────────────

class TestGetAppDataPath(unittest.TestCase):

    def test_creates_directory(self):
        DCon = _module.DCon
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                sys.modules["p3lib.helper"], "getHomePath", return_value=tmpdir
            ):
                # Reload so getHomePath is re-evaluated inside the static method
                path = DCon.GetAppDataPath("testapp")
                self.assertTrue(path.is_dir())

    def test_path_contains_app_name(self):
        DCon = _module.DCon
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a .config subdir to exercise the XDG branch
            config_dir = Path(tmpdir) / ".config"
            config_dir.mkdir()
            with patch.object(
                sys.modules["p3lib.helper"], "getHomePath", return_value=tmpdir
            ):
                path = DCon.GetAppDataPath("myapp")
                self.assertIn("myapp", str(path))

    def test_idempotent(self):
        DCon = _module.DCon
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                sys.modules["p3lib.helper"], "getHomePath", return_value=tmpdir
            ):
                path1 = DCon.GetAppDataPath("testapp")
                path2 = DCon.GetAppDataPath("testapp")
                self.assertEqual(path1, path2)
                self.assertTrue(path2.is_dir())


# ─────────────────────────────────────────────────────────────────────────────
# Integration: parse → label store → command store
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration(unittest.TestCase):
    """Light integration tests that exercise parse_services together with
    the two store classes, mirroring the flow in DCon._poll_queue."""

    def setUp(self):
        self._tmpdir     = tempfile.mkdtemp()
        self._path       = Path(self._tmpdir)
        self._cmd_store  = ServiceCommandStore(self._path)
        self._lbl_store  = ServiceLabelStore(self._path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_full_round_trip_web_service(self):
        dev_dict = {
            "PRODUCT_ID":   "ROUTER",
            "IP_ADDRESS":   "192.168.1.1",
            "SERVICE_LIST": "WEB:80",
        }
        entries = parse_services(dev_dict)
        self.assertEqual(len(entries), 1)
        svc = entries[0]

        # Apply persisted label (none yet)
        svc.custom_label = self._lbl_store.get(svc.key) or None
        self.assertIsNone(svc.custom_label)

        # Save a label
        self._lbl_store.set(svc.key, "Main Router")

        # Re-parse and re-apply
        entries2 = parse_services(dev_dict)
        entries2[0].custom_label = self._lbl_store.get(entries2[0].key) or None
        self.assertEqual(entries2[0].custom_label, "Main Router")

    def test_launch_command_uses_cmd_store(self):
        dev_dict = {
            "PRODUCT_ID":   "NAS",
            "IP_ADDRESS":   "192.168.1.100",
            "SERVICE_LIST": "WEB:8080",
        }
        self._cmd_store.set("WEB", "/usr/bin/chromium http://$h:$p")
        entries = parse_services(dev_dict)
        svc     = entries[0]
        cmd     = self._cmd_store.get(svc.service_type)
        with patch("subprocess.Popen") as mock_popen:
            svc.launch(cmd)
            mock_popen.assert_called_once_with(
                "/usr/bin/chromium http://192.168.1.100:8080", shell=True
            )

    def test_multi_service_device_all_entries_share_dev_dict(self):
        dev_dict = {
            "PRODUCT_ID":   "SERVER",
            "IP_ADDRESS":   "10.0.0.1",
            "SERVICE_LIST": "WEB:80,SSH:22,FTP:21",
        }
        entries = parse_services(dev_dict)
        self.assertEqual(len(entries), 3)
        for e in entries:
            self.assertIs(e.dev_dict, dev_dict)

    def test_label_delete_reverts_to_product_id(self):
        dev_dict = {
            "PRODUCT_ID":   "PI",
            "IP_ADDRESS":   "10.0.0.5",
            "SERVICE_LIST": "SSH:22",
        }
        svc = parse_services(dev_dict)[0]
        self._lbl_store.set(svc.key, "Raspberry Pi")
        self._lbl_store.delete(svc.key)
        svc.custom_label = self._lbl_store.get(svc.key) or None
        # Falls back to product_id when label is None
        display = svc.custom_label or svc.product_id
        self.assertEqual(display, "PI")


# ─────────────────────────────────────────────────────────────────────────────
# ConfiguredServiceStore
# ─────────────────────────────────────────────────────────────────────────────

class TestConfiguredServiceStore(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path   = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _store(self):
        return ConfiguredServiceStore(self._path)

    def test_empty_on_first_load(self):
        store = self._store()
        self.assertEqual(store.all(), [])

    def test_add_single_record(self):
        store = self._store()
        rec = store.add("My NAS", "192.168.1.10", 8080)
        self.assertEqual(rec["name"], "My NAS")
        self.assertEqual(rec["ip"],   "192.168.1.10")
        self.assertEqual(rec["port"], 8080)
        self.assertEqual(rec["command"], "")

    def test_add_with_command(self):
        store = self._store()
        rec = store.add("Router", "192.168.0.1", 80, "/usr/bin/firefox http://$h:$p")
        self.assertEqual(rec["command"], "/usr/bin/firefox http://$h:$p")

    def test_add_command_defaults_to_empty(self):
        store = self._store()
        rec = store.add("Pi", "10.0.0.1", 22)
        self.assertEqual(rec.get("command", ""), "")

    def test_command_persisted(self):
        store = self._store()
        store.add("NAS", "10.0.0.2", 8080, "xdg-open http://$h:$p")
        store2 = self._store()
        self.assertEqual(store2.all()[0]["command"], "xdg-open http://$h:$p")

    def test_update_changes_command(self):
        store = self._store()
        rec = store.add("Dev", "10.0.0.1", 80, "old-cmd $h $p")
        store.update(rec["id"], "Dev", "10.0.0.1", 80, "new-cmd $h $p")
        self.assertEqual(store.all()[0]["command"], "new-cmd $h $p")

    def test_update_command_persisted(self):
        store = self._store()
        rec = store.add("Dev", "10.0.0.1", 80, "old $h $p")
        store.update(rec["id"], "Dev", "10.0.0.1", 80, "new $h $p")
        store2 = self._store()
        self.assertEqual(store2.all()[0]["command"], "new $h $p")

    def test_command_substitution(self):
        """$h and $p in the command should resolve to the record's ip and port."""
        store = self._store()
        store.add("Web", "192.168.1.5", 8080, "/usr/bin/firefox http://$h:$p")
        rec = store.all()[0]
        cmd = rec["command"].replace("$h", rec["ip"]).replace("$p", str(rec["port"]))
        self.assertEqual(cmd, "/usr/bin/firefox http://192.168.1.5:8080")

    def test_add_returns_record_with_id(self):
        store = self._store()
        rec = store.add("Pi", "10.0.0.1", 22)
        self.assertIn("id", rec)
        self.assertTrue(len(rec["id"]) > 0)

    def test_all_returns_all_added_records(self):
        store = self._store()
        store.add("Alpha", "10.0.0.1", 80)
        store.add("Beta",  "10.0.0.2", 443)
        recs = store.all()
        self.assertEqual(len(recs), 2)
        names = {r["name"] for r in recs}
        self.assertEqual(names, {"Alpha", "Beta"})

    def test_persisted_to_disk_after_add(self):
        store = self._store()
        store.add("Router", "192.168.0.1", 80)
        store2 = self._store()
        self.assertEqual(len(store2.all()), 1)
        self.assertEqual(store2.all()[0]["name"], "Router")

    def test_delete_removes_record(self):
        store = self._store()
        rec = store.add("ToDelete", "1.2.3.4", 9000)
        store.delete(rec["id"])
        self.assertEqual(store.all(), [])

    def test_delete_persisted(self):
        store = self._store()
        rec = store.add("Gone", "1.2.3.4", 9000)
        store.delete(rec["id"])
        store2 = self._store()
        self.assertEqual(store2.all(), [])

    def test_delete_nonexistent_id_does_not_raise(self):
        store = self._store()
        store.delete("no-such-id")  # must be silent

    def test_delete_only_removes_target(self):
        store = self._store()
        rec_a = store.add("Keep",   "1.1.1.1", 80)
        rec_b = store.add("Remove", "2.2.2.2", 443)
        store.delete(rec_b["id"])
        remaining = store.all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "Keep")

    def test_update_changes_fields(self):
        store = self._store()
        rec = store.add("OldName", "10.0.0.1", 80, "old $h $p")
        store.update(rec["id"], "NewName", "10.0.0.2", 8080, "new $h $p")
        updated = store.all()[0]
        self.assertEqual(updated["name"],    "NewName")
        self.assertEqual(updated["ip"],      "10.0.0.2")
        self.assertEqual(updated["port"],    8080)
        self.assertEqual(updated["command"], "new $h $p")

    def test_update_persisted(self):
        store = self._store()
        rec = store.add("Old", "1.2.3.4", 80)
        store.update(rec["id"], "New", "5.6.7.8", 443)
        store2 = self._store()
        self.assertEqual(store2.all()[0]["name"], "New")

    def test_update_id_regenerated(self):
        """After an update the id should reflect the new values."""
        store = self._store()
        rec     = store.add("X", "10.0.0.1", 80)
        old_id  = rec["id"]
        store.update(old_id, "Y", "10.0.0.2", 9000)
        new_rec = store.all()[0]
        self.assertNotEqual(new_rec["id"], old_id)

    def test_update_nonexistent_id_does_not_raise(self):
        store = self._store()
        store.update("no-such-id", "X", "1.2.3.4", 80)  # silent

    def test_multiple_adds_and_deletes(self):
        store = self._store()
        recs = [store.add(f"Dev{i}", f"10.0.0.{i}", 8000 + i) for i in range(5)]
        # Delete even-indexed
        for i in range(0, 5, 2):
            store.delete(recs[i]["id"])
        remaining = store.all()
        self.assertEqual(len(remaining), 2)
        names = {r["name"] for r in remaining}
        self.assertEqual(names, {"Dev1", "Dev3"})

    def test_file_is_valid_json(self):
        store = self._store()
        store.add("Test", "1.2.3.4", 80)
        data = json.loads((self._path / "configured_services.json").read_text())
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_corrupt_file_handled_gracefully(self):
        (self._path / "configured_services.json").write_text("NOT JSON{{")
        store = self._store()
        self.assertEqual(store.all(), [])

    def test_all_returns_copy_not_reference(self):
        """Mutating the returned list must not affect the store."""
        store = self._store()
        store.add("A", "1.1.1.1", 80)
        lst = store.all()
        lst.clear()
        self.assertEqual(len(store.all()), 1)


if __name__ == "__main__":
    unittest.main()