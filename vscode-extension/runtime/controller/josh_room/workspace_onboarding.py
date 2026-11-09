"""Local evidence discovery and create-only Room onboarding, without executing code."""
from __future__ import annotations

import itertools
import json
import os
import re
import stat
import tomllib
from pathlib import Path

EDITOR_FILES = ("tasks.json", "launch.json", "extensions.json", "settings.json")
_MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
_DOCS = ("README.md", "ARCHITECTURE.md", "docs/architecture.md")
_SKIP = {".git", ".venv", "node_modules", "target", "vendor", ".vscode", ".cache", "dist", "build"}
_LIMIT = 64 * 1024
_HAS_DIR_FD = os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd


def _directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    absolute = Path(os.path.abspath(path))
    if not _HAS_DIR_FD:
        return os.open(absolute, flags)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def _read(path: Path) -> str | None:
    # Every parent was admitted by the bounded walk; reject symlinked docs too.
    if any(p.is_symlink() for p in (path, *path.parents)):
        return None
    try:
        directory_fd = _directory(path.parent)
        try:
            kwargs = {"dir_fd": directory_fd} if _HAS_DIR_FD else {}
            fd = os.open(path.name if kwargs else path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), **kwargs)
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(_LIMIT + 1)
            return data.decode("utf-8") if len(data) <= _LIMIT else None
    except (OSError, UnicodeError):
        return None


def discover(root: Path) -> dict:
    directories = [root]
    projects, instructions, warnings = [], [], []
    visited = 0
    while directories and len(projects) < 32 and visited < 128:
        visited += 1
        directory = directories.pop(0)
        relative = directory.relative_to(root).as_posix()
        evidence = {}
        for name in (*_MANIFESTS, *_DOCS):
            content = _read(directory / name)
            if content is not None:
                evidence[name] = content
        if evidence:
            marker = directory / ".git"
            boundary = not marker.is_symlink() and (marker.is_file() or marker.is_dir())
            projects.append({"directory": relative, "evidence": evidence, "repository": boundary})
        if directory != root and _read(directory / "AGENTS.md") is not None:
            instructions.append((directory.relative_to(root) / "AGENTS.md").as_posix())
        if len(directory.relative_to(root).parts) >= 3:
            warnings.append(f"Discovery depth limit at {relative}")
            continue
        try:
            with os.scandir(directory) as entries:
                candidates = list(itertools.islice(entries, 257))
            if len(candidates) > 256:
                warnings.append(f"Directory entry limit at {relative}")
            for entry in sorted(candidates[:256], key=lambda item: item.name):
                if entry.name.startswith(".") or entry.name in _SKIP:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
        except OSError:
            warnings.append(f"Directory unavailable at {relative}")
        if len(directories) > 128:
            directories = directories[:128]
            warnings.append("Directory budget reached")
    if directories:
        warnings.append("Discovery budget reached; discovery incomplete")
    return {"projects": projects, "instructions": instructions, "warnings": warnings}


def _link(directory: str, filename: str) -> str:
    target = (Path(directory) / filename).as_posix()
    return f"[{target}]({target.replace(' ', '%20')})"


def _commands(project: dict) -> list[dict]:
    evidence, directory = project["evidence"], project["directory"]
    commands = []
    try:
        package = json.loads(evidence.get("package.json", "{}"))
        scripts = package.get("scripts", {})
        if isinstance(scripts, dict):
            for name, command in list(scripts.items())[:20]:
                if isinstance(command, str) and re.fullmatch(r"[A-Za-z0-9_][\w:-]*", name):
                    commands.append({"command": f"npm run {name}", "directory": directory,
                                     "evidence": "package.json", "detail": command})
    except (ValueError, AttributeError):
        pass
    # Only self-contained commands explicitly documented in a shell code fence.
    for filename in _DOCS:
        text = evidence.get(filename, "")
        for block in re.findall(r"```(?:sh|shell|bash|zsh|console)\n(.*?)```", text, re.DOTALL):
            if re.search(r"(?m)^\s*(?:\$ )?cd\s", block):
                continue  # CWD transitions require interpretation; do not guess.
            for line in block.splitlines():
                command = line.strip().removeprefix("$ ")
                if command in {"pytest", "make", "just", "npm test", "npm start"} or re.fullmatch(r"(?:uv run|python3? -m|pytest|npm (?:run|test|start)|pnpm|cargo|go (?:test|run|build)|make|just) [\w ./:=,@+-]+", command):
                    commands.append({"command": command, "directory": directory,
                                     "evidence": filename, "detail": "documented command"})
    return commands[:30]


