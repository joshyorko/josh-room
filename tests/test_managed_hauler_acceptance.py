"""The release exception must never hide unrelated native/runtime failures."""
import pytest

from scripts.managed_hauler_acceptance import (
    WINDOWS_HAULER_SHA256,
    WINDOWS_JAT_ARTIFACT,
    known_windows_registry_failure,
    serve_result,
)


def affected_case():
    return {
        "platform": "nt",
        "env": {
            "JOSH_ROOM_JAT_RELEASE_TAG": "v0.1.22-jat-runtime",
            "JOSH_ROOM_JAT_SOURCE_SHA": "1d757a5ab75e82c4132e6356e382290c31af7ebd",
            "JOSH_ROOM_JAT_ARTIFACT": WINDOWS_JAT_ARTIFACT,
        },
        "exit_code": 2,
        "hauler_sha256": WINDOWS_HAULER_SHA256,
        "result": {
            "operation": "serve", "success": False, "exit_status": 1,
            "error": "jat serve failed with exit 1",
            "diagnostics": "Hauler endpoint on port 12345 exited with status 1: "
                "failed to copy config: unexpected status from POST request to "
                "http://127.0.0.1:12346/v2/hauler/joshs-all-the-things-workspace.tar.zst/blobs/uploads/: "
                "500 Internal Server Error unknown: unknown error",
        },
    }


def test_exact_official_windows_registry_failure_is_a_known_limitation():
    assert known_windows_registry_failure(**affected_case())


@pytest.mark.parametrize("reference", [
    "hauler/joshs-all-the-things-workspace.tar.zst",
    "josh-room-synthetic/hauler",
    "hauler/fixture.txt",
])
def test_each_fixture_upload_can_fail_first(reference):
    case = affected_case()
    case["result"]["diagnostics"] = case["result"]["diagnostics"].replace(
        "hauler/joshs-all-the-things-workspace.tar.zst", reference,
    )
    assert known_windows_registry_failure(**case)


def test_unrelated_registry_repository_still_fails():
    case = affected_case()
    case["result"]["diagnostics"] = case["result"]["diagnostics"].replace(
        "hauler/joshs-all-the-things-workspace.tar.zst", "other/image",
    )
    assert not known_windows_registry_failure(**case)


@pytest.mark.parametrize("field,value", [
    ("platform", "posix"), ("exit_code", None), ("exit_code", 1),
    ("hauler_sha256", "0" * 64),
])
def test_other_platform_binary_exit_or_timeout_still_fails(field, value):
    case = affected_case()
    case[field] = value
    assert not known_windows_registry_failure(**case)


@pytest.mark.parametrize("key", [
    "JOSH_ROOM_JAT_RELEASE_TAG", "JOSH_ROOM_JAT_SOURCE_SHA", "JOSH_ROOM_JAT_ARTIFACT",
])
def test_other_runtime_identity_still_fails(key):
    case = affected_case()
    case["env"][key] = "different"
    assert not known_windows_registry_failure(**case)


@pytest.mark.parametrize("field,value", [
    ("operation", "build"), ("success", True), ("exit_status", 0),
    ("error", "permission denied"), ("diagnostics", "permission denied"),
    ("diagnostics", "500 Internal Server Error unknown: unknown error"),
    ("diagnostics", None),
])
def test_unrelated_errors_and_incomplete_diagnostics_still_fail(field, value):
    case = affected_case()
    case["result"][field] = value
    assert not known_windows_registry_failure(**case)


def test_read_latest_structured_serve_result_and_ignore_unstructured_text(tmp_path):
    import json

    log = tmp_path / "serve.log"
    row = affected_case()["result"]
    log.write_text("warning\n" + json.dumps(row) + "\n", encoding="utf-8")
    assert serve_result(log) == row
    log.write_text("500 Internal Server Error\n", encoding="utf-8")
    assert serve_result(log) == {}
