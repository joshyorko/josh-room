"""Application-owned system trust initialization."""

_initialized = False


def initialize_system_trust() -> None:
    """Confirm native system trust once at the CLI application boundary.

    Globally injecting truststore replaces ``ssl.SSLContext`` and is
    incompatible with botocore's option setters on supported Python versions.
    Python already loads the operating system trust store for default contexts,
    so the CLI deliberately leaves the native SSL implementation intact.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True
