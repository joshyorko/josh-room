import hashlib
import os
import uuid

import pytest

from josh_room.cli import _doctor, _requires_oauth, build_parser
from josh_room.minio import MinioBackend, MinioConfig
from josh_room.object_store import ObjectStore


def test_minio_config_is_typed_and_defaults_private_safe():
    config = MinioConfig.from_private({"minio": {"endpoint": "https://minio.invalid", "bucket": "synthetic", "credential_profile": "fixture"}})
    assert config.path_style is True and config.verify_tls is True
    assert isinstance(MinioBackend, type)


def test_minio_backend_is_provider_neutral_store():
    assert issubclass(MinioBackend, ObjectStore)


def test_minio_client_uses_custom_ca_and_path_style(monkeypatch):
    captured = {}
    monkeypatch.setattr("josh_room.minio.lookup", lambda _profile, **_kwargs: {"access-key-id": "id", "secret-access-key": "secret"})
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: captured.update(kwargs) or object())
    MinioBackend(MinioConfig("https://minio.invalid", "synthetic", "fixture", verify_tls=False, ca_bundle="/tmp/ca.pem", path_style=True))
    assert captured["verify"] == "/tmp/ca.pem"
    assert captured["config"].s3["addressing_style"] == "path"


def test_minio_config_carries_persisted_disconnect_state():
    config = MinioConfig.from_private({
        "minio": {
            "endpoint": "https://minio.invalid",
            "bucket": "synthetic",
            "credential_profile": "fixture",
            "auth_state": "disconnected",
        }
    })

    assert config.auth_state == "disconnected"


def test_disconnected_minio_backend_fails_closed_before_using_credentials():
    class UnexpectedClient:
        def head_object(self, **_kwargs):
            raise AssertionError("disconnected backend should fail before using the client")

    backend = MinioBackend(
        MinioConfig(
            "https://minio.invalid",
            "synthetic",
            "fixture",
            auth_state="disconnected",
        ),
        client=UnexpectedClient(),
    )

    with pytest.raises(RuntimeError, match="disconnected"):
        backend.read_catalog()
    with pytest.raises(RuntimeError, match="disconnected"):
        backend.read_control("control/encryption-keyset.v1.json", 64)


def test_doctor_probes_selected_backend_and_oauth_is_r2_only(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("josh_room.cli._backend", lambda name, instance: calls.append(name) or (_ for _ in ()).throw(RuntimeError("offline")))
    report = _doctor(tmp_path, "minio", "terminal")
    assert calls == ["minio"]
    assert any(check["name"] == "minio" for check in report["checks"])
    minio = build_parser().parse_args(["projects", "list", "--backend", "minio"])
    r2 = build_parser().parse_args(["projects", "list", "--backend", "r2"])
    assert _requires_oauth(minio) is False
    assert _requires_oauth(r2) is True


def test_minio_integration_is_explicitly_gated(monkeypatch):
    required = ("JOSH_ROOM_MINIO_LIVE", "JOSH_ROOM_MINIO_ENDPOINT", "JOSH_ROOM_MINIO_BUCKET", "JOSH_ROOM_MINIO_PROFILE")
    if not all(os.environ.get(name) for name in required) or os.environ["JOSH_ROOM_MINIO_LIVE"] != "1":
        pytest.skip("secret-gated MinIO acceptance")
    config = MinioConfig(os.environ["JOSH_ROOM_MINIO_ENDPOINT"], os.environ["JOSH_ROOM_MINIO_BUCKET"], os.environ["JOSH_ROOM_MINIO_PROFILE"], multipart_threshold=1024)
    store = MinioBackend(config)
    body = ("synthetic-minio-" + uuid.uuid4().hex).encode()
    digest = hashlib.sha256(body).hexdigest()
    key = "objects/sha256/" + digest
    try:
        ref = store.put_bytes(key, body)
        assert ref.sha256 == digest and ref.size == len(body)
        assert store.get_bytes(key, expected_digest=digest, expected_size=len(body)) == body
        head = store.client.head_object(Bucket=config.bucket, Key=key)
        assert int(head["ContentLength"]) == len(body)
    finally:
        store.delete_object(key)
