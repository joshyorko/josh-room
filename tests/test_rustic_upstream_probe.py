"""The native upstream proof must tolerate a Windows patch checkout."""

import subprocess

from scripts import rustic_windows_upstream_probe as probe


def test_native_patch_preparation_applies_crlf_checkout_to_exact_lf_source(tmp_path):
    source = tmp_path / "file.rs"
    source.write_bytes(b"old\n")
    patch = tmp_path / "checkout.patch"
    patch.write_bytes(b"--- a/file.rs\r\n+++ b/file.rs\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n")
    canonical = tmp_path / "canonical.patch"
    probe.prepare_patch(patch, canonical)
    subprocess.run(["git", "apply", "--unidiff-zero", str(canonical)], cwd=tmp_path, check=True)
    assert source.read_bytes() == b"new\n"
    assert canonical.read_bytes() == patch.read_bytes().replace(b"\r\n", b"\n")
