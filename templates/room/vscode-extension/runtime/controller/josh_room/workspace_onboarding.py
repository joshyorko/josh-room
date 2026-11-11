"""Local evidence discovery and create-only Room onboarding, without executing code."""
from __future__ import annotations

import errno
import itertools
import json
import os
import re
import shlex
import stat
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

EDITOR_FILES = ("tasks.json", "launch.json", "extensions.json", "settings.json")
_MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
_DOCS = ("README.md", "ARCHITECTURE.md", "docs/architecture.md")
_SKIP = {".git", ".venv", "node_modules", "target", "vendor", ".vscode", ".cache", "dist", "build"}
_LIMIT = 64 * 1024
_CAPS = {"projects": 32, "visited_directories": 128, "pending_directories": 128,
         "directory_entries": 256, "depth": 3, "documents_per_component": 8,
         "file_bytes": _LIMIT, "examples_per_reason": 3}
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


def _read(path: Path, omit=None, *, required=False) -> str | None:
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
            if len(data) > _LIMIT:
                if omit:
                    omit("file-size-cap", path)
                return None
            return data.decode("utf-8")
    except FileNotFoundError:
        if omit and required:
            omit("file-unavailable", path)
        return None
    except (OSError, UnicodeError):
        if omit:
            omit("file-unavailable", path)
        return None


