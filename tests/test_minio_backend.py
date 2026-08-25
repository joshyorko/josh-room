
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
    monkeypatch.setattr("josh_room.minio.lookup", lambda _: {"access-key-id": "id", "secret-access-key": "secret"})
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: captured.update(kwargs) or object())
    MinioBackend(MinioConfig("https://minio.invalid", "synthetic", "fixture", verify_tls=False, ca_bundle="/tmp/ca.pem", path_style=True))
    assert captured["verify"] == "/tmp/ca.pem"
    assert captured["config"].s3["addressing_style"] == "path"


def test_minio_integration_is_explicitly_gated(monkeypatch):
    monkeypatch.delenv("JOSH_ROOM_MINIO_LIVE", raising=False)
    import pytest
    if not __import__("os").environ.get("JOSH_ROOM_MINIO_LIVE"):
        pytest.skip("secret-gated MinIO acceptance")
