from josh_room.config import DimensionConfig
from josh_room.room_store_bridge import (
    _create_backend,
    _provider_environment,
    _repository_locator,
)
from josh_room.s3_backend import S3Backend, S3Config


def dimension(**overrides):
    values = {
        "dimension_id": "aws",
        "display_name": "AWS S3",
        "provider": "s3",
        "endpoint": "https://s3.us-west-2.amazonaws.com",
        "bucket": "synthetic-room-store",
        "credential_profile": "synthetic-s3",
        "region": "us-west-2",
        "encryption_domain_id": "12345678-1234-4234-8234-123456789abc",
    }
    values.update(overrides)
    return DimensionConfig(**values)


def test_s3_dimension_builds_provider_specific_reusable_backend(monkeypatch):
    selected = dimension()
    config = S3Config.from_dimension(selected)
    from josh_room import minio

    monkeypatch.setattr(
        minio,
        "lookup",
        lambda *args, **kwargs: {
            "access-key-id": "synthetic",
            "secret-access-key": "synthetic",
        },
    )
    monkeypatch.setattr(S3Backend, "_client_from_keyring", lambda self: object())
    backend = _create_backend(selected, None)

    assert config.endpoint == "https://s3.us-west-2.amazonaws.com"
    assert config.region == "us-west-2"
    assert type(backend) is S3Backend
    assert backend.config.dimension_id == "aws"


def test_s3_restic_locator_keeps_s3_identity():
    selected = dimension()
    assert _repository_locator(selected).startswith("s3:https://s3.us-west-2.amazonaws.com/")


def test_s3_requires_https_even_for_loopback():
    import pytest

    with pytest.raises(ValueError, match="HTTPS"):
        dimension(endpoint="http://localhost:9000")


def test_s3_restic_environment_uses_private_session_credentials(monkeypatch):
    from josh_room import room_store_bridge

    monkeypatch.setattr(
        room_store_bridge.keyring,
        "lookup",
        lambda profile, allow_runtime: {
            "access-key-id": "synthetic-access",
            "secret-access-key": "synthetic-secret",
            "session-token": "synthetic-session",
        },
    )
    env = _provider_environment(dimension())
    assert env == {
        "AWS_ACCESS_KEY_ID": "synthetic-access",
        "AWS_SECRET_ACCESS_KEY": "synthetic-secret",
        "AWS_SESSION_TOKEN": "synthetic-session",
        "AWS_DEFAULT_REGION": "us-west-2",
    }


def test_private_s3_configuration_requires_explicit_region():
    import pytest

    from josh_room.config import ConnectionConfig

    with pytest.raises(ValueError, match="region"):
        ConnectionConfig.from_private(
            "aws",
            {
                "provider": "s3",
                "credential_profile": "synthetic-s3",
            },
        )


def test_s3_restic_uses_explicit_lookup_style(monkeypatch, tmp_path):
    from josh_room.restic_store import ResticStore

    observed = {}

    class Process:
        stdout = None

    def factory(argv, **_kwargs):
        observed["argv"] = argv
        return Process()

    store = ResticStore(
        repository="s3:https://s3.us-west-2.amazonaws.com/synthetic-room-store/prefix",
        cache_dir=tmp_path,
        password_file=tmp_path / "password",
        executable="restic",
        process_factory=factory,
        s3_bucket_lookup="path",
    )
    store._spawn(["snapshots"])
    assert observed["argv"][:3] == ["restic", "-o", "s3.bucket-lookup=path"]


def test_s3_access_errors_are_sanitized():
    import pytest
    from botocore.exceptions import ClientError

    from josh_room.s3_backend import S3ProviderError, check_bucket_access

    class Client:
        def head_bucket(self, **_kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "AuthorizationHeaderMalformed",
                        "Message": "SECRET synthetic secret details",
                    },
                    "ResponseMetadata": {"HTTPStatusCode": 400},
                },
                "HeadBucket",
            )

    with pytest.raises(S3ProviderError, match="region-or-endpoint-mismatch") as caught:
        check_bucket_access(
            type(
                "Connection",
                (),
                {"provider": "s3", "connection_id": "synthetic", "region": "us-west-2"},
            )(),
            "synthetic-room-store",
            Client(),
        )
    assert "SECRET" not in str(caught.value)
    assert caught.value.result["provider"] == "s3"


