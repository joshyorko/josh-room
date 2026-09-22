import json
import os
import stat
from array import array
from pathlib import Path

import pytest

import josh_room.material_security as security
from josh_room.material_security import (
    AdapterDeclaration,
    Decision,
    DecisionReceipt,
    MaterialCandidate,
    MaterialClass,
    ReasonCode,
    ReceiptError,
    classify_material,
    safe_diagnostic,
    scan_content,
    write_receipt,
)


def _declaration(root: Path, *, classes=None, adapter="synthetic-adapter"):
    return AdapterDeclaration(
        adapter=adapter,
        allowed_roots=(root,),
        allowed_classes=frozenset(classes or {MaterialClass.SESSION_TRANSCRIPT}),
    )


def _candidate(
    root: Path,
    relative,
    *,
    material_class=MaterialClass.SESSION_TRANSCRIPT,
    observed_type=None,
    metadata=None,
    source_kind="codex.transcript",
):
    return MaterialCandidate(
        adapter="synthetic-adapter",
        source_kind=source_kind,
        material_class=material_class,
        path=str(root / relative),
        approved_root=root,
        observed_type=observed_type,
        metadata=metadata,
    )


def test_safe_transcript_file_is_allowed_and_receipt_is_opaque(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text('{"role":"user","text":"synthetic hello"}\n')

    receipt = classify_material(_candidate(tmp_path, "session.jsonl"), _declaration(tmp_path))

    assert receipt.decision is Decision.ALLOW
    assert receipt.material_class is MaterialClass.SESSION_TRANSCRIPT
    assert receipt.reason_code is ReasonCode.ALLOWED
    assert set(receipt.to_dict()) == {"adapter", "material_class", "decision", "reason_code", "count", "digest"}


def test_missing_safe_path_is_quarantined_even_with_adapter_type_claim(tmp_path):
    receipt = classify_material(_candidate(tmp_path, "gone.jsonl", observed_type="regular"), _declaration(tmp_path))

    assert receipt.decision is Decision.QUARANTINE
    assert receipt.reason_code is ReasonCode.UNKNOWN_FILE_TYPE


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("auth.json", MaterialClass.AUTH_STORE),
        (".local/share/keyrings/login.keyring", MaterialClass.KEYRING),
        (".ssh/id_ed25519", MaterialClass.PRIVATE_KEY),
        (".ssh/renamed-private-material", MaterialClass.PRIVATE_KEY),
        (".gnupg/private-keys-v1.d", MaterialClass.PRIVATE_KEY),
        (".gnupg/private-keys-v1.d/opaque-key-blob", MaterialClass.PRIVATE_KEY),
        ("identity.age", MaterialClass.PRIVATE_KEY),
        (".kube/config", MaterialClass.KUBECONFIG),
        (".env", MaterialClass.ENV_SECRET),
        (".aws/credentials", MaterialClass.CLOUD_CREDENTIAL),
        (".config/gcloud/credentials.db", MaterialClass.CLOUD_CREDENTIAL),
        (".cloudflared/config.yml", MaterialClass.CLOUD_CREDENTIAL),
        (".config/cloudflared/cert.pem", MaterialClass.CLOUD_CREDENTIAL),
        (".git-credentials", MaterialClass.GIT_CREDENTIAL),
        (".config/git/credentials", MaterialClass.GIT_CREDENTIAL),
        ("git-credential-store", MaterialClass.GIT_CREDENTIAL),
        (".netrc", MaterialClass.CLOUD_CREDENTIAL),
        (".docker/config.json", MaterialClass.CLOUD_CREDENTIAL),
        (".npmrc", MaterialClass.CLOUD_CREDENTIAL),
        (".terraform.d/credentials.tfrc.json", MaterialClass.CLOUD_CREDENTIAL),
        (".config/gh/hosts.yml", MaterialClass.CLOUD_CREDENTIAL),
        (".config/rclone/rclone.conf", MaterialClass.CLOUD_CREDENTIAL),
        (".config/containers/auth.json", MaterialClass.CLOUD_CREDENTIAL),
        (".config/helm/registry", MaterialClass.CLOUD_CREDENTIAL),
        (".config/helm/registry/opaque-credential-name", MaterialClass.CLOUD_CREDENTIAL),
        ("Cookies", MaterialClass.BROWSER_CREDENTIAL),
        ("Cookies-journal", MaterialClass.BROWSER_CREDENTIAL),
        ("Cookies-wal", MaterialClass.BROWSER_CREDENTIAL),
        ("cookies.sqlite-shm", MaterialClass.BROWSER_CREDENTIAL),
        ("Login Data", MaterialClass.BROWSER_CREDENTIAL),
        (".bash_history", MaterialClass.SHELL_HISTORY),
        (".local/share/fish/fish_history", MaterialClass.SHELL_HISTORY),
        (".config/fish/fish_history", MaterialClass.SHELL_HISTORY),
        ("refresh-token.cache", MaterialClass.TOKEN_CACHE),
        ("session.sqlite", MaterialClass.LIVE_DATABASE),
        ("session.sqlite-wal", MaterialClass.LIVE_DATABASE),
        ("session.sqlite-shm", MaterialClass.LIVE_DATABASE),
    ],
)
def test_structural_deny_matrix_overrides_safe_adapter_class(tmp_path, relative, expected):
    receipt = classify_material(_candidate(tmp_path, relative, observed_type="regular"), _declaration(tmp_path))

    assert receipt.decision is Decision.BLOCK
    assert receipt.material_class is expected
    assert receipt.reason_code is ReasonCode.DENIED_STRUCTURAL_CLASS


