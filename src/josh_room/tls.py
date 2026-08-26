"""Application-owned system trust initialization."""

import sys

import truststore

_initialized = False


def initialize_system_trust() -> None:
    """Install system trust once, at the CLI application boundary."""
    global _initialized
    if _initialized:
        return
    # truststore 0.9.1's SSLContext injection recurses through Python 3.14's
    # property setters when botocore creates its urllib3 pool. Python 3.14's
    # default context already uses the container's system trust, so leave it
    # intact until truststore publishes a compatible injector.
    if sys.version_info < (3, 14):
        truststore.inject_into_ssl()
    _initialized = True
