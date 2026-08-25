import ssl


def test_system_trust_initialization_is_idempotent(monkeypatch):
    from josh_room import tls

    calls = []
    monkeypatch.setattr(tls.truststore, "inject_into_ssl", lambda: calls.append(True))
    monkeypatch.setattr(tls, "_initialized", False)

    tls.initialize_system_trust()
    tls.initialize_system_trust()

    assert calls == [True]


def test_importing_tls_does_not_replace_ssl_context():
    original = ssl.SSLContext

    from josh_room import tls  # noqa: F401

    assert ssl.SSLContext is original
