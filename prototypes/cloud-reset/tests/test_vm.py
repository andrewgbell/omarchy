import contextlib
from http.client import HTTPConnection
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest


LOADER = SourceFileLoader("cloud_reset_vm", str(Path(__file__).resolve().parents[1] / "bin/test-vm"))
VM = module_from_spec(spec_from_loader(LOADER.name, LOADER))
LOADER.exec_module(VM)


class TestVM(unittest.TestCase):
    def request(self, server, path, method="GET"):
        # Run the actual HTTP parser/handler even in sandboxes that block
        # socket sends. The TCP integration test below exercises the same path.
        message = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        output = io.BytesIO()
        stream = SimpleNamespace(makefile=lambda *args: io.BytesIO(message.encode()), sendall=output.write)
        VM.InstallerHTTP(stream, ("127.0.0.1", 1), server)
        header, body = output.getvalue().split(b"\r\n\r\n", 1)
        return int(header.split()[1]), body

    def test_serves_only_pinned_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp, "installer.iso")
            image.write_bytes(b"verified installer")
            with image.open("rb") as pinned:
                server = SimpleNamespace(image_fd=pinned.fileno(), image_size=image.stat().st_size,
                                         transferred=threading.Event(), read_lock=threading.Lock())
                image.unlink()
                image.write_bytes(b"replacement")
                for path in ("/", "/installer.iso?foo", "//installer.iso", "/some-file"):
                    self.assertEqual(self.request(server, path)[0], 404)
                self.assertEqual(self.request(server, "/installer.iso", "HEAD"), (200, b""))
                self.assertFalse(server.transferred.is_set())
                self.assertEqual(self.request(server, "/installer.iso"), (200, b"verified installer"))
                self.assertTrue(server.transferred.is_set())

    def test_loopback_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp, "installer.iso")
            image.write_bytes(b"fixture")
            with contextlib.ExitStack() as stack:
                try:
                    server = stack.enter_context(VM.image_server(image))
                except PermissionError as err:
                    self.skipTest(f"IPv4 listener unavailable: {err}")
                self.assertEqual(server.server_address[0], "127.0.0.1")
                with contextlib.closing(HTTPConnection(*server.server_address, timeout=2)) as connection:
                    connection.request("GET", "/installer.iso")
                    self.assertEqual(connection.getresponse().read(), b"fixture")
                    self.assertTrue(server.transferred.wait(1))

    def test_requires_full_transfer_and_live_marker(self):
        fetched = threading.Event()
        ok = [sys.executable, "-c", 'print("OMARCHY_CLOUD_RESET_LIVE_OK")']
        with self.assertRaisesRegex(ValueError, "without receiving"):
            VM.boot(ok, fetched, 5)
        fetched.set()
        VM.boot(ok, fetched, 5)
        for output in ["OMARCHY_CLOUD_RESET_MEDIA_VERIFIED", "Kernel command line: OMARCHY_CLOUD_RESET_LIVE_OK"]:
            with self.assertRaisesRegex(ValueError, "exited before"):
                VM.boot([sys.executable, "-c", f"print({output!r})"], fetched, 5)
        for marker in ["OMARCHY_CLOUD_RESET_FAILED: ISO digest mismatch", "OMARCHY_CLOUD_RESET_LIVE_FAILED"]:
            with self.assertRaisesRegex(ValueError, marker):
                VM.boot([sys.executable, "-c", f"print({marker!r})"], fetched, 5)

    def test_stops_process_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp, "pid")
            script = 'import os, sys, time; open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(60)'
            with self.assertRaisesRegex(ValueError, "timed out"):
                VM.boot([sys.executable, "-c", script, pidfile], threading.Event(), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    def test_guest_attached_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            esp = Path(tmp) / "esp"
            command = VM.qemu_command("qemu", "/read/OVMF.fd", "/vm,local/vars.fd", "/vm/disk",
                                      esp, 10, False)
            drives = [command[i + 1] for i, arg in enumerate(command) if arg == "-drive"]
            self.assertEqual(drives, [
                "if=pflash,unit=0,format=raw,readonly=on,file=/read/OVMF.fd",
                "if=pflash,unit=1,format=raw,file=/vm,,local/vars.fd",
                "if=none,id=target,format=raw,file=/vm/disk",
                f"if=none,id=recovery_esp,format=raw,readonly=on,file=fat:16:{esp}",
            ])
            self.assertIn("q35,accel=kvm:tcg", command)
            self.assertIn("virtio-blk-pci,drive=recovery_esp,bootindex=1", command)
            for option in ("-kernel", "-initrd", "-append"):
                self.assertNotIn(option, command)
            self.assertNotIn(VM.LIVE_OK.decode(), " ".join(command))


if __name__ == "__main__":
    unittest.main()
