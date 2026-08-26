import ssl


def test_system_trust_initialization_is_idempotent(monkeypatch):
    from josh_room import tls

    calls = []
    monkeypatch.setattr(tls.truststore, "inject_into_ssl", lambda: calls.append(True))
    monkeypatch.setattr(tls.sys, "version_info", (3, 13))
    monkeypatch.setattr(tls, "_initialized", False)

    tls.initialize_system_trust()
    tls.initialize_system_trust()

    assert calls == [True]


def test_python_314_keeps_native_ssl_context_for_botocore(monkeypatch):
    import boto3

    from josh_room import tls

    calls = []
    monkeypatch.setattr(tls.truststore, "inject_into_ssl", lambda: calls.append(True))
    monkeypatch.setattr(tls.sys, "version_info", (3, 14))
    monkeypatch.setattr(tls, "_initialized", False)

    tls.initialize_system_trust()
    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:9000",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic-secret",
    )

    assert calls == []
    assert client.meta.endpoint_url == "http://127.0.0.1:9000"


def test_importing_tls_does_not_replace_ssl_context():
    original = ssl.SSLContext

    from josh_room import tls  # noqa: F401

    assert ssl.SSLContext is original