def discover(root: Path) -> dict:
    # Separate directory admission from component admission: a README-only
    # directory must not consume a manifest's slot or stop instruction discovery.
    observed = {name: 0 for name in (
        "directories_visited", "directories_probed", "directory_entries_observed",
        "components_observed", "components_admitted", "instruction_scopes",
        "documents_read", "additional_document_links_observed")}
    omissions = {}

    def omit(code, path, count=1, scope="."):
        item = omissions.setdefault((code, scope), {
            "code": code, "scope": scope, "observed": 0, "examples": []})
        item["observed"] += count
        relative = path.relative_to(root).as_posix()
        # Examples are display-only, bounded relative paths, never payloads.
        example = re.sub(r"[^A-Za-z0-9_./ -]", "?", relative)[:160]
        if example not in item["examples"] and len(item["examples"]) < _CAPS["examples_per_reason"]:
            item["examples"].append(example)

    def probe(directory):
        observed["directories_probed"] += 1
        names = set()
        try:
            if any(p.is_symlink() for p in (directory, *directory.parents)):
                return names
            descriptor = _directory(directory)
            try:
                for name in (".git", "AGENTS.md", *_MANIFESTS, *_DOCS):
                    # docs/architecture.md is checked during evidence reading;
                    # do not follow its intermediate directory while probing.
                    if "/" in name:
                        continue
                    try:
                        kwargs = {"dir_fd": descriptor} if _HAS_DIR_FD else {}
                        info = os.stat(name if kwargs else directory / name,
                                       follow_symlinks=False, **kwargs)
                        if stat.S_ISREG(info.st_mode) or (name == ".git" and stat.S_ISDIR(info.st_mode)):
                            names.add(name)
                    except OSError as error:
                        if error.errno != errno.ENOENT:
                            omit("file-unavailable", directory / name)
                if not (directory / "docs").is_symlink():
                    try:
                        docs_fd = _directory(directory / "docs")
                        try:
                            kwargs = {"dir_fd": docs_fd} if _HAS_DIR_FD else {}
                            info = os.stat("architecture.md" if kwargs else directory / "docs/architecture.md",
                                           follow_symlinks=False, **kwargs)
                            if stat.S_ISREG(info.st_mode):
                                names.add("docs/architecture.md")
                        finally:
                            os.close(docs_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        omit("file-unavailable", directory / "docs/architecture.md")
            finally:
                os.close(descriptor)
        except OSError:
            omit("directory-unavailable", directory)
        return names

    def priority(item):
        directory, names = item
        rank = (0 if "AGENTS.md" in names else
                1 if ".git" in names or names.intersection(_MANIFESTS) else
                2 if names.intersection(_DOCS) else 3)
        return rank, len(directory.relative_to(root).parts), directory.relative_to(root).as_posix()

    directories = [(root, probe(root))]
    nodes, instructions, boundaries = [], [], set()
    while directories and observed["directories_visited"] < _CAPS["visited_directories"]:
        directories.sort(key=priority)
        directory, names = directories.pop(0)
        observed["directories_visited"] += 1
        if ".git" in names or names.intersection(_MANIFESTS):
            boundaries.add(directory)
        if names.intersection((*_MANIFESTS, *_DOCS)) or ".git" in names:
            nodes.append((directory, names))
        if directory != root and "AGENTS.md" in names and _read(directory / "AGENTS.md", omit, required=True) is not None:
            instructions.append((directory.relative_to(root) / "AGENTS.md").as_posix())
        try:
            descriptor = _directory(directory)
            try:
                with os.scandir(descriptor if _HAS_DIR_FD else directory) as entries:
                    candidates = list(itertools.islice(entries, _CAPS["directory_entries"] + 1))
                    children = []
                    for entry in candidates[:_CAPS["directory_entries"]]:
                        if entry.name.startswith(".") or entry.name in _SKIP:
                            continue
                        try:
                            # DT_UNKNOWN filesystems need the live directory
                            # descriptor for this type check.
                            if entry.is_dir(follow_symlinks=False):
                                children.append(entry.name)
                        except OSError:
                            omit("directory-unavailable", directory / entry.name)
            finally:
                os.close(descriptor)
            observed["directory_entries_observed"] += len(candidates)
            if len(candidates) > _CAPS["directory_entries"]:
                omit("directory-entry-cap", directory)
            for name in sorted(children):
                child = directory / name
                if len(directory.relative_to(root).parts) >= _CAPS["depth"]:
                    omit("depth-cap", child)
                    continue
                child_names = probe(child)
                if ".git" in child_names or child_names.intersection(_MANIFESTS):
                    boundaries.add(child)
                directories.append((child, child_names))
        except OSError:
            omit("directory-unavailable", directory)
        if len(directories) > _CAPS["pending_directories"]:
            directories.sort(key=priority)
            dropped = directories[_CAPS["pending_directories"]:]
            for child, _ in dropped:
                omit("pending-directory-cap", child)
            directories = directories[:_CAPS["pending_directories"]]
    for directory, _ in directories:
        omit("visited-directory-cap", directory)

    # Keep independent boundaries/manifests ahead of incidental documentation.
    nodes.sort(key=lambda item: (0 if ".git" in item[1] or item[1].intersection(_MANIFESTS) else 1,
                                 len(item[0].relative_to(root).parts), item[0].relative_to(root).as_posix()))
    observed["components_observed"] = len(nodes)
    for directory, _ in nodes[_CAPS["projects"]:]:
        omit("project-cap", directory)
    projects = []
    for directory, names in nodes[:_CAPS["projects"]]:
        relative = directory.relative_to(root).as_posix()
        evidence = {}
        for name in (*_MANIFESTS, *_DOCS):
            content = _read(directory / name, omit, required=name in names)
            if content is not None:
                evidence[name] = content
        documents = [name for name in _DOCS if name in evidence]
        seen = {Path(os.path.abspath(directory / name)) for name in documents}
        for filename in documents:
            for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^\s)]+)\)", evidence[filename]):
                try:
                    parsed = urlsplit(match[1])
                except ValueError:
                    continue
                if parsed.scheme or parsed.netloc or parsed.query or not parsed.path:
                    continue
                decoded = unquote(parsed.path)
                if any(ord(character) < 0x20 or ord(character) == 0x7f for character in decoded):
                    continue
                linked = Path(os.path.abspath(directory / Path(filename).parent / decoded))
                if not linked.is_relative_to(root) or linked.suffix.lower() != ".md":
                    continue
                parts = linked.relative_to(root).parts
                if any(part.startswith(".") or part in _SKIP for part in parts):
                    continue
                if linked in seen or any(p.is_symlink() for p in (linked, *linked.parents)):
                    continue
                seen.add(linked)
                # Additional links are observed eligible paths, not claims that
                # their unread contents exist, fit the file cap, or are useful.
                if len(documents) >= _CAPS["documents_per_component"]:
                    observed["additional_document_links_observed"] += 1
                    omit("document-cap", linked, scope=relative)
                    continue
                content = _read(linked, omit, required=True)
                if content is not None:
                    name = os.path.relpath(linked, directory)
                    evidence[name] = content
                    documents.append(name)
        observed["documents_read"] += len(documents)
        if evidence or ".git" in names:
            projects.append({"directory": relative, "evidence": evidence,
                             "repository": ".git" in names, "documents": documents, "root": root})
    observed["components_admitted"] = len(projects)
    observed["instruction_scopes"] = len(instructions)
    for project in projects:
        project["command_documents"] = []
        for filename in project["documents"]:
            document = Path(os.path.abspath(root / project["directory"] / filename))
            # A linked document can cross a boundary that directory discovery
            # never admitted. Qualify ownership along its ancestry as well.
            for parent in document.parents:
                if not parent.is_relative_to(root):
                    break
                try:
                    descriptor = _directory(parent)
                    try:
                        for name in (".git", *_MANIFESTS):
                            kwargs = {"dir_fd": descriptor} if _HAS_DIR_FD else {}
                            try:
                                info = os.stat(name if kwargs else parent / name, follow_symlinks=False, **kwargs)
                            except FileNotFoundError:
                                continue
                            if stat.S_ISREG(info.st_mode) or (name == ".git" and stat.S_ISDIR(info.st_mode)):
                                boundaries.add(parent)
                                break
                    finally:
                        os.close(descriptor)
                except OSError:
                    omit("directory-unavailable", parent)
                    boundaries.add(parent)  # Unknown ownership cannot authorize an outer task.
            owners = [boundary for boundary in boundaries if document.is_relative_to(boundary)]
            owner = max(owners, key=lambda candidate: len(candidate.parts), default=root / project["directory"])
            if owner == root / project["directory"]:
                project["command_documents"].append(filename)
        ancestors = sorted((candidate for candidate in projects
                            if (root / project["directory"]).is_relative_to(root / candidate["directory"])),
                           key=lambda candidate: len(Path(candidate["directory"]).parts), reverse=True)
        for ancestor in ancestors:
            try:
                package = json.loads(ancestor["evidence"].get("package.json", "{}"))
                declared = package.get("packageManager", "")
                match = re.fullmatch(r"(npm|pnpm|yarn|bun)@\S+", declared)
                if match:
                    project["package_manager"] = match[1]
                    break
            except (ValueError, TypeError, AttributeError):
                pass
            if ancestor["repository"]:
                break
    items = sorted(omissions.values(), key=lambda item: (item["code"], item["scope"]))
    warnings = list(dict.fromkeys(
        f"{item['code']} at {re.sub(r'[^A-Za-z0-9_./ -]', '?', item['scope'])}: "
        f"{item['observed']} observed omission(s); further coverage unknown." for item in items))
    coverage = {"scope": ".", "complete": not items, "status": "partial" if items else "complete",
                "observed": observed, "caps": dict(_CAPS), "omissions": items,
                "unexplored_descendants": "unknown" if items else "none within supported scope",
                "enumeration_order_dependent": any(item["code"] == "directory-entry-cap" for item in items)}
    return {"projects": projects, "instructions": sorted(instructions), "warnings": warnings, "coverage": coverage}


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
                    commands.append({"command": f"{project.get('package_manager', 'npm')} run {name}", "directory": directory,
                                     "evidence": "package.json", "detail": command})
    except (ValueError, AttributeError):
        pass
    # Only self-contained commands explicitly documented in a shell code fence.
    for filename in project["command_documents"]:
        text = evidence.get(filename, "")
        for block in re.findall(r"```(?:sh|shell|bash|zsh|console)\n(.*?)```", text, re.DOTALL):
            working_directory = directory
            for line in block.splitlines():
                command = line.strip().removeprefix("$ ")
                if re.match(r"(?:cd|pushd|popd)(?:\s|$)", command):
                    try:
                        parts = shlex.split(command)
                        if len(parts) != 2 or parts[0] != "cd" or any(c in parts[1] for c in "$`~;&|<>"):
                            break
                        root = project["root"]
                        target = Path(os.path.abspath(root / working_directory / parts[1]))
                        if not target.is_relative_to(root):
                            break
                        descriptor = _directory(target)
                        os.close(descriptor)
                        working_directory = target.relative_to(root).as_posix()
                    except (OSError, ValueError):
                        break
                    continue
                if command in {"pytest", "make", "just", "npm test", "npm start"} or re.fullmatch(r"(?:uv run|python3? -m|pytest|npm (?:run|test|start)|pnpm|yarn|bun|cargo|go (?:test|run|build)|make|just) [\w ./:=,@+-]+", command):
                    commands.append({"command": command, "directory": working_directory,
                                     "evidence": filename, "detail": "documented command"})
    unique = {(item["command"], item["directory"]): item for item in commands}
    return list(unique.values())[:30]