def test_s3_orphan_receipt_keeps_s3_provider_identity(tmp_path):
    import json

    from josh_room.local_store import ObjectRef

    backend = S3Backend.__new__(S3Backend)
    backend.config = S3Config.from_dimension(dimension())
    backend.receipt_dir = tmp_path
    receipt = backend.record_orphan(
        ObjectRef("objects/sha256/" + "a" * 64, "a" * 64, 1)
    )
    assert json.loads(receipt.read_text())["destination"]["provider"] == "s3"


def test_native_cli_creates_private_s3_connection_with_region_and_session_token(
    tmp_path, monkeypatch, capsys
):
    import io
    import json
    import sys

    from josh_room import cli
    from josh_room.config import connection_configs

    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    stored = []
    monkeypatch.setattr(
        cli, "store_keyring", lambda profile, values: stored.append((profile, values))
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "access-key-id": "synthetic-access",
                    "secret-access-key": "synthetic-secret",
                    "session-token": "synthetic-session",
                }
            )
        ),
    )
    assert (
        cli.main(
            [
                "provider",
                "connection",
                "create",
                "--provider",
                "s3",
                "--region",
                "us-west-2",
                "--connection",
                "synthetic-aws",
                "--json",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)["connection"]
    assert created["provider"] == "s3"
    assert created["endpoint"] == "https://s3.us-west-2.amazonaws.com"
    assert created["region"] == "us-west-2"
    assert stored[0][1]["session-token"] == "synthetic-session"
    parsed = connection_configs(
        {
            "connections": {
                created["id"]: {
                    key: value for key, value in created.items() if key != "id"
                }
            }
        }
    )[created["id"]]
    assert parsed.provider == "s3"
    assert parsed.region == "us-west-2"


def test_s3_connection_rejects_remote_http_endpoint():
    import pytest

    from josh_room.config import ConnectionConfig

    with pytest.raises(ValueError, match="HTTPS"):
        ConnectionConfig.from_private(
            "synthetic-aws",
            {
                "provider": "s3",
                "endpoint": "http://s3.example.invalid",
                "credential_profile": "synthetic",
                "region": "us-west-2",
            },
        )


def test_s3_backend_configuration_cannot_disable_tls_or_embed_endpoint_queries():
    import pytest

    selected = dimension()
    with pytest.raises(ValueError, match="TLS"):
        S3Config.from_dimension(selected).__class__(
            endpoint=selected.endpoint,
            bucket=selected.bucket,
            credential_profile=selected.credential_profile,
            region=selected.region,
            verify_tls=False,
        )
    with pytest.raises(ValueError, match="HTTPS"):
        S3Config(
            endpoint="https://s3.example.invalid/?signature=synthetic",
            bucket="synthetic-room-store",
            credential_profile="synthetic-s3",
            region="us-west-2",
        )


def test_default_s3_endpoint_uses_aws_partition_and_region():
    import pytest

    from josh_room.config import default_s3_endpoint

    assert default_s3_endpoint("eu-west-1") == "https://s3.eu-west-1.amazonaws.com"
    assert default_s3_endpoint("us-gov-west-1") == "https://s3.us-gov-west-1.amazonaws.com"
    assert default_s3_endpoint("cn-north-1") == "https://s3.cn-north-1.amazonaws.com.cn"
    with pytest.raises(ValueError, match="not supported"):
        default_s3_endpoint("us-iso-east-1")
    with pytest.raises(ValueError, match="safe region"):
        default_s3_endpoint("us-west-2?secret=synthetic")


def test_custom_s3_endpoint_does_not_require_a_known_aws_region():
    from josh_room.config import ConnectionConfig

    connection = ConnectionConfig.from_private('synthetic-custom', {
        'provider': 's3', 'endpoint': 'https://objects.example.invalid',
        'region': 'custom-1', 'credential_profile': 'synthetic-profile',
    })
    assert connection.region == 'custom-1'
    assert connection.endpoint == 'https://objects.example.invalid'
