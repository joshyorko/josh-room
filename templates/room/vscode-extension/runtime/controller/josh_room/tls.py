"""Application-owned system trust initialization."""

import os
import ssl
import sys
from pathlib import Path

_initialized = False


def initialize_system_trust() -> None:
    """Configure relocatable CA defaults once at the CLI boundary.

    RCC Environment Artifacts can be built in a temporary directory. Python's
    compiled OpenSSL defaults then point at that vanished directory after the
    artifact is imported into a different holotree. Point standard-library and
    Botocore defaults at the relocated bundle, while preserving explicit user
    overrides and the native ``ssl.SSLContext`` class. Botocore retains its
    own bundled CA path; this repairs Python libraries that use OpenSSL's
    default locations.
    """
    global _initialized
    if _initialized:
        return
    prefix = os.environ.get("CONDA_PREFIX") or sys.prefix
    bundle = Path(prefix) / "ssl" / "cert.pem"
    certs = Path(prefix) / "ssl" / "certs"
    if not os.environ.get("SSL_CERT_FILE") and bundle.is_file():
        os.environ["SSL_CERT_FILE"] = str(bundle)
    if not os.environ.get("SSL_CERT_DIR") and certs.is_dir():
        os.environ["SSL_CERT_DIR"] = str(certs)
    _initialized = True


def system_ssl_context() -> ssl.SSLContext:
    """Return a scoped native-trust context without changing global SSL state."""
    initialize_system_trust()
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
