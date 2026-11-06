#!/usr/bin/env python3
"""Prepare and validate a versioned Josh Room release request."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.promote_rcc_candidate import _next_extension_version, _version_update

SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
TAG = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-standalone-vsix$"
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def version_tuple(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise ValueError("version must be canonical MAJOR.MINOR.PATCH")
    return tuple(map(int, match.groups()))


def safe_checkout(root: Path) -> None:
    if git(root, "status", "--porcelain"):
        raise ValueError(
            "selected source checkout must be clean before release preparation"
        )
    for relative in (
        "vscode-extension/package.json",
        "templates/room/vscode-extension/package.json",
        "vscode-extension/runtime/manifest.json",
        "templates/room/vscode-extension/runtime/manifest.json",
        "release-lock.json",
        "templates/room/vscode-extension/runtime/release-lock.json",
        "templates/room/devcontainer-template.json",
        ".devcontainer/bootstrap.sh",
        "templates/room/.devcontainer/bootstrap.sh",
        "CHANGELOG.md",
    ):
        path = root / relative
        cursor = root
        for part in Path(relative).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(
                    f"repository path must not contain symlinks: {relative}"
                )
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            if relative == "CHANGELOG.md" and not path.exists():
                continue
            raise ValueError(f"expected regular repository file: {relative}")


def prepare(args: argparse.Namespace) -> dict:
    root = args.root.resolve()
    safe_checkout(root)
    current = git(root, "rev-parse", "HEAD")
    main = git(root, "rev-parse", "refs/remotes/origin/main")
    if subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", main, current],
        check=False,
        capture_output=True,
    ).returncode:
        raise ValueError(
            "source branch is stale or does not include current main; update it and retry"
        )
    releases = json.loads(args.releases.read_text(encoding="utf-8"))
    if not isinstance(releases, list):
        raise TypeError("release inventory must be a JSON array")
    published = []
    release_tags = set()
    for release in releases:
        if not isinstance(release, dict):
            continue
        tag = release.get("tag_name", "")
        if isinstance(tag, str):
            release_tags.add(tag)
        if release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        match = TAG.fullmatch(tag)
        if match:
            published.append((tuple(map(int, match.groups())), tag))
    if not published:
        raise ValueError(
            "no published standalone VSIX release tag is available as an ancestor baseline"
        )
    ancestors = []
    for ver, tag in published:
        try:
            target = git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        except subprocess.CalledProcessError:
            continue
        if not subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", target, current],
            check=False,
            capture_output=True,
        ).returncode:
            ancestors.append((ver, tag, target))
    if not ancestors:
        raise ValueError(
            "no published standalone VSIX tag is an ancestor of the selected source"
        )
    _, baseline, baseline_sha = max(ancestors)
    existing = [item[0] for item in published]
    next_patch_version = _next_extension_version(root)
    current_version = json.loads(
        (root / "vscode-extension/package.json").read_text(encoding="utf-8")
    )["version"]
    if args.version in {"patch", "minor", "major"}:
        major, minor, _ = version_tuple(current_version)
        if args.version == "patch":
            requested = version_tuple(next_patch_version)
        elif args.version == "minor":
            requested = (major, minor + 1, 0)
        else:
            requested = (major + 1, 0, 0)
    elif args.version:
        requested = version_tuple(args.version)
    else:
        requested = version_tuple(current_version)
    if requested <= version_tuple(current_version) or (
        existing and requested <= max(existing)
    ):
        raise ValueError(
            "requested version must exceed current and every published standalone version"
        )
    version = ".".join(map(str, requested))
    branch = f"release/v{version}"
    tag = f"v{version}-standalone-vsix"
    if tag in release_tags:
        raise ValueError(f"release collision exists at {tag}")
    for ref in (
        f"refs/heads/{branch}",
        f"refs/remotes/origin/{branch}",
        f"refs/tags/{tag}",
    ):
        if (
            subprocess.run(
                ["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref],
                check=False,
            ).returncode
            == 0
        ):
            raise ValueError(f"release collision exists at {ref}")
    subjects = git(
        root, "log", "--no-merges", "--format=%s", f"{baseline_sha}..{current}"
    ).splitlines()
    if not subjects:
        raise ValueError(
            "selected source has no non-merge commits after the published baseline"
        )
    section = f"## [{version}]\n\n" + "\n".join(f"- {s}" for s in subjects) + "\n"
    changelog = root / "CHANGELOG.md"
    old = (
        changelog.read_text(encoding="utf-8")
        if changelog.exists()
        else "# Changelog\n\n"
    )
    if re.search(rf"(?m)^## \[{re.escape(version)}\]", old):
        raise ValueError(
            f"changelog already contains {version}; review its existing notes"
        )
    if old.startswith("# Changelog"):
        title, _, previous = old.partition("\n")
        updated = f"{title}\n\n{section}\n{previous.lstrip()}".rstrip() + "\n"
    else:
        updated = section + "\n" + old.lstrip()
    changes = _version_update(root, version, [])
    planned = {path: data for path, data in changes}
    planned[changelog] = updated.encode()
    changed = []
    for path, data in planned.items():
        if not path.is_relative_to(root) or path.is_symlink():
            raise ValueError(f"refusing unsafe output path: {path}")
        changed.append(path.relative_to(root).as_posix())
    # All validation and generation completes before the first write.
    for path, data in planned.items():
        path.write_bytes(data)
    body = (
        f"## Release request v{version}\n\nSource: `{current}`; published baseline: `{baseline}` (`{baseline_sha}`).\n\n"
        f"{section}\n"
        f"Changes are listed in `CHANGELOG.md`. This draft PR requires full Linux and Windows CI, managed runtime qualification, "
        "review, and merge to main. Staging and publication remain separate; the owner Josh must explicitly approve the exact staged main source.\n\n"
        "Documentation impact: this release request adds its generated release notes to the canonical CHANGELOG.md.\n\n"
        "Verification evidence: requested source SHA is dispatched through the existing full CI workflow after branch creation.\n\n"
        "Remaining limits: platform/provider qualification and live release publication are not established by request preparation."
    )
    return {
        "version": version,
        "source_sha": current,
        "baseline": baseline,
        "baseline_sha": baseline_sha,
        "branch": branch,
        "changed_files": sorted(changed),
        "body": body,
    }


def validate(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    path = root / "CHANGELOG.md"
    if path.is_symlink() or (root / "vscode-extension").is_symlink():
        raise ValueError("CHANGELOG.md must not be a symlink")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    version = json.loads((root / "vscode-extension/package.json").read_text())[
        "version"
    ]
    section_pattern = re.compile(rf"(?m)^## \[{re.escape(version)}\](?:[ \t].*)?$")
    sections = list(section_pattern.finditer(text))
    bodies = []
    all_headers = list(re.finditer(r"(?m)^## \[.*\].*$", text))
    for section in sections:
        end = next(
            (
                header.start()
                for header in all_headers
                if header.start() > section.start()
            ),
            len(text),
        )
        bodies.append(text[section.end() : end].strip())
    if args.branch.startswith("release/") and len(sections) != 1:
        raise ValueError(
            f"release branch requires exactly one current-version changelog section ({version})"
        )
    if len(sections) > 1 or any(not body for body in bodies):
        raise ValueError(
            f"current-version changelog section must be unique and nonempty ({version})"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--releases", type=Path, required=True)
    p.add_argument("--version", default="patch")
    v = sub.add_parser("validate")
    v.add_argument("--root", type=Path, required=True)
    v.add_argument("--branch", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args) if args.command == "prepare" else validate(args)
        if args.command == "prepare":
            print(json.dumps(result, sort_keys=True))
        else:
            print("changelog valid")
        return 0
    except (
        TypeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"request-release: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
