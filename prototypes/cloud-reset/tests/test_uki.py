from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader("cloud_uki", str(PROJECT / "bin/build-uki"))
uki = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(uki)


@unittest.skipUnless(uki.STUB.is_file() and shutil.which("objcopy"), "requires systemd-stub and objcopy")
class UkITest(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.out = self.work / "recovery.efi"
        self.bundle = self.work / "bundle"
        self.bundle.mkdir()
        (self.bundle / "cmdline").write_bytes((PROJECT / "recovery/cmdline").read_bytes())
        (self.bundle / "initramfs.img").write_bytes(b"initrd section fixture")
        # A small PE fixture exercises packaging on hosts without a packaged
        # x86 kernel. It is deliberately not a bootable kernel.
        fixture = bytearray(uki.STUB.read_bytes())
        fixture[0x202:0x206] = b"HdrS"
        (self.bundle / "vmlinuz").write_bytes(fixture)

    def test_sections_and_default_command_line(self):
        uki.build(self.bundle, self.out)
        if shutil.which("bootctl"):
            self.assertEqual(subprocess.check_output(["bootctl", "kernel-identify", self.out]), b"uki\n")
        wanted = {".linux": (self.bundle / "vmlinuz").read_bytes(),
                  ".initrd": (self.bundle / "initramfs.img").read_bytes(),
                  ".osrel": uki.OS_RELEASE,
                  ".cmdline": (self.bundle / "cmdline").read_bytes().rstrip() + b"\0"}
        for name, data in wanted.items():
            with self.subTest(section=name):
                destination = self.work / "section"
                # Dump from a copy. GNU objcopy also rewrites the input file
                # when --dump-section receives no distinct output path.
                subprocess.run(["objcopy", "--dump-section", f"{name}={destination}", self.out,
                                self.work / "inspection.efi"], check=True)
                self.assertEqual(destination.read_bytes(), data)

    def test_packaged_linux_pe_with_probe(self):
        kernels = sorted(Path("/usr/lib/modules").glob("*/vmlinuz"))
        if not kernels or not shutil.which("bootctl"):
            self.skipTest("no local packaged kernel or bootctl")
        kernel = next((p for p in kernels if "omarchy" in p.parent.name), kernels[0])
        (self.bundle / "vmlinuz").unlink()
        (self.bundle / "vmlinuz").symlink_to(kernel)
        subprocess.run([PROJECT / "bin/build-uki", "--bundle", self.bundle, "--output", self.out,
                        "--probe"], check=True)
        details = subprocess.check_output(["bootctl", "kernel-inspect", self.out], text=True)
        self.assertIn("omarchy_cloud_probe=1 console=tty0 console=ttyS0,115200", details)
        self.assertIn("Omarchy cloud reset installer", details)

    def test_rejects_foreign_kernel_and_changed_command_line(self):
        kernel = self.bundle / "vmlinuz"
        original = kernel.read_bytes()
        offset = struct.unpack_from("<I", original, 60)[0]
        foreign = bytearray(original)
        struct.pack_into("<H", foreign, offset + 4, 0xaa64)
        kernel.write_bytes(foreign)
        with self.assertRaisesRegex(ValueError, "x86-64"):
            uki.build(self.bundle, self.out)
        kernel.write_bytes(original)
        (self.bundle / "cmdline").write_text("init=/bin/sh\n")
        with self.assertRaisesRegex(ValueError, "options"):
            uki.build(self.bundle, self.out)
        self.assertFalse(self.out.exists())

    def test_failed_build_and_existing_artifact(self):
        (self.bundle / "initramfs.img").write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "initrd"):
            uki.build(self.bundle, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.work.glob(".omarchy-uki-*")), [])
        self.out.write_bytes(b"previous build")
        with self.assertRaisesRegex(ValueError, "already exists"):
            uki.build(self.bundle, self.out)
        self.assertEqual(self.out.read_bytes(), b"previous build")
