from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("phones_handler", ROOT / "plugins" / "phones" / "handler.py")
phones = importlib.util.module_from_spec(_spec)
sys.modules["phones_handler"] = phones
_spec.loader.exec_module(phones)

from omni.registry import Context  # noqa: E402


def mkdev(
    root: Path,
    name: str,
    *,
    vendor: str,
    product: str,
    bus: int = 1,
    dev: int = 2,
    manufacturer: str = "Corp",
    prodname: str = "Widget",
    serial: str = "SN001",
    speed: str = "480",
    usb_class: str = "00",
) -> Path:
    entry = root / name
    entry.mkdir()
    values = {
        "idVendor": vendor,
        "idProduct": product,
        "busnum": str(bus),
        "devnum": str(dev),
        "manufacturer": manufacturer,
        "product": prodname,
        "serial": serial,
        "speed": speed,
        "bDeviceClass": usb_class,
    }
    for key, value in values.items():
        (entry / key).write_text(value)
    return entry


MTP_FIXTURE = """\
libmtp version: 1.1.23

Listing raw device(s)
   001/002: (2717:ff08)@1/2, Xiaomi MTP
Device 0 (VID=2717 and PID=ff08) is a Xiaomi Redmi/Poco.
   Manufacturer: Xiaomi
   Model: POCO X3 NFC
   Device version: Android/12
   Serial number: 47ab00e0
   Vendor extension ID: 0x00000006
   Vendor extension description: microsoft.com: 1.0; android.com: 1.0;
   Detected object size: 64 bits
Storage details:
  StorageID: 0x00020001
    Storage type: 0x0003 fixed physical storage
    Max Capacity: 0x0000000059F21C00 (1,508,288,000 bytes)
    Free Space in Bytes: 0x000000004B43EC00 (1,262,866,432 bytes)
  StorageID: 0x00020002
    Storage type: 0x0003 fixed physical storage
    Max Capacity: 0x0000000275110000 (63,108,864,000 bytes)
    Free Space in Bytes: 0x00000001C8288000 (47,894,589,440 bytes)
"""


class TestClassify(unittest.TestCase):
    def test_hub(self):
        out = phones.classify_device({"vendor_id": "1d6b", "product_id": "0002", "usb_class": "09"})
        self.assertEqual(out["kind"], "hub")
        self.assertFalse(out["phone"])

    def test_known_phone_vendor(self):
        out = phones.classify_device({"vendor_id": "2717", "product_id": "ff08", "usb_class": "00"})
        self.assertEqual(out["kind"], "phone")
        self.assertTrue(out["phone"])
        self.assertEqual(out["vendor_name"], "Xiaomi")

    def test_apple_is_phone(self):
        out = phones.classify_device({"vendor_id": "05ac", "product_id": "12a8", "usb_class": "00"})
        self.assertTrue(out["phone"])
        self.assertEqual(out["vendor_name"], "Apple")

    def test_unknown(self):
        out = phones.classify_device({"vendor_id": "04f2", "product_id": "b230", "usb_class": "ef"})
        self.assertEqual(out["kind"], "unknown")
        self.assertFalse(out["phone"])


class TestSnapshotDiff(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="omni-phones-"))

    def test_plug_and_unplug(self):
        mkdev(self.root, "1-1", vendor="2717", product="ff08")
        start = phones.snapshot_usb(self.root)
        mkdev(self.root, "1-2", vendor="18d1", product="4ee7", bus=1, dev=3)
        # one device disappears too
        (self.root / "1-1" / "idVendor").unlink()  # breaks presence -> snapshot drops it
        plug, unplug = phones.diff_snapshot(start, phones.snapshot_usb(self.root))
        vanish = phones.diff_snapshot(phones.snapshot_usb(self.root), start)
        self.assertEqual(len(plug), 1)
        self.assertTrue(plug[0]["phone"])
        self.assertEqual(plug[0]["vendor_name"], "Google/Android")
        self.assertEqual(len(unplug), 1)
        self.assertEqual(len(vanish[1]), 1)  # the 1-2 google dev is "gone" in reverse diff

    def test_event_shape(self):
        ev = phones._event(
            "plug",
            {"phone": True, "kind": "phone", "vendor_name": "Xiaomi", "vendor_id": "2717",
             "product_id": "ff08", "product": "POCO", "manufacturer": "Xiaomi",
             "serial": "abc", "sysfs": "/x/1-1"},
        )
        self.assertEqual(ev["action"], "plug")
        self.assertTrue(ev["phone"])
        self.assertIn("ts", ev)

    def test_events_file_roundtrip(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="omni-phones-log-"))
        path = tmpdir / "events.jsonl"
        path.write_text(
            json.dumps({"action": "plug", "phone": True}) + "\n"
            + json.dumps({"action": "unplug", "phone": False}) + "\n",
            encoding="utf-8",
        )
        events = phones.read_events(limit=10, path=path)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["action"], "plug")  # chronological
        self.assertEqual(events[1]["action"], "unplug")