def _guide(discovery: dict) -> bytes:
    lines = ["# Room onboarding", "", "Generated from bounded local documentation and manifests; no commands were executed.",
             "Documented commands are evidence, not proof they succeed on this host. Review them before running.", "",
             "## Instruction scopes", "Nested instructions apply only within their own directories."]
    lines += [f"- {_link('.', item)}" for item in discovery["instructions"]] or ["No nested AGENTS.md was discovered within the scan budget."]
    for project in discovery["projects"]:
        directory, evidence = project["directory"], project["evidence"]
        lines += ["", f"## Component in `{directory}`", f"Working directory: `{directory}`.", "",
                  "Evidence: " + ", ".join(_link(directory, name) for name in evidence) + "."]
        lines.append("Repository boundary: local .git marker present (including linked-worktree markers)." if project["repository"]
                     else "No local repository boundary established here; this may be a component of an enclosing repository.")
        for name in _DOCS:
            text = evidence.get(name)
            if not text:
                continue
            paragraphs = re.split(r"\n\s*\n", text)
            excerpts = [p.strip() for p in paragraphs if p.strip() and not p.lstrip().startswith(("#", "```", "<", "[!"))][:2]
            if excerpts:
                lines += [f"Documentation excerpt ({_link(directory, name)}; current/proposed status follows that source):", ""]
                lines += ["> " + p[:900].replace("\n", "\n> ") for p in excerpts]
        try:
            metadata = tomllib.loads(evidence.get("pyproject.toml", "")).get("project", {})
            if metadata.get("name"):
                lines.append(f"Python project: `{metadata['name']}` ({_link(directory, 'pyproject.toml')}).")
            if metadata.get("description"):
                lines.append(f"Manifest purpose: {str(metadata['description'])[:300]} ({_link(directory, 'pyproject.toml')}).")
            dependencies = metadata.get("dependencies", [])
            if isinstance(dependencies, list) and dependencies:
                lines.append(f"Declared dependencies ({_link(directory, 'pyproject.toml')}): " + ", ".join(f"`{str(value)[:150]}`" for value in dependencies[:20]) + ".")
            for entry, target in list(metadata.get("scripts", {}).items())[:20]:
                lines.append(f"Declared CLI entry point `{entry}` → `{target}` ({_link(directory, 'pyproject.toml')}); installation/runtime availability unverified.")
        except (ValueError, TypeError, AttributeError):
            pass
        try:
            cargo = tomllib.loads(evidence.get("Cargo.toml", ""))
            metadata = cargo.get("package", {})
            if metadata.get("name"):
                lines.append(f"Rust package: `{metadata['name']}` ({_link(directory, 'Cargo.toml')}).")
            if metadata.get("description"):
                lines.append(f"Manifest purpose: {str(metadata['description'])[:300]}.")
            dependencies = cargo.get("dependencies", {})
            if isinstance(dependencies, dict) and dependencies:
                lines.append("Declared Rust dependencies: " + ", ".join(f"`{name}`" for name in list(dependencies)[:20]) + ".")
        except (ValueError, TypeError, AttributeError):
            pass
        module = re.search(r"(?m)^module\s+(\S+)", evidence.get("go.mod", ""))
        if module:
            lines.append(f"Go module: `{module[1]}` ({_link(directory, 'go.mod')}); its purpose and runnable entry points require documentation.")
        try:
            package = json.loads(evidence.get("package.json", "{}"))
            if isinstance(package.get("description"), str):
                lines.append(f"Manifest purpose: {package['description'][:300]} ({_link(directory, 'package.json')}).")
            dependencies = package.get("dependencies", {})
            if isinstance(dependencies, dict) and dependencies:
                lines.append(f"Declared dependencies ({_link(directory, 'package.json')}): " + ", ".join(f"`{name}`" for name in list(dependencies)[:20]) + ".")
        except (ValueError, TypeError, AttributeError):
            pass
        commands = _commands(project)
        lines += [f"- `{item['command']}` in `{directory}` from {_link(directory, item['evidence'])}." for item in commands]
        if not commands:
            lines.append("Development commands: unknown; consult the linked documentation.")
        lines.append("Dependencies and cross-component relationships are established only by the linked evidence; undocumented relationships remain unknown.")
    if not discovery["projects"]:
        lines += ["", "Purpose, architecture, entry points and commands are unknown; no supported documentation/manifests were discovered."]
    lines += ["", "## Discovery limits", "Only recognized local manifests/docs were read, within depth, entry, project and file-size limits. External symlinks, credential stores and project hooks were not read or run."]
    lines += [f"- {warning}" for warning in discovery["warnings"]]
    return ("\n".join(lines) + "\n").encode()