def _documentation_excerpt(text: str) -> list[str]:
    # Keep heading ancestry: dropping a "Proposed" parent changes the meaning.
    headings, excerpts, paragraph = [], [], []
    fenced = False

    def flush():
        if paragraph and len(excerpts) < 12:
            context = " / ".join(title for _, title in headings)
            excerpts.append((context, "\n".join(paragraph)[:900]))
        paragraph.clear()

    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            flush()
            fenced = not fenced
        elif not fenced:
            heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            setext = re.fullmatch(r" {0,3}(=+|-+)\s*", line) if paragraph else None
            if heading:
                flush()
                level = len(heading[1])
                headings[:] = [(depth, title) for depth, title in headings if depth < level]
                headings.append((level, heading[2]))
            elif setext:
                level = 1 if setext[1].startswith("=") else 2
                headings[:] = [(depth, title) for depth, title in headings if depth < level]
                headings.append((level, " ".join(paragraph).strip()))
                paragraph.clear()
            elif not line.strip():
                flush()
            elif not line.lstrip().startswith(("<", "[!")):
                paragraph.append(line)
    flush()
    lines = []
    for context, paragraph in excerpts:
        if context:
            lines.append(f"Source section: **{context}**")
        lines.extend(["> " + paragraph.replace("\n", "\n> "), ""])
    if len(excerpts) == 12:
        lines.append("Excerpt limit reached; consult the linked document for further sections.")
    return lines


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
        for name in project["documents"]:
            text = evidence.get(name)
            if not text:
                continue
            excerpts = _documentation_excerpt(text)
            if excerpts:
                lines += [f"Documentation excerpt ({_link(directory, name)}):", "", *excerpts]
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
        lines += [f"- `{item['command']}` in `{item['directory']}` from {_link(directory, item['evidence'])}." for item in commands]
        if not commands:
            lines.append("Development commands: unknown; consult the linked documentation.")
        lines.append("Dependencies and cross-component relationships are established only by the linked evidence; undocumented relationships remain unknown.")
    if not discovery["projects"]:
        lines += ["", "Purpose, architecture, entry points and commands are unknown; no supported documentation/manifests were discovered."]
    lines += ["", "## Discovery limits", "Only recognized local manifests/docs were read, within depth, entry, project and file-size limits. External symlinks, credential stores and project hooks were not read or run."]
    coverage = discovery["coverage"]
    counts, caps = coverage["observed"], coverage["caps"]
    lines += [f"Scan scope: selected source (`.`), at most depth {caps['depth']}; recognized manifests, Markdown and nested AGENTS.md only.",
              (f"Coverage: {coverage['status']}. Visited {counts['directories_visited']} directories; "
              f"admitted {counts['components_admitted']} of {counts['components_observed']} observed components, "
              f"{counts['instruction_scopes']} nested instruction scopes and {counts['documents_read']} documents."),
              "Undiscovered instruction scopes remain unknown; observed omissions are not totals for unexplored subtrees."]
    totals = {}
    for item in coverage["omissions"]:
        totals[item["code"]] = totals.get(item["code"], 0) + item["observed"]
    lines += [f"- {code}: {count} observed omission(s); further coverage unknown." for code, count in sorted(totals.items())]
    if coverage["enumeration_order_dependent"]:
        lines.append("Overfull directories use an enumeration-order-dependent entry sample; later entries may contain important evidence.")
    if not coverage["complete"]:
        lines.append("Review evidence and nested scopes before use. For a focused guide, initialize a selected component separately. "
                     "Existing guides are preserved; replacement requires an explicit manual backup, review and removal before initialization.")
    return ("\n".join(lines) + "\n").encode()


def _editor(root: Path, discovery: dict, selected: tuple[str, ...]) -> tuple[dict, dict]:
    artifacts, omitted = {}, {}
    tasks, recommendations, launches = [], set(), []
    for project in discovery["projects"]:
        directory = project["directory"]
        for item in _commands(project):
            working_directory = item["directory"]
            tasks.append({"label": f"{working_directory}: {item['command']}", "type": "shell", "command": item["command"],
                          "options": {"cwd": "${workspaceFolder}" + ("/" + working_directory if working_directory != "." else "")}, "problemMatcher": []})
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
    return {"editor": editor, "artifacts": outcomes, "omitted": omitted, "warnings": discovery["warnings"],
            "discovery": discovery["coverage"]}
