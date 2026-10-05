import hashlib
import json
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_marketplace_candidate import verify_candidate


class MarketplaceCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {
            "name": "example-room",
            "publisher": "example-owner",
            "version": "1.2.3",
            "displayName": "Example Room",
            "description": "Example extension",
        }
        self.manifest_path = self.root / "package.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.vsix = self.root / "accepted.vsix"
        self.write_vsix()
        self.promotion_path = self.root / "release-promotion.json"
        self.write_promotion()

    def write_vsix(self, *, package=None, publisher="example-owner", version="1.2.3"):
        package = package or self.manifest
        package_xml = (
            '<PackageManifest xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">'
            f'<Metadata><Identity Id="{package["name"]}" Publisher="{publisher}" Version="{version}" />'
            "</Metadata></PackageManifest>"
        )
        with zipfile.ZipFile(self.vsix, "w") as archive:
            archive.writestr("extension/package.json", json.dumps(package))
            archive.writestr("extension.vsixmanifest", package_xml)
            archive.writestr("extension/readme.md", "synthetic readme")

    def write_promotion(self, *, version="1.2.3"):
        self.promotion_path.write_text(
            json.dumps({"version": version, "sha256": hashlib.sha256(self.vsix.read_bytes()).hexdigest()}),
            encoding="utf-8",
        )

    def test_accepts_matching_promoted_package_and_identity(self):
        result = verify_candidate(self.vsix, self.promotion_path, self.manifest_path)
        self.assertEqual(result["extension_id"], "example-owner.example-room")
        self.assertEqual(result["version"], "1.2.3")

    def test_rejects_bytes_that_differ_from_promotion_digest(self):
        with self.vsix.open("ab") as candidate:
            candidate.write(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            verify_candidate(self.vsix, self.promotion_path, self.manifest_path)

    def test_rejects_embedded_identity_mismatch(self):
        self.write_vsix(publisher="other-owner")
        self.write_promotion()
        with self.assertRaisesRegex(ValueError, "identity"):
            verify_candidate(self.vsix, self.promotion_path, self.manifest_path)

    def test_rejects_promotion_version_mismatch(self):
        self.write_promotion(version="1.2.2")
        with self.assertRaisesRegex(ValueError, "promotion version"):
            verify_candidate(self.vsix, self.promotion_path, self.manifest_path)

    def test_rejects_malformed_promotion_checksum(self):
        self.promotion_path.write_text(json.dumps({"version": "1.2.3", "sha256": "not-a-sha256"}))
        with self.assertRaisesRegex(ValueError, "invalid promotion checksum"):
            verify_candidate(self.vsix, self.promotion_path, self.manifest_path)

    def test_rejects_duplicate_archive_members(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.vsix, "a") as archive:
                archive.writestr("extension/package.json", json.dumps(self.manifest))
        self.write_promotion()
        with self.assertRaisesRegex(ValueError, "duplicate archive members"):
            verify_candidate(self.vsix, self.promotion_path, self.manifest_path)

    def test_rejects_archive_crc_failure(self):
        with zipfile.ZipFile(self.vsix) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        contents["extension/readme.md"] = b"synthetic readme"
        with zipfile.ZipFile(self.vsix, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, body in contents.items():
                archive.writestr(name, body)
        data = bytearray(self.vsix.read_bytes())
        with zipfile.ZipFile(self.vsix) as archive:
            info = archive.getinfo("extension/readme.md")
        data[info.header_offset + 30 + len(info.filename) + len(info.extra)] ^= 0x01
        self.vsix.write_bytes(data)
        self.write_promotion()
        with self.assertRaisesRegex(ValueError, "integrity failure"):
            verify_candidate(self.vsix, self.promotion_path, self.manifest_path)

    def test_hashing_does_not_read_entire_candidate_into_memory(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            result = verify_candidate(self.vsix, self.promotion_path, self.manifest_path)
        self.assertEqual(result["sha256"], json.loads(self.promotion_path.read_text())["sha256"])


if __name__ == "__main__":
    unittest.main()