def _editor(root: Path, discovery: dict, selected: tuple[str, ...]) -> tuple[dict, dict]:
    artifacts, omitted = {}, {}
    tasks, recommendations, launches = [], set(), []
    for project in discovery["projects"]:
        directory = project["directory"]
        for item in _commands(project):
            tasks.append({"label": f"{directory}: {item['command']}", "type": "shell", "command": item["command"],
                          "options": {"cwd": "${workspaceFolder}" + ("/" + directory if directory != "." else "")}, "problemMatcher": []})
        evidence = project["evidence"]
        try:
            package = json.loads(evidence.get("package.json", "{}"))
            start = package.get("scripts", {}).get("start", "")
            match = re.fullmatch(r"node ([\w./-]+\.(?:js|mjs|cjs))", start)
            if match:
                relative = Path(directory) / match[1]
                program = root / relative
                if (not relative.is_absolute() and ".." not in relative.parts
                        and not any(part.startswith(".") and part != "." for part in relative.parts)
                        and not any(p.is_symlink() for p in (program, *program.parents))
                        and program.is_file()):
                    launches.append({"name": f"{directory}: documented Node start", "type": "pwa-node",
                                     "request": "launch", "program": "${workspaceFolder}/" + relative.as_posix(),
                                     "cwd": "${workspaceFolder}" + ("/" + directory if directory != "." else "")})
        except (ValueError, TypeError, AttributeError):
            pass
        if "pyproject.toml" in evidence:
            recommendations.add("ms-python.python")
        if "Cargo.toml" in evidence:
            recommendations.add("rust-lang.rust-analyzer")
        if "go.mod" in evidence:
            recommendations.add("golang.go")
    values = {}
    if tasks:
        values["tasks.json"] = {"version": "2.0.0", "tasks": tasks}
    if recommendations:
        values["extensions.json"] = {"recommendations": sorted(recommendations)}
    if launches:
        values["launch.json"] = {"version": "0.2.0", "configurations": launches}
    reasons = {"launch.json": "No debugger/runtime entry point was qualified; add a project-specific launch configuration after review.",
               "settings.json": "No project-specific editor settings were established by the local evidence.",
               "tasks.json": "No supported documented commands were discovered.",
               "extensions.json": "No language requiring an extension recommendation was established."}
    for name in selected:
        relative = ".vscode/" + name
        if name in values:
            artifacts[relative] = (json.dumps(values[name], indent=2) + "\n").encode()
        else:
            omitted[relative] = reasons[name]
    return artifacts, omitted


def _create(root: Path, relative: str, content: bytes) -> str:
    parent = root
    if relative.startswith(".vscode/"):
        parent = root / ".vscode"
    # Hold a directory descriptor so a concurrent rename/symlink cannot redirect writes.
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        return "unsafe-target"
    try:
        directory_fd = _directory(root)
    except OSError:
        return "unsafe-or-unavailable-target"
    try:
        if parent != root:
            if not _HAS_DIR_FD:
                return "safe-directory-creation-unavailable"
            try:
                os.mkdir(".vscode", dir_fd=directory_fd)
            except FileExistsError:
                pass
            try:
                child = os.open(".vscode", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            except OSError:
                return "unsafe-or-unavailable-target"
            os.close(directory_fd)
            directory_fd = child
        name = Path(relative).name
        if _HAS_DIR_FD:
            kwargs = {"dir_fd": directory_fd}
            target = name
        else:
            kwargs, target = {}, str(parent / name)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644, **kwargs)
        except FileExistsError:
            try:
                info = os.stat(target, follow_symlinks=False, **kwargs)
            except OSError:
                return "conflict"
            return "already-present" if stat.S_ISREG(info.st_mode) else "unsafe-target"
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return "created"
    finally:
        os.close(directory_fd)


def initialize_onboarding(root: Path, editor: str, selected: tuple[str, ...]) -> dict:
    discovery = discover(root)
    artifacts = {"AGENTS.md": _guide(discovery)}
    extras, omitted = _editor(root, discovery, selected) if editor == "vscode" else ({}, {})
    artifacts.update(extras)
    outcomes = {name: _create(root, name, content) for name, content in artifacts.items()}
    return {"editor": editor, "artifacts": outcomes, "omitted": omitted, "warnings": discovery["warnings"]}