class TestParsers(unittest.TestCase):
    def test_parse_mtp(self):
        out = phones.parse_mtp(MTP_FIXTURE)
        self.assertTrue(out["connected"])
        self.assertEqual(out["manufacturer"], "Xiaomi")
        self.assertEqual(out["model"], "POCO X3 NFC")
        self.assertEqual(out["os_version"], "Android/12")
        self.assertEqual(out["serial"], "47ab00e0")
        self.assertIn("android_MTP", out["extensions"])
        self.assertEqual(len(out["storage"]), 2)
        self.assertEqual(out["storage"][0]["capacity_bytes"], 1_508_288_000)
        self.assertEqual(out["storage"][0]["free_bytes"], 1_262_866_432)
        self.assertEqual(out["storage"][1]["capacity_bytes"], 63_108_864_000)

    def test_bytes_line_variants(self):
        self.assertEqual(phones._bytes_from_mtp_line("Max Capacity: (630,542,540,800 bytes)"), 630_542_540_800)
        self.assertEqual(phones._bytes_from_mtp_line("Max Capacity: (1 234 567 bytes)"), 1_234_567)
        self.assertIsNone(phones._bytes_from_mtp_line("Max Capacity: raw no parens"))
        # vivo-style: hex id, decimal without parens
        self.assertEqual(
            phones._bytes_from_mtp_line("Max Capacity: 0x0000000059F21C00 68,719,476,736 bytes"),
            68_719_476_736,
        )

    def test_parse_adb_devices(self):
        text = "List of devices attached\n\tabc123\tdevice usb:1-1 product:xyz\n\tdef456\tunauthorized\n\n"
        out = phones.parse_adb_devices(text)
        self.assertEqual(out[0], {"serial": "abc123", "state": "device"})
        self.assertEqual(out[1], {"serial": "def456", "state": "unauthorized"})


    def test_parse_mtp_vivo_dialect(self):
        vivo = MTP_FIXTURE.splitlines()[0] + "\n" + """\
Device 0 (VID=2d95 and PID=6003) is a vivo V2066.
   Manufacturer: vivo
   Model: V2066
   Device version: 1.0
   Serial number: 0BDC78C8D157DE5511E0BC7822CAECCA
Storage Devices:
   StorageID: 0x00010001
      StorageType: 0x0003 fixed RAM storage
      AccessCapability: 0x0000 read/write
      MaxCapacity: 114921799680
      FreeSpaceInBytes: 44207837184
      FreeSpaceInObjects: 1073741824
      StorageDescription: Internal storage
MTP-specific device properties:
   Battery level 91 of 100 (91%)
"""
        out = phones.parse_mtp(vivo)
        self.assertEqual(len(out["storage"]), 1)
        self.assertEqual(out["storage"][0]["capacity_bytes"], 114_921_799_680)
        self.assertEqual(out["storage"][0]["free_bytes"], 44_207_837_184)
        self.assertEqual(out["storage"][0]["description"], "Internal storage")
        self.assertEqual(out["battery_percent"], 91)


class TestDetect(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="omni-phones-detect-"))
        self.ctx = Context(config={})

    def test_no_phone_hint(self):
        mkdev(self.root, "usb1", vendor="1d6b", product="0002", usb_class="09")
        result = phones.detect({}, self.ctx, root=self.root)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["phone_count"], 0)
        self.assertIn("charge-only", result.data["hint"])

    def test_phone_detected_no_probe(self):
        mkdev(self.root, "1-1", vendor="04e8", product="6860", manufacturer="S. Electronics",
              prodname="MTP", usb_class="00")
        result = phones.detect({"probe": False}, self.ctx, root=self.root)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["phone_count"], 1)
        self.assertEqual(result.data["phones"][0]["vendor_name"], "Samsung")
        self.assertIsNone(result.data["hint"])
        self.assertTrue(result.data["bridges"]["mtp"]["available"])

    def test_phone_silent_hint(self):
        # a phone on the bus, but we ask for probes on a machine with no mtp binary
        mkdev(self.root, "1-1", vendor="2717", product="ff08")
        with tempfile.TemporaryDirectory() as binpath:
            fakebin = Path(binpath)
            (fakebin / "mtp-detect").write_text("#!/bin/sh\nexit 255\n")
            (fakebin / "mtp-detect").chmod(0o755)
            (fakebin / "adb").write_text("#!/bin/sh\necho 'List of devices attached'; exit 0\n")
            (fakebin / "adb").chmod(0o755)
            env = {"PATH": f"{fakebin}:{os_path()}"}
            old_path = temp_path(env)
            try:
                result = phones.detect({"probe": True}, self.ctx, root=self.root)
            finally:
                old_path()
        self.assertTrue(result.ok)
        self.assertIn("silent", result.data["hint"])


def os_path() -> str:
    import os

    return os.environ.get("PATH", "")


def temp_path(env: dict[str, str]):
    import os

    old = os.environ.get("PATH")

    def apply():
        os.environ["PATH"] = env["PATH"]

    def restore():
        if old is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old

    apply()
    return restore


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
