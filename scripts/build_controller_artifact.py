#!/usr/bin/env python3
"""Build and verify the Josh Room controller RCC Environment Artifact."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

EXPECTED_RCC = "v18.19.3"
SHA256_LENGTH = 64


def load_manifest(path: Path) -> dict:
    value = json.loads(Path(path).read_text())
    if value.get("schema_version") != 1:
        raise ValueError("controller artifact manifest schema must be 1")
    if value.get("rcc", {}).get("version") != EXPECTED_RCC:
        raise ValueError(f"controller artifact lane requires RCC {EXPECTED_RCC}")
    controller = value.get("controller", {})
    if not controller.get("artifact_asset") and not controller.get("artifact_assets"):
        raise ValueError("controller artifact manifest is missing artifact asset naming")
    return value


def resolve_rcc_pin(manifest: dict, platform: str, checksum: str | None = None) -> dict:
    if manifest.get("rcc", {}).get("version") != EXPECTED_RCC:
        raise ValueError(f"controller artifact lane requires RCC {EXPECTED_RCC}")
    pin = manifest.get("rcc", {}).get("platforms", {}).get(platform)
    if not isinstance(pin, dict):
        raise TypeError(f"no RCC pin for {platform}")
    value = checksum or pin.get("sha256")
    if not isinstance(value, str) or len(value) != SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value.lower()):
        raise ValueError(f"RCC {platform} checksum is pending; supply the real v18.19.3 release SHA256")
    if not str(pin.get("url", "")).startswith("https://github.com/joshyorko/rcc/releases/download/v18.19.3/"):
        raise ValueError(f"RCC {platform} URL is outside the official v18.19.3 release")
    return {**pin, "version": EXPECTED_RCC, "sha256": value}


def build_commands(*, rcc: str, robot: str, archive: str, artifact: str, receipt: str) -> list[list[str]]:
    del rcc
    return [
        ["env", "publish", "--robot", robot, "--provider", "local", "--json"],
        ["env", "export", "--artifact", artifact, "--provider", "local", "--output", archive],
        ["env", "acquire", "--archive", archive, "--permissive-local", "--json"],
        ["--no-build", "ht", "vars", "--robot", robot, "--json"],
        ["--no-build", "env", "exec", "--artifact", artifact, "--permissive-local", "--inherit-streams", "--receipt-file", receipt, "--", "python", "-m", "josh_room", "dimensions", "list", "--json"],
    ]


def controller_crypto_command(*, artifact: str, receipt: str, script: str) -> list[str]:
    return [
        "--no-build",
        "env",
        "exec",
        "--artifact",
        artifact,
        "--permissive-local",
        "--inherit-streams",
        "--receipt-file",
        receipt,
        "--",
        "python",
        script,
    ]


def _json_result(stdout: str) -> dict | list:
    decoder = json.JSONDecoder()
    values = []
    index = 0
    while index < len(stdout):
        try:
            value, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            index += 1
        else:
            values.append(value)
            index += end
    for value in reversed(values):
        if isinstance(value, (dict, list)):
            return value
    raise ValueError("RCC command returned no JSON object or array")


def _run(
    rcc: Path,
    args: list[str],
    *,
    home: Path,
    cwd: Path,
    receipt: Path | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> dict | list:
    environment = {
        **os.environ,
        "ROBOCORP_HOME": str(home),
        "RCC_HOLOTREE_MODE": "private",
        **(environment_overrides or {}),
    }
    process = subprocess.run([str(rcc), *args], cwd=cwd, env=environment, capture_output=True, text=True, check=False)
    for line in process.stderr.splitlines():
        print(line, flush=True)
    if process.returncode:
        raise RuntimeError(f"RCC {' '.join(args)} failed with exit {process.returncode}: {process.stderr.strip()[-2048:]}")
    if receipt and receipt.is_file():
        return _json_result(receipt.read_text())
    if receipt:
        raise RuntimeError(f"RCC command did not write the required receipt: {receipt}")
    if "--json" in args:
        return _json_result(process.stdout)
    return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def destination_stage(destination: Path) -> Path:
    """Reserve a temporary artifact path on the destination filesystem."""
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".stage", dir=destination.parent)
    os.close(fd)
    stage = Path(name)
    stage.unlink()
    return stage


def validate_receipt(receipt: dict, *, expected_platform: str, expected_rcc: str) -> None:
    required = {"artifact_digest", "specification_digest", "legacy_blueprint_key", "archive", "rcc_version", "source", "platform", "verified_acquire", "verified_no_build", "verified_exec", "verified_crypto"}
    if not required <= receipt.keys():
        raise ValueError("controller artifact receipt is missing provenance fields")
    if receipt["rcc_version"] != expected_rcc:
        raise ValueError("controller artifact receipt RCC version does not match")
    if receipt["platform"] != expected_platform:
        raise ValueError("controller artifact receipt platform does not match")
    if not isinstance(receipt["artifact_digest"], str) or not receipt["artifact_digest"].startswith("sha256:"):
        raise ValueError("controller artifact receipt artifact digest is invalid")
    if not isinstance(receipt["specification_digest"], str) or not receipt["specification_digest"].startswith("sha256:"):
        raise ValueError("controller artifact receipt specification digest is invalid")
    if not isinstance(receipt["source"], str) or len(receipt["source"]) != 40:
        raise ValueError("controller artifact receipt source commit is invalid")
    if not isinstance(receipt["archive"].get("sha256"), str) or len(receipt["archive"]["sha256"]) != SHA256_LENGTH:
        raise ValueError("controller artifact receipt archive SHA256 is invalid")
    if not isinstance(receipt["archive"].get("size"), int) or receipt["archive"]["size"] < 1:
        raise ValueError("controller artifact receipt archive size is invalid")
    if not all(receipt[name] is True for name in ("verified_acquire", "verified_no_build", "verified_exec", "verified_crypto")):
        raise ValueError("controller artifact receipt is not fully verified")


def build(*, manifest_path: Path, rcc: Path, platform: str, rcc_checksum: str | None, output_dir: Path, repository: Path) -> dict:
    manifest = load_manifest(manifest_path)
    pin = resolve_rcc_pin(manifest, platform, rcc_checksum)
    version = subprocess.run([str(rcc), "version"], capture_output=True, text=True, check=False)
    if version.returncode != 0 or EXPECTED_RCC not in f"{version.stdout}\n{version.stderr}":
        raise RuntimeError(f"managed RCC version verification failed: expected {EXPECTED_RCC}")
    if _sha256(rcc) != pin["sha256"]:
        raise ValueError("managed RCC checksum does not match the canonical v18.19.3 pin")
    output_dir.mkdir(parents=True, exist_ok=True)
    asset_names = manifest["controller"].get("artifact_assets", {})
    asset = asset_names.get(platform, manifest["controller"].get("artifact_asset"))
    if not asset:
        raise ValueError(f"controller artifact naming is not configured for {platform}")
    archive = output_dir / asset
    receipt_path = output_dir / f"{archive.stem}.json"
    if archive.exists() or receipt_path.exists():
        raise FileExistsError("refusing to overwrite an existing controller artifact or receipt")
    robot = (repository / manifest["controller"]["robot"]).resolve()
    crypto_smoke = (repository / "scripts" / "controller_crypto_smoke.py").resolve()
    if not crypto_smoke.is_file():
        raise FileNotFoundError("controller crypto smoke script is missing")
    source = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    stage = destination_stage(archive)
    try:
        with tempfile.TemporaryDirectory(prefix="josh-room-controller-build-") as temporary:
            builder_home = Path(temporary) / "builder"
            consumer_home = Path(temporary) / "consumer"
            publish = _run(rcc, ["env", "publish", "--robot", str(robot), "--provider", "local", "--json"], home=builder_home, cwd=robot.parent)
            artifact = publish.get("artifactDigest") or publish.get("artifact_digest")
            if not isinstance(artifact, str) or not artifact.startswith("sha256:"):
                raise ValueError("RCC publish did not return artifactDigest")
            exported = _run(rcc, ["env", "export", "--artifact", artifact, "--provider", "local", "--output", str(stage)], home=builder_home, cwd=robot.parent)
            del exported
            if not stage.is_file():
                raise RuntimeError("RCC export did not create the controller archive")
            acquire = _run(rcc, ["env", "acquire", "--archive", str(stage), "--permissive-local", "--json"], home=consumer_home, cwd=robot.parent)
            if acquire.get("artifactDigest", acquire.get("artifact_digest")) != artifact or acquire.get("verification", {}).get("valid") is not True:
                raise ValueError("fresh RCC acquire did not verify the controller artifact")
            _run(rcc, ["--no-build", "ht", "vars", "--robot", str(robot), "--json"], home=consumer_home, cwd=robot.parent)
            controller_environment = {
                "PYTHONPATH": str(robot.parent),
                "JOSH_ROOM_CONTROLLER_ROOT": str(robot.parent),
                "JOSH_ROOM_EXTENSION_MODE": "1",
            }
            exec_receipt = Path(temporary) / "exec-receipt.json"
            execution = _run(
                rcc,
                ["--no-build", "env", "exec", "--artifact", artifact, "--permissive-local", "--inherit-streams", "--receipt-file", str(exec_receipt), "--", "python", "-m", "josh_room", "dimensions", "list", "--json"],
                home=consumer_home,
                cwd=robot.parent,
                receipt=exec_receipt,
                environment_overrides=controller_environment,
            )
            if execution.get("exitCode", execution.get("exit_code", 0)) != 0:
                raise RuntimeError("controller dimensions list failed in the acquired artifact")
            crypto_receipt = Path(temporary) / "crypto-exec-receipt.json"
            crypto_execution = _run(
                rcc,
                controller_crypto_command(
                    artifact=artifact,
                    receipt=str(crypto_receipt),
                    script=str(crypto_smoke),
                ),
                home=consumer_home,
                cwd=robot.parent,
                receipt=crypto_receipt,
                environment_overrides=controller_environment,
            )
            if crypto_execution.get("exitCode", crypto_execution.get("exit_code", 0)) != 0:
                raise RuntimeError("controller age encrypt/decrypt smoke failed in the acquired artifact")
            specification = publish.get("specificationDigest") or publish.get("specification_digest")
            blueprint = publish.get("legacyBlueprintKey") or publish.get("legacy_blueprint_key")
            if not isinstance(specification, str) or not specification.startswith("sha256:") or not isinstance(blueprint, str) or not blueprint:
                raise ValueError("RCC publish did not return specificationDigest and legacyBlueprintKey")
            receipt = {
                "format_version": 1,
                "artifact_digest": artifact,
                "specification_digest": specification,
                "legacy_blueprint_key": blueprint,
                "archive": {"sha256": _sha256(stage), "size": stage.stat().st_size},
                "rcc_version": EXPECTED_RCC,
                "source": source,
                "platform": platform,
                "verified_acquire": True,
                "verified_no_build": True,
                "verified_exec": True,
                "verified_crypto": True,
            }
            validate_receipt(receipt, expected_platform=platform, expected_rcc=EXPECTED_RCC)
            stage.replace(archive)
    finally:
        stage.unlink(missing_ok=True)
    temporary_receipt = receipt_path.with_name(f".{receipt_path.name}.{os.getpid()}")
    temporary_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    temporary_receipt.chmod(0o600)
    temporary_receipt.replace(receipt_path)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("vscode-extension/runtime/controller-artifact-manifest.json"))
    parser.add_argument("--rcc", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux-x64", "win32-x64"), default="linux-x64")
    parser.add_argument("--rcc-sha256")
    parser.add_argument("--output-dir", type=Path, default=Path("dist/controller-artifact"))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    build(manifest_path=args.manifest, rcc=args.rcc, platform=args.platform, rcc_checksum=args.rcc_sha256, output_dir=args.output_dir, repository=args.repository)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
