"""Exercise the destructive acceptance harness only against synthetic guests."""

from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
loader = SourceFileLoader("test_cloud_install", str(BIN / "test-install"))
run = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(run)


class InstallTest(unittest.TestCase):
    def test_cidata_is_labeled_and_targets_only_the_disposable_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            seed = run.make_cidata(scratch, "unused-test-password")
            with seed.open("rb") as image:
                image.seek(16 * 2048 + 40)
                self.assertEqual(image.read(32).strip(), b"CIDATA")

            def contents(member):
                return subprocess.check_output(["bsdtar", "-xOf", seed, "./" + member], text=True)

            config = json.loads(contents("user_configuration.json"))
            disks = config["disk_config"]["device_modifications"]
            self.assertEqual([(disk["device"], disk["wipe"]) for disk in disks], [("/dev/vda", True)])
            boot, root = disks[0]["partitions"]
            self.assertEqual(root["start"]["value"], boot["start"]["value"] + boot["size"]["value"])
            self.assertEqual(root["start"]["value"] + root["size"]["value"] + 2**20, 64 * 2**30)
            creds = json.loads(contents("user_credentials.json"))
            self.assertEqual(creds["users"][0]["username"], run.USER)
            self.assertNotIn("unused-test-password", str(creds))
            self.assertTrue(creds["users"][0]["enc_password"].startswith("$6$"))
            self.assertEqual(contents("authorized_keys"), (scratch / "guest-key.pub").read_text())
            self.assertEqual(contents("user_encrypt_installation.txt"), "false\n")

    def test_disk_only_and_one_time_recovery_have_no_boot_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            args = ("qemu", scratch / "OVMF.fd", scratch, 12)
            for seed, port in ((scratch / "cidata.iso", None), (None, 23456)):
                command = run.qemu_args(*args, seed=seed, ssh_port=port)
                serialized = " ".join(command)
                self.assertNotIn("-kernel", command)
                self.assertNotIn("-append", command)
                self.assertNotIn("bootindex", serialized)
                self.assertNotIn("/dev/", serialized)
                self.assertIn(f"serial={run.vm.TARGET_SERIAL}", serialized)
                self.assertIn(run.vm.VM_UUID, command)
                devices = [command[i + 1] for i, word in enumerate(command) if word == "-drive"]
                self.assertEqual(sum("id=target" in dev for dev in devices), 1)
                self.assertTrue(all("readonly=on" in dev for dev in devices if "media=cdrom" in dev))
                if port:
                    self.assertIn("127.0.0.1:23456-:22", serialized)
            control = run.qemu_args(*args, medium=scratch / "release.iso", seed=scratch / "answers.iso")
            self.assertIn("scsi-cd,bus=scsi.0,drive=installer,bootindex=1", control)

    def test_installed_query_checks_version_old_marker_and_configuration(self):
        with patch.object(run.subprocess, "check_output", return_value="omarchy 4.5.0-3\n") as remote:
            self.assertEqual(run.installed_manifest(["ssh"], "4.5.0"), ["omarchy 4.5.0-3"])
            script = remote.call_args.args[0][-1]
            for word in ("FSROOT", "btrfs", run.MARKER, "4.5.0", run.BINDINGS, "pacman -Q"):
                self.assertIn(word, script)
        run.compare(["same"], ["same"])
        with self.assertRaisesRegex(ValueError, "manifests differ"):
            run.compare(["one"], ["two"])

    def test_second_boot_fails_without_complete_media_transfer(self):
        phases = []
        @contextmanager
        def fake_guest(command, scratch, phase):
            phases.append(phase)
            yield SimpleNamespace()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            firmware = root / "vars"
            firmware.write_bytes(b"ovmf")
            server = SimpleNamespace(image_size=4096, server_port=1234,
                                     transferred=SimpleNamespace(is_set=lambda: False))
            @contextmanager
            def server_context(image):
                yield server
            with (patch.object(run.vm, "dependencies", return_value=("qemu", root / "code", firmware)),
                  patch.object(run.vm, "image_server", server_context),
                  patch.object(run.vm, "make_recovery_esp", create=True),
                  patch.object(run, "make_cidata", return_value=root / "seed"),
                  patch.object(run, "guest", fake_guest), patch.object(run, "free_port", return_value=32000),
                  patch.object(run, "wait_reboot"), patch.object(run, "wait_login"),
                  patch.object(run, "installed_manifest", return_value=["same"]),
                  patch.object(run, "arm_reset"), patch.object(run.subprocess, "run"),
                  patch.object(run.subprocess, "check_output", return_value="/tmp/omarchy-4.5.0-x86.iso\n")):
                with self.assertRaisesRegex(ValueError, "did not fetch"):
                    run.run(SimpleNamespace(iso=None, version="4.5.0", work_dir=root, timeout=2))
            self.assertEqual(phases, ["Installing USB-equivalent control", "Arming recovery in installed guest",
                                      "Installing via one-time cloud reset"])


if __name__ == "__main__":
    unittest.main()
