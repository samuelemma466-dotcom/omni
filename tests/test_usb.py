from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "usb"
sys.path.insert(0, str(PLUGIN))

_spec = importlib.util.spec_from_file_location("usb_handler", PLUGIN / "handler.py")
assert _spec and _spec.loader
usb = importlib.util.module_from_spec(_spec)
sys.modules["usb_handler"] = usb
_spec.loader.exec_module(usb)

from omni.registry import Context  # noqa: E402


def _device(root: Path, name: str, vendor_id: str, product_id: str, **extra: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "idVendor").write_text(vendor_id + "\n")
    (directory / "idProduct").write_text(product_id + "\n")
    (directory / "busnum").write_text("1\n")
    (directory / "devnum").write_text("4\n")
    for key, value in extra.items():
        (directory / key).write_text(value + "\n")


def _lsblk() -> dict:
    return {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "path": "/dev/nvme0n1",
                "type": "disk",
                "size": 500_000_000_000,
                "tran": "nvme",
                "rm": False,
                "hotplug": False,
                "mountpoints": [None],
                "children": [
                    {
                        "name": "nvme0n1p2",
                        "path": "/dev/nvme0n1p2",
                        "type": "part",
                        "size": 499_000_000_000,
                        "mountpoints": ["/"],
                        "children": [],
                    }
                ],
            },
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "type": "disk",
                "size": 32_000_000_000,
                "tran": "usb",
                "rm": True,
                "hotplug": True,
                "mountpoints": [None],
                "children": [
                    {
                        "name": "sdb1",
                        "path": "/dev/sdb1",
                        "type": "part",
                        "size": 31_000_000_000,
                        "mountpoints": ["/run/media/biggiesam/USB"],
                        "children": [],
                    }
                ],
            },
        ]
    }


class UsbEnumerationTests(unittest.TestCase):
    def test_enumerates_and_sorts(self):
        root = Path(tempfile.mkdtemp(prefix="omni-sysusb-"))
        _device(root, "2-1", "132b", "2342", manufacturer="KONICA MINOLTA", product="C3851Series")
        _device(root, "1-2", "046d", "c52b", manufacturer="Logitech", product="Unifying Receiver")
        (root / "usb1").mkdir()  # bus node without ids, must be ignored
        devices = usb.enumerate_usb(root)
        self.assertEqual([d["vendor_id"] for d in devices], ["046d", "132b"])
        self.assertEqual(devices[1]["manufacturer"], "KONICA MINOLTA")
        self.assertEqual(devices[1]["product_id"], "2342")

    def test_recipes_match_connected_device(self):
        devices = [
            {
                "vendor_id": "132b",
                "product_id": "2342",
                "product": "C3851Series",
            }
        ]
        matched = usb.match_recipes(devices, usb.load_recipes())
        bizhub = [r for r in matched if r["id"] == "bizhub-c3351"]
        self.assertEqual(len(bizhub), 1)
        self.assertTrue(bizhub[0]["connected"])


class FlashGuardTests(unittest.TestCase):
    def _validate(self, **overrides):
        kwargs = dict(
            image_size=100,
            image="/tmp/omni.iso",
            device="/dev/sdb",
            data=_lsblk(),
            confirm=None,
            unmount=True,
            allow_internal=False,
            dry_run=True,
        )
        kwargs.update(overrides)
        return usb.validate_flash_plan(**kwargs)

    def test_plan_for_usb_disk(self):
        plan, error = self._validate()
        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        actions = [step["action"] for step in plan["plan"]]
        self.assertEqual(actions, ["unmount", "write", "sync", "reread"])

    def test_refuses_running_system_disk(self):
        plan, error = self._validate(device="/dev/nvme0n1")
        self.assertIsNone(plan)
        self.assertIn("running system", error)

    def test_refuses_mounted_without_unmount(self):
        plan, error = self._validate(unmount=False)
        self.assertIsNone(plan)
        self.assertIn("mounted", error)

    def test_refuses_oversized_image(self):
        plan, error = self._validate(image_size=64_000_000_000)
        self.assertIsNone(plan)
        self.assertIn("larger", error)

    def test_refuses_non_removable_without_override(self):
        data = _lsblk()
        data["blockdevices"][1]["tran"] = "sata"
        data["blockdevices"][1]["rm"] = False
        data["blockdevices"][1]["hotplug"] = False
        plan, error = self._validate(data=data)
        self.assertIsNone(plan)
        self.assertIn("not removable", error)

    def test_real_write_requires_confirmation(self):
        plan, error = self._validate(dry_run=False)
        self.assertIsNone(plan)
        self.assertIn("confirmation", error)

    def test_real_write_with_confirmation(self):
        plan, error = self._validate(dry_run=False, confirm="/dev/sdb")
        self.assertIsNone(error)
        self.assertEqual(plan["device"]["path"], "/dev/sdb")


class RecipePlanTests(unittest.TestCase):
    def test_expand_placeholders(self):
        out = usb._expand({"argv": ["usermod", "-aG", "lp", "{user}"], "cwd": "{tmp}"}, {"user": "sam", "tmp": "/tmp/x"})
        self.assertEqual(out["argv"][-1], "sam")
        self.assertEqual(out["cwd"], "/tmp/x")

    def test_pacman_step_is_privileged(self):
        step = usb._plan_step({"kind": "pacman", "packages": ["cups"]})
        self.assertTrue(step["sudo"])
        self.assertIn("--noconfirm", step["argv"])

    def test_install_driver_dry_run(self):
        ctx = Context(config={}, dry_run=True)
        result = usb.install_driver({"recipe": "bizhub-c3351", "dry_run": True, "force": True}, ctx)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["dry_run"])
        kinds = [step["kind"] for step in result.data["steps"]]
        self.assertIn("pacman", kinds)
        self.assertIn("cups_usb_queue", kinds)


if __name__ == "__main__":
    unittest.main()