def test_adapter_cannot_self_approve_denied_class(tmp_path):
    declaration = _declaration(tmp_path, classes={MaterialClass.AUTH_STORE})
    receipt = classify_material(
        _candidate(tmp_path, "ordinary-name", material_class=MaterialClass.AUTH_STORE, observed_type="regular"),
        declaration,
    )

    assert receipt.decision is Decision.BLOCK
    assert receipt.reason_code is ReasonCode.DENIED_CLASS


def test_unknown_class_and_unknown_type_quarantine(tmp_path):
    unknown_class = classify_material(
        _candidate(tmp_path, "file.bin", material_class="future-secret", observed_type="regular"),
        _declaration(tmp_path),
    )
    unknown_type = classify_material(
        _candidate(tmp_path, "file.bin", observed_type="future-file-type"),
        _declaration(tmp_path),
    )

    assert (unknown_class.decision, unknown_class.reason_code) == (Decision.QUARANTINE, ReasonCode.UNKNOWN_CLASS)
    assert (unknown_type.decision, unknown_type.reason_code) == (Decision.QUARANTINE, ReasonCode.UNKNOWN_FILE_TYPE)


@pytest.mark.parametrize("source_kind", [["unhashable-source-kind"], "future.source"])
def test_unknown_source_kind_quarantines_without_raw_lookup_error(tmp_path, source_kind):
    candidate = _candidate(tmp_path, "safe.txt", source_kind=source_kind, observed_type="regular")

    receipt = classify_material(candidate, _declaration(tmp_path))

    assert (receipt.decision, receipt.reason_code) == (Decision.QUARANTINE, ReasonCode.UNKNOWN_SOURCE)


@pytest.mark.parametrize(
    "path_text",
    [
        "../outside.txt",
        "nested\\outside.txt",
        "C:\\outside.txt",
        "\\\\server\\share\\outside.txt",
        "safe.txt:secret-stream",
    ],
)
def test_platform_neutral_path_forms_cannot_bypass_root(tmp_path, path_text):
    candidate = MaterialCandidate(
        adapter="synthetic-adapter",
        source_kind="codex.transcript",
        material_class=MaterialClass.SESSION_TRANSCRIPT,
        path=path_text,
        approved_root=tmp_path,
        observed_type="regular",
    )

    receipt = classify_material(candidate, _declaration(tmp_path))

    assert receipt.decision is Decision.BLOCK
    assert receipt.reason_code in {ReasonCode.DENIED_PATH, ReasonCode.OUT_OF_ROOT}


def test_case_folded_root_alias_and_real_out_of_root_are_rejected(tmp_path):
    source = tmp_path / "safe.txt"
    source.write_text("synthetic")
    root_alias = str(tmp_path).swapcase()

    aliases = [
        MaterialCandidate("synthetic-adapter", "codex.transcript", MaterialClass.SESSION_TRANSCRIPT, root_alias + "/SAFE.TXT", tmp_path, "regular"),
        MaterialCandidate("synthetic-adapter", "codex.transcript", MaterialClass.SESSION_TRANSCRIPT, str(tmp_path.parent / "outside.txt"), tmp_path, "regular"),
    ]

    for candidate in aliases:
        receipt = classify_material(candidate, _declaration(tmp_path))
        assert receipt.decision is Decision.BLOCK
        assert receipt.reason_code in {ReasonCode.DENIED_PATH, ReasonCode.OUT_OF_ROOT}


