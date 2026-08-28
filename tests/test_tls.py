import os
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


def test_system_trust_initialization_repairs_relocated_runtime_ca_paths(monkeypatch, tmp_path):
    from josh_room import tls

    prefix = tmp_path / "relocated-runtime"
    (prefix / "ssl" / "certs").mkdir(parents=True)
    (prefix / "ssl" / "cert.pem").write_text("synthetic-ca-bundle")
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(tls, "_initialized", False)

    tls.initialize_system_trust()

    assert os.environ["SSL_CERT_FILE"] == str(prefix / "ssl" / "cert.pem")
    assert os.environ["SSL_CERT_DIR"] == str(prefix / "ssl" / "certs")


def test_system_ssl_context_uses_scoped_robocorp_truststore(monkeypatch):
    import truststore

    from josh_room import tls

    monkeypatch.setattr(tls, "_initialized", False)
    context = tls.system_ssl_context()

    assert isinstance(context, truststore.SSLContext)
    assert ssl.SSLContext is not truststore.SSLContext


def test_importing_tls_does_not_replace_ssl_context():
    original = ssl.SSLContext

    from josh_room import tls  # noqa: F401

    assert ssl.SSLContext is original
