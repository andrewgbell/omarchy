import hashlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
loader = SourceFileLoader("prepare_installer", str(HERE.parent / "bin/prepare-installer"))
installer = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(installer)

# Detached signature made by a disposable signing subkey. Only the public key
# and signature are retained, so real gpgv can run even without a key agent.
KEY = HERE / "fixtures/test-signing-key.gpg"
SIGNATURE = (HERE / "fixtures/test-image.sig").read_bytes()
PRIMARY = "55744AB4C542A18E0DE48E7F13F599824D13C249"
DATA = b"Cloud reset verifier fixture\n"
NAME = "omarchy-4.5.0-x86.iso"


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.image_bytes = DATA
        self.iso_downloads = 0
        self.enterContext(patch.object(installer, "PRIMARY_KEY", PRIMARY))
        self.enterContext(patch.object(installer, "fetch", side_effect=self.fixture_server))

    def fixture_server(self, url, destination=None, limit=None):
        if url in (installer.RELEASES + "/latest", installer.RELEASES + "/tags/v4.5.0"):
            return b'{"tag_name": "v4.5.0", "prerelease": false, "draft": false}'
        if url == f"{installer.IMAGES}/{NAME}.sha256":
            return f"{hashlib.sha256(DATA).hexdigest()}  {NAME}\n".encode()
        if url == f"{installer.IMAGES}/{NAME}.sig":
            return destination.write_bytes(SIGNATURE)
        self.assertEqual(url, f"{installer.IMAGES}/{NAME}")
        self.iso_downloads += 1
        return destination.write_bytes(self.image_bytes)

    def test_fetch_reuse_and_offline_signature_with_signing_subkey(self):
        image = installer.prepare(self.cache, KEY)
        self.assertEqual(image.read_bytes(), DATA)
        self.assertEqual(installer.prepare(self.cache, KEY, "4.5.0"), image)
        self.assertEqual(self.iso_downloads, 1)
        self.assertEqual(installer.verify_file(image, KEY), image)
        image.write_bytes(DATA + b"corrupt")
        with self.assertRaisesRegex(ValueError, "valid signature"):
            installer.verify_file(image, KEY)
        self.assertEqual(installer.prepare(self.cache, KEY).read_bytes(), DATA)
        self.assertEqual(self.iso_downloads, 2)

    def test_valid_signature_from_unpinned_primary_never_published(self):
        with patch.object(installer, "PRIMARY_KEY", "F" * 40):
            with self.assertRaisesRegex(ValueError, "Omarchy release key"):
                installer.prepare(self.cache, KEY)
        self.assertFalse((self.cache / "4.5.0").exists())

    def test_truncated_download_never_published(self):
        self.image_bytes = b"partial"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            installer.prepare(self.cache, KEY)
        self.assertFalse((self.cache / "4.5.0").exists())

    def test_prerelease_tag_mismatch_and_unsupported_release(self):
        metadata = {"tag_name": "v4.5.0", "draft": False, "prerelease": False}
        for override, requested in [
                ({"draft": True}, None), ({"prerelease": True}, None),
                ({"tag_name": "v4.5.1-rc1"}, None), ({"tag_name": "v4.0.3"}, None),
                ({}, "4.5.1")]:
            with self.subTest(override=override, requested=requested):
                with patch.object(installer, "fetch", return_value=json.dumps(metadata | override)):
                    with self.assertRaises(ValueError):
                        installer.stable_version(requested)

    def test_wrong_architecture_sidecar_rejected(self):
        sidecar = b"0" * 64 + b"  omarchy-4.5.0-aarch64.iso\n"
        with self.assertRaisesRegex(ValueError, "different image"):
            installer.expected_checksum(sidecar, NAME)


if __name__ == "__main__":
    unittest.main()
