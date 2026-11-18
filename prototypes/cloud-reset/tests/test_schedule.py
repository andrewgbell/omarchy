from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader("schedule_recovery", str(PROJECT / "bin/schedule-recovery"))
scheduler = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(scheduler)
GUID = "77c96964-821a-48d7-9677-45f598259db3"


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.esp = self.work / "esp"
        (self.esp / "EFI").mkdir(parents=True)
        self.receipt = self.work / "transaction"
        self.uki = self.work / "input.efi"
        self.uki.write_bytes(b"fixture")
        self.calls = []
        self.order = "0001,0002"
        self.next_boot = None
        self.entries = {}
        self.enterContext(patch.object(scheduler, "output", side_effect=self.efi))
        self.verify = self.enterContext(patch.object(scheduler, "verify_uki"))

    def efi(self, tool, *args):
        self.assertEqual(tool, "efibootmgr")
        self.calls.append(args)
        if args == ("--verbose",):
            text = f"BootOrder: {self.order}\n"
            if self.next_boot:
                text += f"BootNext: {self.next_boot}\n"
            return text + "\n".join(f"Boot{k}* {label}\t{device}" for k, (label, device) in self.entries.items())
        if args[0] == "--create-only":
            self.assertEqual(args[1:5], ("--disk", "/dev/vda", "--part", "1"))
            self.entries["0003"] = (args[6], f"HD(1,GPT,{GUID},0x800,0x10000)/" + args[8])
        elif args[0] == "--bootnext":
            self.next_boot = args[1]
        elif args == ("--delete-bootnext",):
            self.next_boot = None
        elif args == ("--bootnum", "0003", "--delete-bootnum"):
            del self.entries["0003"]
        else:
            self.fail(f"unexpected firmware mutation: {args}")

    def arm(self):
        return scheduler.schedule(self.esp, self.receipt, "/dev/vda", "1", GUID, self.uki)

    def cancel(self):
        scheduler.cancel(self.esp, self.receipt, "1", GUID)

    def test_one_time_boot_and_clean_cancel(self):
        self.assertEqual(self.arm(), "0003")
        self.assertEqual(self.next_boot, "0003")
        self.assertEqual(self.order, "0001,0002")
        self.assertTrue(self.receipt.is_file())
        image = next(self.esp.rglob("recovery.efi"))
        self.verify.assert_called_once_with(image)
        self.assertEqual(image.read_bytes(), self.uki.read_bytes())
        with self.assertRaisesRegex(ValueError, "earlier recovery"):
            self.arm()
        self.cancel()
        self.assertIsNone(self.next_boot)
        self.assertEqual(self.entries, {})
        self.assertFalse(self.receipt.exists())
        self.assertEqual(list((self.esp / "EFI").iterdir()), [])

    def test_existing_bootnext_preserved(self):
        self.next_boot = "0002"
        with self.assertRaisesRegex(ValueError, "already scheduled"):
            self.arm()
        self.assertEqual(self.calls, [("--verbose",)])
        self.assertFalse(self.receipt.exists())

    def test_bad_uki_reclaims_staged_files_without_efi_write(self):
        self.verify.side_effect = ValueError("bad UKI")
        with self.assertRaisesRegex(ValueError, "bad UKI"):
            self.arm()
        self.assertFalse(self.receipt.exists())
        self.assertEqual(list((self.esp / "EFI").iterdir()), [])
        self.assertTrue(all(args == ("--verbose",) for args in self.calls))

    def test_firmware_failure_after_create_reclaims_entry(self):
        def fail_after_create(*args):
            result = self.efi(*args)
            if args[1] == "--create-only":
                raise OSError("firmware error after write")
            return result
        with patch.object(scheduler, "output", side_effect=fail_after_create):
            with self.assertRaisesRegex(OSError, "firmware error"):
                self.arm()
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.entries, {})

    def test_foreign_bootnext_preserved_by_cancel(self):
        self.arm()
        self.next_boot = "0001"
        self.cancel()
        self.assertEqual(self.next_boot, "0001")
        self.assertNotIn(("--delete-bootnext",), self.calls)

    def test_cancel_checks_esp_guid_entry_identity_and_persistent_order(self):
        self.arm()
        with self.assertRaisesRegex(ValueError, "receipt"):
            scheduler.cancel(self.esp, self.receipt, "1", "other-esp")
        label, path = self.entries["0003"]
        self.entries["0003"] = label, path.replace(GUID, "other-esp")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.cancel()
        self.entries["0003"] = label, path
        self.order += ",0003"
        with self.assertRaisesRegex(ValueError, "BootOrder"):
            self.cancel()
        self.assertTrue(self.receipt.exists())

    def test_device_selection(self):
        disk = dict(type="disk", path="/dev/vda", serial=scheduler.DISK_SERIAL, ro=False, label=None)
        esp = dict(type="part", path="/dev/vda1", pkname="/dev/vda", parttype=scheduler.ESP_TYPE,
                   partn=1, partuuid=GUID, ro=False, label="EFI")
        mount = {"target": "/boot", "fstype": "vfat", "source": "/dev/vda1", "vfs-options": "rw,nosuid"}
        self.assertEqual(scheduler.target([disk, esp, disk | {"path": "/dev/zram0"}], [mount]),
                         ("/dev/vda", "1", GUID))
        for devices, mounts in (([disk, disk | {"path": "/dev/vdb"}, esp], [mount]),
                                ([disk | {"serial": "other"}, esp], [mount]),
                                ([disk, esp | {"label": "CIDATA"}], [mount]),
                                ([disk, esp | {"pkname": "/dev/vdb"}], [mount]),
                                ([disk, esp], [mount | {"vfs-options": "ro"}])):
            with self.subTest(devices=devices, mounts=mounts), self.assertRaises(ValueError):
                scheduler.target(devices, mounts)

    def test_real_machine_rejected_before_firmware_access(self):
        with patch.object(scheduler.os, "geteuid", return_value=0), patch.object(scheduler, "output", return_value="none") as run:
            with self.assertRaisesRegex(ValueError, "prototype VM"):
                scheduler.guest_target()
            run.assert_called_once_with("systemd-detect-virt", "--vm")

if __name__ == "__main__":
    unittest.main()
