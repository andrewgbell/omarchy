import gzip
import hashlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader("cloud_bundle", str(PROJECT / "bin/build-recovery"))
builder = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(builder)


def archive(root, filename, kind):
    subprocess.run(["bsdtar", f"--format={kind}", "-cf", filename, "."], cwd=root, check=True)


class BootBundleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bundle = self.work / "out"
        self.bundle.mkdir()
        self.iso = self.work / "omarchy-4.5.0-x86.iso"
        self.tree = self.work / "disc"
        self.media = self.tree / builder.MEDIA
        self.media.mkdir(parents=True)
        for name in ("airootfs.sfs", "omarchy-root.img.zst", "omarchy-root.img.zst.sha256"):
            (self.media / name).write_text(name)
        boot = self.tree / builder.BOOT
        boot.mkdir(parents=True)
        self.kernel = boot / "vmlinuz-linux-omarchy"
        self.kernel.write_bytes(b"x86 kernel fixture")
        self.init = boot / "initramfs-linux-omarchy.img"
        initroot = self.work / "initroot"
        hooks = initroot / "hooks"
        hooks.mkdir(parents=True)
        for name in ("archiso", "archiso_pxe_common", "archiso_pxe_http"):
            (hooks / name).write_text("stock hook fixture")
        cpio = self.work / "original.cpio"
        archive(initroot, cpio, "newc")
        self.init.write_bytes(gzip.compress(cpio.read_bytes()))
        self.url = "http://10.0.2.2:8010/installer.iso"

    def stage(self):
        archive(self.tree, self.iso, "iso9660")
        builder.stage_bundle(self.iso, self.url, self.work, self.bundle)

    def test_appends_recovery_to_installer_initramfs(self):
        self.stage()
        self.assertEqual((self.bundle / "vmlinuz").read_bytes(), self.kernel.read_bytes())
        combined = (self.bundle / "initramfs.img").read_bytes()
        original = self.init.read_bytes()
        self.assertEqual(combined[:len(original)], original)
        offset = (len(original) + 3) & ~3
        self.assertEqual(combined[offset:offset + 6], b"070701")
        unpacked = self.work / "appended"
        unpacked.mkdir()
        subprocess.run(["bsdtar", "-xf", "-"], cwd=unpacked, input=combined[offset:], check=True)
        iso_bytes = self.iso.read_bytes()
        self.assertEqual((unpacked / "etc/omarchy-cloud-reset/media").read_text().splitlines(),
                         [self.url, hashlib.sha256(iso_bytes).hexdigest(), str(len(iso_bytes))])
        self.assertEqual((unpacked / builder.HOOK).read_bytes(),
                         (PROJECT / "recovery/archiso_pxe_http").read_bytes())
        self.assertTrue(os.access(unpacked / builder.HOOK, os.X_OK))
        self.assertTrue((unpacked / "omarchy-probe.service").is_file())
        self.assertIn("copytoram=n", (self.bundle / "cmdline").read_text())

    def test_partial_install_medium_is_rejected(self):
        (self.media / "omarchy-root.img.zst").unlink()
        with self.assertRaisesRegex(ValueError, "install image"):
            self.stage()

    def test_firmware_image_contains_the_recovery_hook_and_pinned_media(self):
        kernels = sorted(Path("/usr/lib/modules").glob("*omarchy*/vmlinuz"))
        if not kernels or not shutil.which("bootctl") or not shutil.which("objcopy"):
            self.skipTest("requires an installed Omarchy kernel, systemd and binutils")
        shutil.copyfile(kernels[-1], self.kernel)
        self.stage()
        efi = self.work / "recovery.efi"
        subprocess.run([PROJECT / "bin/build-uki", "--bundle", self.bundle, "--output", efi], check=True)
        self.assertEqual(subprocess.check_output(["bootctl", "kernel-identify", efi], text=True).strip(), "uki")
        initrd = self.work / "efi-initrd"
        subprocess.run(["objcopy", "--dump-section", f".initrd={initrd}", efi,
                        self.work / "checked.efi"], check=True)
        offset = (self.init.stat().st_size + 3) & ~3
        overlay = initrd.read_bytes()[offset:]
        config = subprocess.check_output(["bsdtar", "-xOf", "-", "etc/omarchy-cloud-reset/media"],
                                         input=overlay).decode().splitlines()
        with self.iso.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        self.assertEqual(config, [self.url, digest, str(self.iso.stat().st_size)])
        actual_hook = subprocess.check_output(["bsdtar", "-xOf", "-", builder.HOOK], input=overlay)
        self.assertEqual(actual_hook, (PROJECT / "recovery/archiso_pxe_http").read_bytes())

    def test_no_network_hooks_is_rejected(self):
        (self.work / "initroot/hooks/archiso_pxe_common").unlink()
        archive(self.work / "initroot", self.work / "original.cpio", "newc")
        self.init.write_bytes(gzip.compress((self.work / "original.cpio").read_bytes()))
        with self.assertRaisesRegex(ValueError, "network hooks"):
            self.stage()

    def test_missing_signature_publishes_nothing(self):
        archive(self.tree, self.iso, "iso9660")
        out = self.work / "missing"
        with self.assertRaises(FileNotFoundError):
            builder.build(self.iso, None, out)
        self.assertFalse(out.exists())
        self.assertEqual(list(self.work.glob(".omarchy-recovery-*")), [])

    def test_failed_signature_stops_before_extraction(self):
        archive(self.tree, self.iso, "iso9660")
        Path(str(self.iso) + ".sig").write_bytes(b"invalid detached signature")
        with patch.object(builder, "stage_bundle", side_effect=AssertionError("reached extraction")):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.build(self.iso, None, self.work / "invalid")
        self.assertFalse((self.work / "invalid").exists())

    def test_user_facing_builder_propagates_a_rejected_signature(self):
        archive(self.tree, self.iso, "iso9660")
        Path(str(self.iso) + ".sig").write_bytes(b"invalid signature")
        output = self.work / "recovery.efi"
        attempt = subprocess.run([PROJECT / "bin/omarchy-cloud-reset", "--iso", self.iso,
                                  "--output", output], capture_output=True, text=True)
        self.assertNotEqual(attempt.returncode, 0)
        self.assertIn("signature", attempt.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.work.glob(".cloud-reset-*")), [])

    def test_refuses_other_endpoint_and_existing_output(self):
        for url in ("https://example.org/omarchy.iso", "http://10.0.2.2:65536/installer.iso"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "URL"):
                builder.build(self.iso, url, self.work / "bad")
        with self.assertRaisesRegex(ValueError, "already exists"):
            builder.build(self.iso, self.url, self.bundle)