def test_symlink_and_hardlink_are_rejected(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("synthetic")
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    hardlink = tmp_path / "hardlink.txt"
    os.link(source, hardlink)

    symlink_receipt = classify_material(_candidate(tmp_path, "link.txt"), _declaration(tmp_path))
    hardlink_receipt = classify_material(_candidate(tmp_path, "hardlink.txt"), _declaration(tmp_path))

    assert symlink_receipt.reason_code is ReasonCode.SYMLINK_OR_REPARSE
    assert hardlink_receipt.reason_code is ReasonCode.HARDLINK
    assert symlink_receipt.decision is hardlink_receipt.decision is Decision.BLOCK


def test_symlinked_ancestor_of_approved_root_is_rejected(tmp_path):
    real_parent = tmp_path / "real-parent"
    root = real_parent / "room"
    root.mkdir(parents=True)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    aliased_root = alias_parent / "room"
    (aliased_root / "safe.txt").write_text("synthetic")

    receipt = classify_material(_candidate(aliased_root, "safe.txt"), _declaration(aliased_root))

    assert receipt.decision is Decision.BLOCK
    assert receipt.reason_code is ReasonCode.SYMLINK_OR_REPARSE


def test_special_files_and_reparse_points_are_rejected(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    special = classify_material(_candidate(tmp_path, "pipe"), _declaration(tmp_path))
    reparse = classify_material(_candidate(tmp_path, "junction", observed_type="reparse-point"), _declaration(tmp_path))
    socket_receipt = classify_material(_candidate(tmp_path, "socket", observed_type="socket"), _declaration(tmp_path))

    assert special.reason_code is ReasonCode.SPECIAL_FILE
    assert reparse.reason_code is ReasonCode.SYMLINK_OR_REPARSE
    assert socket_receipt.reason_code is ReasonCode.SPECIAL_FILE
    assert all(receipt.decision is Decision.BLOCK for receipt in (special, reparse, socket_receipt))


def test_shell_history_requires_separate_approval(tmp_path):
    (tmp_path / ".zsh_history").write_text("synthetic history")
    candidate = _candidate(tmp_path, ".zsh_history", observed_type="regular")
    denied = classify_material(candidate, _declaration(tmp_path))
    approved = classify_material(
        _candidate(
            tmp_path,
            ".zsh_history",
            material_class=MaterialClass.SHELL_HISTORY,
            observed_type="regular",
            source_kind="sanitized.shell-history",
        ),
        AdapterDeclaration(
            adapter="synthetic-adapter",
            allowed_roots=(tmp_path,),
            allowed_classes=frozenset({MaterialClass.SHELL_HISTORY}),
            allow_shell_history=True,
        ),
    )

    assert denied.reason_code is ReasonCode.DENIED_STRUCTURAL_CLASS
    assert approved.reason_code is ReasonCode.ALLOWED
    assert denied.decision is Decision.BLOCK
    assert approved.decision is Decision.ALLOW


def test_shell_history_requires_distinct_sanitized_source_kind(tmp_path):
    (tmp_path / ".zsh_history").write_text("synthetic history")
    declaration = AdapterDeclaration(
        adapter="synthetic-adapter",
        allowed_roots=(tmp_path,),
        allowed_classes=frozenset({MaterialClass.SHELL_HISTORY}),
        allow_shell_history=True,
    )

    transcript_labeled = classify_material(
        _candidate(
            tmp_path,
            ".zsh_history",
            material_class=MaterialClass.SHELL_HISTORY,
            observed_type="regular",
        ),
        declaration,
    )
    sanitized_source = classify_material(
        _candidate(
            tmp_path,
            ".zsh_history",
            material_class=MaterialClass.SHELL_HISTORY,
            observed_type="regular",
            source_kind="sanitized.shell-history",
        ),
        declaration,
    )

    assert (transcript_labeled.decision, transcript_labeled.reason_code) == (
        Decision.BLOCK,
        ReasonCode.SOURCE_CLASS_MISMATCH,
    )
    assert sanitized_source.decision is Decision.ALLOW


def test_fish_history_requires_sanitized_source_without_blocking_nearby_safe_file(tmp_path):
    fish_history = tmp_path / ".local/share/fish/fish_history"
    fish_history.parent.mkdir(parents=True)
    fish_history.write_text("synthetic fish history")
    safe_file = fish_history.parent / "notes.txt"
    safe_file.write_text("synthetic fish notes")
    declaration = AdapterDeclaration(
        adapter="synthetic-adapter",
        allowed_roots=(tmp_path,),
        allowed_classes=frozenset({MaterialClass.SESSION_TRANSCRIPT, MaterialClass.SHELL_HISTORY}),
        allow_shell_history=True,
    )

    denied = classify_material(
        _candidate(tmp_path, ".local/share/fish/fish_history", observed_type="regular"),
        declaration,
    )
    approved = classify_material(
        _candidate(
            tmp_path,
            ".local/share/fish/fish_history",
            material_class=MaterialClass.SHELL_HISTORY,
            observed_type="regular",
            source_kind="sanitized.shell-history",
        ),
        declaration,
    )
    safe = classify_material(_candidate(tmp_path, ".local/share/fish/notes.txt"), declaration)

    assert (denied.decision, denied.reason_code) == (Decision.BLOCK, ReasonCode.DENIED_STRUCTURAL_CLASS)
    assert approved.decision is Decision.ALLOW
    assert safe.decision is Decision.ALLOW


def _split_at_every_boundary(value: bytes):
    for index in range(1, len(value)):
        yield value[:index], value[index:]


@pytest.mark.parametrize(
    "payload",
    [
        b"-----BEGIN " + b"SYNTHETIC PRIVATE KEY-----",
        b"Authorization: Bearer synthetic-bearer-token-123456",
        b'api_key="synthetic-api-key-1234567890"',
        b"aws_access_key_id=synthetic-aws-key-1234567890",
        b"postgresql://synthetic:synthetic-password@example.invalid/db",
        b"Cookie: session=synthetic-cookie-token-123456",
        b"AWS_SECRET_ACCESS_KEY=synthetic-aws-secret-1234567890",
        b"AWS_SESSION_TOKEN=synthetic-aws-session-1234567890",
        b"https://synthetic-user:synthetic-password@example.invalid/",
    ],
)
def test_streaming_content_checks_find_high_confidence_matches_across_chunks(payload):
    for chunks in _split_at_every_boundary(payload):
        result = scan_content(chunks, adapter="synthetic-adapter", material_class=MaterialClass.SESSION_TRANSCRIPT)
        assert result.decision is Decision.BLOCK
        assert result.reason_code is ReasonCode.CONTENT_MATCH
        assert result.matched_value is None


def test_safe_transcript_content_passes_unchanged_without_low_confidence_match():
    body = b"The transcript discusses API design and bearer authentication conceptually; no credential is present."

    result = scan_content([body], adapter="synthetic-adapter", material_class=MaterialClass.SESSION_TRANSCRIPT)

    assert result.decision is Decision.ALLOW
    assert result.reason_code is ReasonCode.ALLOWED
    assert result.bytes_scanned == len(body)
    assert result.digest


def test_content_bound_counts_memoryview_bytes_not_elements():
    words = array("H", [0x4141, 0x4242])

    result = scan_content(
        [memoryview(words)],
        adapter="synthetic-adapter",
        material_class=MaterialClass.SESSION_TRANSCRIPT,
        max_bytes=3,
    )

    assert (result.decision, result.reason_code) == (Decision.QUARANTINE, ReasonCode.CONTENT_LIMIT)
    assert result.bytes_scanned == 3


def test_ordinary_cookie_header_is_not_a_secret_match():
    result = scan_content(
        [b"Cookie: theme=dark; locale=en"],
        adapter="synthetic-adapter",
        material_class=MaterialClass.SESSION_TRANSCRIPT,
    )

    assert result.decision is Decision.ALLOW
    assert result.reason_code is ReasonCode.ALLOWED


def test_unknown_material_class_quarantines_content_before_scanning():
    result = scan_content(
        [b"ordinary synthetic transcript"],
        adapter="synthetic-adapter",
        material_class="future-material-class",
    )

    assert (result.decision, result.reason_code) == (Decision.QUARANTINE, ReasonCode.UNKNOWN_CLASS)


def test_content_bound_is_quarantine_and_does_not_read_unbounded_input():
    consumed = []

    def chunks():
        for chunk in (b"a" * 8, b"b" * 8, b"c" * 8):
            consumed.append(chunk)
            yield chunk

    result = scan_content(chunks(), adapter="synthetic-adapter", material_class=MaterialClass.SESSION_TRANSCRIPT, max_bytes=10)

    assert result.decision is Decision.QUARANTINE
    assert result.reason_code is ReasonCode.CONTENT_LIMIT
    assert result.bytes_scanned <= 10
    assert len(consumed) < 3
    assert result.digest is None


def test_iterator_exception_is_quarantined_without_raw_error_text():
    secret = "synthetic-iterator-secret"

    def chunks():
        yield b"safe prefix"
        raise RuntimeError(secret)

    result = scan_content(chunks(), adapter="synthetic-adapter", material_class=MaterialClass.SESSION_TRANSCRIPT)

    assert (result.decision, result.reason_code) == (Decision.QUARANTINE, ReasonCode.CONTENT_ERROR)
    assert secret not in repr(result)
    assert secret not in json.dumps(safe_diagnostic(result.receipt))


def test_malicious_transcript_metadata_is_inert(tmp_path):
    metadata = {
        "path": "../../outside.txt",
        "command": "upload https://synthetic-user:synthetic-password@example.invalid",
        "profile": "work",
        "destination": "personal-r2",
        "material_class": "auth-store",
    }
    source = tmp_path / "safe.txt"
    source.write_text("synthetic")

    receipt = classify_material(_candidate(tmp_path, "safe.txt", metadata=metadata), _declaration(tmp_path))

    assert receipt.decision is Decision.ALLOW
    assert "outside" not in json.dumps(receipt.to_dict())
    assert "personal-r2" not in json.dumps(receipt.to_dict())


def test_receipts_diagnostics_and_exceptions_never_leak_content_or_private_path(tmp_path):
    secret = b"synthetic-bearer-token-123456"
    result = scan_content([b"Authorization: Bearer " + secret], adapter="synthetic-adapter", material_class=MaterialClass.SESSION_TRANSCRIPT)
    receipt = result.receipt
    output = json.dumps(safe_diagnostic(receipt), sort_keys=True)
    assert secret.decode() not in output
    assert str(tmp_path) not in output
    assert "matched_value" not in output
    assert "exception" not in output
    assert repr(receipt).find(secret.decode()) == -1

    with pytest.raises(ValueError) as error:
        AdapterDeclaration(adapter="synthetic-adapter", allowed_roots=(tmp_path,), allowed_classes=frozenset({"not-an-enum"}))
    assert secret.decode() not in str(error.value)

    direct = DecisionReceipt("synthetic secret label", MaterialClass.SESSION_TRANSCRIPT, Decision.BLOCK, ReasonCode.CONTENT_MATCH)
    assert "synthetic secret label" not in repr(direct)


def test_receipt_write_is_private_atomic_and_contains_only_opaque_fields(tmp_path):
    receipt = scan_content(
        [b"Authorization: Bearer synthetic-bearer-token-123456"],
        adapter="synthetic-adapter",
        material_class=MaterialClass.SESSION_TRANSCRIPT,
    ).receipt
    destination = tmp_path / "receipt.json"

    write_receipt(destination, receipt)

    assert json.loads(destination.read_text()) == receipt.to_dict()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".receipt.json.*"))
    assert "synthetic-bearer-token-123456" not in destination.read_text()


def test_receipt_write_sanitizes_failure_and_temp_metadata(tmp_path, monkeypatch):
    secret_name = "synthetic-secret-token-123456.json"
    destination = tmp_path / secret_name
    receipt = DecisionReceipt("synthetic-adapter", MaterialClass.SESSION_TRANSCRIPT, Decision.BLOCK, ReasonCode.CONTENT_MATCH)
    temporary_paths = []

    def fail_replace(temporary, target):
        temporary_paths.append(str(temporary))
        raise OSError(f"synthetic raw path {target}")

    monkeypatch.setattr(security.os, "replace", fail_replace)

    with pytest.raises(ReceiptError) as error:
        security.write_receipt(destination, receipt)

    assert secret_name not in str(error.value)
    assert str(tmp_path) not in str(error.value)
    assert temporary_paths
    assert secret_name not in temporary_paths[0]
    assert not list(tmp_path.iterdir())
