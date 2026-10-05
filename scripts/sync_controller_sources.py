"""Synchronize canonical controller modules into both shipped extension copies."""

from __future__ import annotations

import argparse
from pathlib import Path


def synchronize(root: Path, *, names: list[str] | None = None) -> list[str]:
    source = root / "src/josh_room"
    files = [source / name for name in names] if names else sorted(source.glob("*.py"))
    files.extend([source / "workspace_capture_defaults.json"])
    files.extend(sorted(path for path in (source / "_vendor").rglob("*")
                        if path.suffix in {".py", ".json"} or path.name == "LICENSE"))
    files = [path for path in files if path.is_file()]
    changed = []
    for relative in ("vscode-extension/runtime/controller", "templates/room/vscode-extension/runtime/controller"):
        package = root / relative / "josh_room"
        for path in files:
            if source not in path.parents or path.is_symlink() or not path.is_file():
                raise ValueError("controller source must be a regular canonical module")
            target = package / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = path.read_bytes()
            if not target.exists() or target.read_bytes() != content:
                target.write_bytes(content)
                changed.append(str(target.relative_to(root)))
        installer = root / relative / "install_restic.py"
        content = (root / "scripts/install_restic.py").read_bytes()
        if not installer.exists() or installer.read_bytes() != content:
            installer.write_bytes(content)
            changed.append(str(installer.relative_to(root)))
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--module", action="append", dest="modules")
    args = parser.parse_args()
    changed = synchronize(args.root, names=args.modules)
    print(f"Synchronized {len(changed)} controller files.")


if __name__ == "__main__":
    main()
