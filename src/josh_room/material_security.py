"""Deterministic, caller-owned material safety boundary for PCC evidence.

This module is deliberately narrow. It classifies logical material and host
filesystem observations; it does not discover sources, choose policy or
destinations, redact content, or perform transport/encryption.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath


class MaterialClass(StrEnum):
    SESSION_TRANSCRIPT = "session-transcript"
    SESSION_METADATA = "session-metadata"
    SESSION_ASSET = "session-asset"
    AUTH_STORE = "auth-store"
    KEYRING = "keyring"
    PRIVATE_KEY = "private-key"
    KUBECONFIG = "kubeconfig"
    ENV_SECRET = "environment-secret"
    CLOUD_CREDENTIAL = "cloud-credential"
    GIT_CREDENTIAL = "git-credential"
    BROWSER_CREDENTIAL = "browser-credential"
    SHELL_HISTORY = "shell-history"
    TOKEN_CACHE = "token-cache"
    LIVE_DATABASE = "live-database"
    SPECIAL_FILE = "special-file"
    OUT_OF_ROOT = "out-of-root"
    UNKNOWN = "unknown"


class Decision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    QUARANTINE = "quarantine"


class ReasonCode(StrEnum):
    ALLOWED = "allowed"
    DENIED_CLASS = "denied-class"
    DENIED_STRUCTURAL_CLASS = "denied-structural-class"
    DENIED_PATH = "denied-path"
    OUT_OF_ROOT = "out-of-root"
    APPROVED_ROOT = "approved-root"
    ADAPTER_MISMATCH = "adapter-mismatch"
    SYMLINK_OR_REPARSE = "symlink-or-reparse"
    HARDLINK = "hardlink"
    SPECIAL_FILE = "special-file"
    UNKNOWN_CLASS = "unknown-class"
    UNKNOWN_FILE_TYPE = "unknown-file-type"
    UNKNOWN_SOURCE = "unknown-source"
    SOURCE_CLASS_MISMATCH = "source-class-mismatch"
    CONTENT_MATCH = "content-match"
    CONTENT_LIMIT = "content-limit"
    CONTENT_ERROR = "content-error"


class ReceiptError(RuntimeError):
    """Raised with a public-safe message when an opaque receipt cannot be written."""


_SAFE_CLASSES = frozenset(
    {
        MaterialClass.SESSION_TRANSCRIPT,
        MaterialClass.SESSION_METADATA,
        MaterialClass.SESSION_ASSET,
    }
)
_DENIED_CLASSES = frozenset(MaterialClass) - _SAFE_CLASSES - {MaterialClass.UNKNOWN}
_SOURCE_CLASSES = {
    "codex.transcript": frozenset({MaterialClass.SESSION_TRANSCRIPT}),
    "codex.session-metadata": frozenset({MaterialClass.SESSION_METADATA}),
    "codex.session-asset": frozenset({MaterialClass.SESSION_ASSET}),
    "sanitized.shell-history": frozenset({MaterialClass.SHELL_HISTORY}),
}
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_COUNT = 1_000_000


def _safe_label(value: object, fallback: str) -> str:
    if isinstance(value, str) and _LABEL.fullmatch(value):
        return value
    return fallback


def _coerce_class(value: object) -> MaterialClass:
    if isinstance(value, MaterialClass):
        return value
    try:
        return MaterialClass(value)
    except (TypeError, ValueError):
        return MaterialClass.UNKNOWN


@dataclass(frozen=True)
class AdapterDeclaration:
    adapter: str
    allowed_roots: tuple[Path, ...]
    allowed_classes: frozenset[MaterialClass]
    allow_shell_history: bool = False

    def __post_init__(self) -> None:
        if not _LABEL.fullmatch(self.adapter):
            raise ValueError("adapter declaration is invalid")
        if not self.allowed_roots or any(not isinstance(root, Path) for root in self.allowed_roots):
            raise ValueError("adapter roots are invalid")
        normalized = frozenset(_coerce_class(value) for value in self.allowed_classes)
        if MaterialClass.UNKNOWN in normalized or normalized != set(self.allowed_classes):
            raise ValueError("adapter class declaration is invalid")
        if type(self.allow_shell_history) is not bool:
            raise TypeError("shell history approval is invalid")
        object.__setattr__(self, "allowed_classes", normalized)


@dataclass(frozen=True)
class MaterialCandidate:
    adapter: str
    source_kind: str
    material_class: MaterialClass | str
    path: Path | str
    approved_root: Path | str
    observed_type: str | None = None
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True)
class DecisionReceipt:
    adapter: str
    material_class: MaterialClass
    decision: Decision
    reason_code: ReasonCode
    count: int = 1
    digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter", _safe_label(self.adapter, "untrusted-adapter"))
        try:
            material_class = MaterialClass(self.material_class)
        except Exception:  # noqa: BLE001 - receipt construction must stay public-safe
            material_class = MaterialClass.UNKNOWN
        try:
            decision = Decision(self.decision)
        except Exception:  # noqa: BLE001 - receipt construction must stay public-safe
            decision = Decision.QUARANTINE
        try:
            reason_code = ReasonCode(self.reason_code)
        except Exception:  # noqa: BLE001 - receipt construction must stay public-safe
            reason_code = ReasonCode.CONTENT_ERROR
        if material_class is MaterialClass.UNKNOWN:
            decision = Decision.QUARANTINE if decision is Decision.ALLOW else decision
            reason_code = ReasonCode.UNKNOWN_CLASS
        object.__setattr__(self, "material_class", material_class)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "reason_code", reason_code)
        if type(self.count) is not int or not 0 <= self.count <= _MAX_RECEIPT_COUNT:
            raise ValueError("receipt count is invalid")
        if self.digest is not None and not _HASH.fullmatch(self.digest):
            raise ValueError("receipt digest is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter": _safe_label(self.adapter, "untrusted-adapter"),
            "material_class": self.material_class.value,
            "decision": self.decision.value,
            "reason_code": self.reason_code.value,
            "count": self.count,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ContentScanResult:
    receipt: DecisionReceipt
    bytes_scanned: int
    matched_value: None = None

    @property
    def decision(self) -> Decision:
        return self.receipt.decision

    @property
    def reason_code(self) -> ReasonCode:
        return self.receipt.reason_code

    @property
    def digest(self) -> str | None:
        return self.receipt.digest


def _receipt(
    candidate: MaterialCandidate,
    material_class: MaterialClass,
    decision: Decision,
    reason_code: ReasonCode,
    *,
    digest: str | None = None,
) -> DecisionReceipt:
    return DecisionReceipt(
        adapter=_safe_label(candidate.adapter, "untrusted-adapter"),
        material_class=material_class,
        decision=decision,
        reason_code=reason_code,
        digest=digest,
    )


def _raw_path_safe(text: str) -> bool:
    if not text or "\x00" in text or "\\" in text or text.startswith("//"):
        return False
    windows = PureWindowsPath(text)
    posix = PurePosixPath(text)
    if windows.drive or (windows.anchor and not posix.is_absolute()):
        return False
    parts = tuple(part for part in posix.parts if part not in {"/", ""})
    return bool(parts) and not any(part in {".", ".."} or ":" in part for part in parts)


def _exact_absolute(path: Path, root: Path, safe: bool) -> Path | None:
    if not safe:
        return None
    if path.is_absolute():
        candidate = path.absolute()
    else:
        candidate = (root / path).absolute()
    root_absolute = root.absolute()
    try:
        candidate.relative_to(root_absolute)
    except ValueError:
        return None
    return candidate


def _canonical_contained(root: Path, candidate: Path) -> bool:
    try:
        root_real = root.resolve(strict=False)
        candidate_real = candidate.resolve(strict=False)
        candidate_real.relative_to(root_real)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _contains_components(parts: tuple[str, ...], components: tuple[str, ...]) -> bool:
    width = len(components)
    return any(parts[index : index + width] == components for index in range(len(parts) - width + 1))


def _path_class(path: Path) -> MaterialClass | None:
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    joined = "/".join(parts)
    if any(part in {"proc", "sys", "procfs", "sysfs"} for part in parts):
        return MaterialClass.SPECIAL_FILE
    if ".cloudflared" in parts or "cloudflared" in parts or ("containers" in parts and name == "auth.json"):
        return MaterialClass.CLOUD_CREDENTIAL
    if ".ssh" in parts or _contains_components(parts, (".gnupg", "private-keys-v1.d")):
        return MaterialClass.PRIVATE_KEY
    if _contains_components(parts, ("helm", "registry")):
        return MaterialClass.CLOUD_CREDENTIAL
    if name == "auth.json" or "/auth/" in f"/{joined}/" or name in {"session.json", "credentials.json"}:
        return MaterialClass.AUTH_STORE
    if (
        "keyring" in parts
        or "secret-service" in parts
        or "gnome-keyring" in parts
        or name.endswith(".keyring")
        or name in {"login.keychain", "login.keychain-db", "credential.db", "secret-service.sock"}
    ):
        return MaterialClass.KEYRING
    if name in {"id_rsa", "id_ed25519", "id_ecdsa", "identity.age", "signing.key"} or name.endswith((".agekey", ".pem", ".p12")):
        return MaterialClass.PRIVATE_KEY
    if name == "config" and any(part in {".kube", "kube", "kubeconfigs"} for part in parts):
        return MaterialClass.KUBECONFIG
    if name == ".env" or name.startswith(".env.") or name in {"dotenv", "environment.dump"}:
        return MaterialClass.ENV_SECRET
    if (
        ".aws" in parts
        or ".azure" in parts
        or "gcloud" in parts
        or name in {".netrc", "_netrc", ".npmrc", ".pypirc"}
        or (".docker" in parts and name == "config.json")
        or (".terraform.d" in parts and name == "credentials.tfrc.json")
        or ("gh" in parts and name == "hosts.yml")
        or ("rclone" in parts and name == "rclone.conf")
        or ("containers" in parts and name == "auth.json")
    ):
        return MaterialClass.CLOUD_CREDENTIAL
    if (
        name in {".git-credentials", ".gitconfig", ".gitcredential"}
        or name.startswith("git-credential")
        or ("git" in parts and name == "credentials")
        or ".git" in parts
    ):
        return MaterialClass.GIT_CREDENTIAL
    if (
        name.startswith("cookies")
        or name in {"login data", "logins.json", "key4.db", "web data"}
        or any(part in {"local storage", "session storage"} for part in parts)
    ):
        return MaterialClass.BROWSER_CREDENTIAL
    if name in {".bash_history", ".zsh_history", ".fish_history", "consolehost_history.txt", "powershell_history.txt"} or _contains_components(
        parts, ("fish", "fish_history")
    ):
        return MaterialClass.SHELL_HISTORY
    if any(token in name for token in ("refresh-token", "refresh_token", "device-code", "device_code", "token-cache", "token_cache")) or name in {
        "token.json",
        "tokens.json",
    }:
        return MaterialClass.TOKEN_CACHE
    if name.endswith((".sqlite", ".sqlite3", ".db", "-wal", "-shm")):
        return MaterialClass.LIVE_DATABASE
    return None


def _actual_type(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return "unknown"
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & 0x400:
        return "reparse-point"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISREG(info.st_mode):
        return "regular"
    if stat.S_ISSOCK(info.st_mode):
        return "socket"
    if stat.S_ISFIFO(info.st_mode):
        return "fifo"
    if stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode):
        return "device"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    return "unknown"


def _has_unsafe_component(root: Path, candidate: Path) -> ReasonCode | None:
    try:
        root_absolute = root.absolute()
        candidate_absolute = candidate.absolute()
        candidate_absolute.relative_to(root_absolute)
    except ValueError:
        return ReasonCode.OUT_OF_ROOT
    for boundary in (root_absolute, candidate_absolute):
        current = Path(boundary.anchor) if boundary.anchor else Path()
        for part in boundary.parts:
            if part == boundary.anchor:
                continue
            current /= part
            if _actual_type(current) in {"symlink", "reparse-point"}:
                return ReasonCode.SYMLINK_OR_REPARSE
    return None


def _root_declared(root: Path, declaration: AdapterDeclaration) -> bool:
    root_absolute = root.absolute()
    return any(root_absolute == declared.absolute() for declared in declaration.allowed_roots)


def classify_material(candidate: MaterialCandidate, declaration: AdapterDeclaration) -> DecisionReceipt:
    """Classify one host-side candidate without interpreting its metadata."""
    material_class = _coerce_class(candidate.material_class)
    if candidate.adapter != declaration.adapter:
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.ADAPTER_MISMATCH)
    if material_class is MaterialClass.UNKNOWN:
        return _receipt(candidate, material_class, Decision.QUARANTINE, ReasonCode.UNKNOWN_CLASS)
    if material_class in _DENIED_CLASSES and not (
        material_class is MaterialClass.SHELL_HISTORY and declaration.allow_shell_history
    ):
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.DENIED_CLASS)
    if type(candidate.source_kind) is not str:
        return _receipt(candidate, material_class, Decision.QUARANTINE, ReasonCode.UNKNOWN_SOURCE)
    expected_classes = _SOURCE_CLASSES.get(candidate.source_kind)
    if expected_classes is None:
        return _receipt(candidate, material_class, Decision.QUARANTINE, ReasonCode.UNKNOWN_SOURCE)
    if material_class not in expected_classes:
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.SOURCE_CLASS_MISMATCH)
    if material_class not in declaration.allowed_classes:
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.DENIED_CLASS)

    try:
        root = Path(candidate.approved_root)
        raw_path = Path(candidate.path)
        raw_text = os.fspath(candidate.path) if isinstance(candidate.path, (str, os.PathLike)) else ""
        path = _exact_absolute(raw_path, root, _raw_path_safe(raw_text))
    except Exception:  # noqa: BLE001 - untrusted path metadata must not escape
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.DENIED_PATH)
    if not _root_declared(root, declaration) or not root.is_dir() or root.is_symlink():
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.APPROVED_ROOT)
    if path is None:
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.DENIED_PATH)
    if not _canonical_contained(root, path):
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.OUT_OF_ROOT)
    unsafe_component = _has_unsafe_component(root.absolute(), path)
    if unsafe_component is not None:
        return _receipt(candidate, material_class, Decision.BLOCK, unsafe_component)

    inferred = _path_class(path)
    if inferred is MaterialClass.SHELL_HISTORY and declaration.allow_shell_history and material_class is MaterialClass.SHELL_HISTORY:
        inferred = None
    if inferred is not None:
        return _receipt(candidate, inferred, Decision.BLOCK, ReasonCode.DENIED_STRUCTURAL_CLASS)

    actual = _actual_type(path)
    if actual is None and candidate.observed_type in {None, "regular"}:
        return _receipt(candidate, material_class, Decision.QUARANTINE, ReasonCode.UNKNOWN_FILE_TYPE)
    observed = actual or candidate.observed_type
    if observed is None or observed == "unknown" or observed not in {
        "regular",
        "directory",
        "symlink",
        "reparse-point",
        "socket",
        "fifo",
        "device",
    }:
        return _receipt(candidate, material_class, Decision.QUARANTINE, ReasonCode.UNKNOWN_FILE_TYPE)
    if observed in {"symlink", "reparse-point"}:
        return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.SYMLINK_OR_REPARSE)
    if observed != "regular":
        return _receipt(candidate, MaterialClass.SPECIAL_FILE, Decision.BLOCK, ReasonCode.SPECIAL_FILE)
    try:
        if path.exists() and path.stat().st_nlink > 1:
            return _receipt(candidate, material_class, Decision.BLOCK, ReasonCode.HARDLINK)
    except OSError:
        return _receipt(candidate, material_class, Decision.QUARANTINE, ReasonCode.UNKNOWN_FILE_TYPE)
    return _receipt(candidate, material_class, Decision.ALLOW, ReasonCode.ALLOWED)


_CONTENT_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 _-]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(rb"\b(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token|aws[_ -]?access[_ -]?key[_ -]?id)\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(rb"\bAWS_(?:SECRET_ACCESS_KEY|SESSION_TOKEN)\b\s*=\s*[\"']?[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(rb"\b(?:postgres(?:ql)?|mysql|redis)://[^\s/@:]+:[^\s/@]+@[^\s]+", re.IGNORECASE),
    re.compile(
        rb"\b(?:Cookie|Set-Cookie):\s*(?:__Host-|__Secure-|(?:session|sid|auth|token|refresh|csrf|jwt|login|remember|connect\.sid)[^=\s;]*)=[A-Za-z0-9._~+/=-]{16,}",
        re.IGNORECASE,
    ),
    re.compile(rb"https?://[^\s/@:]+:[^\s/@]+@[^\s]+", re.IGNORECASE),
)
_CONTENT_OVERLAP = max(len(pattern.pattern) for pattern in _CONTENT_PATTERNS)
MAX_CONTENT_SCAN_BYTES = 4 * 1024 * 1024


def scan_content(
    chunks: Iterable[bytes],
    *,
    adapter: str,
    material_class: MaterialClass | str,
    max_bytes: int = MAX_CONTENT_SCAN_BYTES,
) -> ContentScanResult:
    """Scan bounded byte chunks without returning or retaining matched values."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("content bound is invalid")
    selected_class = _coerce_class(material_class)
    if selected_class is MaterialClass.UNKNOWN:
        return ContentScanResult(
            DecisionReceipt(_safe_label(adapter, "untrusted-adapter"), selected_class, Decision.QUARANTINE, ReasonCode.UNKNOWN_CLASS),
            0,
        )
    digest = hashlib.sha256()
    overlap = b""
    scanned = 0
    try:
        for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError("content chunks must be bytes")
            if isinstance(chunk, memoryview):
                chunk = chunk.cast("B")
            remaining = max_bytes - scanned
            if remaining <= 0:
                return ContentScanResult(
                    DecisionReceipt(_safe_label(adapter, "untrusted-adapter"), selected_class, Decision.QUARANTINE, ReasonCode.CONTENT_LIMIT),
                    scanned,
                )
            current = bytes(chunk[:remaining])
            scanned += len(current)
            digest.update(current)
            window = overlap + current
            if any(pattern.search(window) for pattern in _CONTENT_PATTERNS):
                return ContentScanResult(
                    DecisionReceipt(_safe_label(adapter, "untrusted-adapter"), selected_class, Decision.BLOCK, ReasonCode.CONTENT_MATCH),
                    scanned,
                )
            overlap = window[-_CONTENT_OVERLAP:]
            if len(current) < len(chunk):
                return ContentScanResult(
                    DecisionReceipt(_safe_label(adapter, "untrusted-adapter"), selected_class, Decision.QUARANTINE, ReasonCode.CONTENT_LIMIT),
                    scanned,
                )
    except Exception:  # noqa: BLE001 - iterator/scan details are never public output
        return ContentScanResult(
            DecisionReceipt(_safe_label(adapter, "untrusted-adapter"), selected_class, Decision.QUARANTINE, ReasonCode.CONTENT_ERROR),
            scanned,
        )
    return ContentScanResult(
        DecisionReceipt(_safe_label(adapter, "untrusted-adapter"), selected_class, Decision.ALLOW, ReasonCode.ALLOWED, digest=digest.hexdigest()),
        scanned,
    )


def safe_diagnostic(receipt: DecisionReceipt) -> dict[str, object]:
    """Return the only diagnostic representation callers may log or persist."""
    return receipt.to_dict()


def write_receipt(path: Path, receipt: DecisionReceipt) -> None:
    """Atomically write a private opaque receipt without exposing input data."""
    fd: int | None = None
    temporary: Path | None = None
    try:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(safe_diagnostic(receipt), sort_keys=True, separators=(",", ":")).encode() + b"\n"
        fd, temporary_name = tempfile.mkstemp(prefix=".josh-room-receipt-", dir=destination.parent)
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        fd = None
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    except Exception:  # noqa: BLE001 - receipt failures must not expose path or value metadata
        raise ReceiptError("receipt write failed") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
