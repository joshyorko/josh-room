import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAFETY_TEST = Path(__file__).resolve().relative_to(ROOT).as_posix()


def _private_endpoint_patterns() -> dict[str, re.Pattern[str]]:
    octet = r"(?:[0-9]{1,3})"
    dot = re.escape(".")
    private_ip = (
        rf"(?<![0-9.])(?:10|192){dot}{octet}{dot}{octet}{dot}{octet}"
        rf"|(?<![0-9.])172{dot}(?:1[6-9]|2[0-9]|3[0-1]){dot}{octet}{dot}{octet}"
    )
    return {
        "LAN/private IP endpoint": re.compile(private_ip),
        "LAN/private hostname endpoint": re.compile(
            r"(?i)https?://[^\s/\"'<>]+(?:\.home\.arpa|\.lan|\.local)(?:[/\s\"'<>]|$)"
        ),
        "personal deployment endpoint": re.compile(
            r"(?i)https?://(?!(?:josh-room-auth\.joshua-yorko\.workers\.dev)(?:[/\s\"'<>]|$))(?!\$\{)[^\s/\"'<>]+(?:\.workers\.dev|\.r2\.cloudflarestorage\.com)(?:[/\s\"'<>]|$)"
        ),
    }


def _credential_patterns() -> dict[str, re.Pattern[str]]:
    assignment_value = r"[^\s,}\"']{8,}"
    return {
        "Kamal secret material path": re.compile(
            r"(?i)(?:\.env\.kamal\.local|\.kamal/(?:secret|secrets|env)(?:[/\s]|$))"
        ),
        "root credential value": re.compile(
            rf"(?i)(?:\broot@[^\s/]+|\broot_(?:password|secret|token|key)\s*[:=]\s*{assignment_value})"
        ),
        "Kamal credential value": re.compile(
            rf"(?i)\bKAMAL_[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|KEY)\s*[:=]\s*{assignment_value}"
        ),
        "actual key-shaped value": re.compile(
            r"(?i)(?:-----BEGIN [A-Z ]+PRIVATE KEY-----|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bAGE-SECRET-KEY-1[0-9a-z]{20,}\b)"
        ),
        "personal deployment config identifier": re.compile(
            r"(?i)\"(?:OAUTH_CLIENT_ID|id)\"\s*:\s*\"[0-9a-f]{32}\""
        ),
        "credential-shaped value": re.compile(
            r"(?i)\b(?:secret[-_ ]?access[-_ ]?key|api[-_ ]?key|private[-_ ]?key)\s*[:=]\s*[\"'](?!synthetic|temporary|example)[A-Za-z0-9+/=_-]{24,}[\"']"
        ),
    }


def _personal_bucket_patterns() -> dict[str, re.Pattern[str]]:
    bucket_value = r"[^\s,}\"']+"
    production_context = r"(?=[^\s,}\"']*(?:prod|production))"
    personal_context = r"(?=[^\s,}\"']*(?:josh|personal|private))"
    return {
        "personal/production bucket artifact": re.compile(
            rf"(?i)(?:--bucket\s+|\b(?:bucket|bucket[-_]name)\s*[:=]\s*[\"']?){production_context}{personal_context}{bucket_value}"
        ),
    }


def _tracked_text() -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    files = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode()
        if relative == SAFETY_TEST:
            continue
        path = ROOT / relative
        if not path.is_file():
            continue
        content = path.read_bytes()
        if b"\0" in content:
            continue
        files.append((relative, content.decode(errors="replace")))
    return files


def test_public_artifacts_exclude_dogfood_storage_and_credentials():
    patterns = {
        **_private_endpoint_patterns(),
        **_credential_patterns(),
        **_personal_bucket_patterns(),
    }
    violations = []
    for relative, content in _tracked_text():
        for line_number, line in enumerate(content.splitlines(), 1):
            for label, pattern in patterns.items():
                if pattern.search(line):
                    violations.append(f"{relative}:{line_number}: {label}")

    synthetic_public_fixture = "Josh Room uses https://example.invalid and the josh-room product identifier."
    assert not any(pattern.search(synthetic_public_fixture) for pattern in patterns.values())
    assert not violations, (
        "Public-artifact safety violation(s): tracked public source, templates, "
        "fixtures, docs, or tests contain dogfood-only infrastructure. "
        "Use synthetic example.invalid fixtures and host-provided credentials instead.\n"
        + "\n".join(violations)
    )


def test_public_safety_allows_only_the_official_auth_authority():
    endpoint_pattern = _private_endpoint_patterns()["personal deployment endpoint"]
    assert not endpoint_pattern.search("https://josh-room-auth.joshua-yorko.workers.dev/session/start")
    assert endpoint_pattern.search("http://someone-else.workers.dev/session/start")
