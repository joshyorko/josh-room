import ssl


def test_system_trust_initialization_is_idempotent(monkeypatch):
    from josh_room import tls

    original = ssl.SSLContext
    monkeypatch.setattr(tls, "_initialized", False)

    tls.initialize_system_trust()
    tls.initialize_system_trust()

    assert tls._initialized is True
    assert ssl.SSLContext is original


def test_native_ssl_context_supports_botocore_after_initialization(monkeypatch):
    import boto3

    from josh_room import tls

    monkeypatch.setattr(tls, "_initialized", False)

    tls.initialize_system_trust()
    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:9000",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic-secret",
    )

    assert client.meta.endpoint_url == "http://127.0.0.1:9000"


def test_importing_tls_does_not_replace_ssl_context():
    original = ssl.SSLContext

    from josh_room import tls  # noqa: F401

    assert ssl.SSLContext is original
