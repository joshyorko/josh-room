"""Application-owned system trust initialization."""

import truststore

_initialized = False


def initialize_system_trust() -> None:
    """Install system trust once, at the CLI application boundary."""
    global _initialized
    if _initialized:
        return
    truststore.inject_into_ssl()
    _initialized = True
