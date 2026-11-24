"""Explicit composition capture and whole-child entry; never unload or migrate."""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import os
import secrets
import shutil
import stat
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .catalog import Catalog, corroborate_logical_snapshot
from .composition import (
    MANIFEST,
    parse_manifest,
    portable_path,
    require_uncomposed_workspace,
    validate_child_git,
    validate_paths,
    validate_payload_path,
)
from .composition_authority import (
    CompositionAuthority,
    dimension_binding,
    namespace_prefix,
)
from .composition_git_integrity import verify_staged_git
from .composition_safety import CompositionSafetyState, _identifier, plan_digest
from .composition_transaction import (
    _require_extension,
    canonical,
    manifest_for,
    prepare_parent,
    record_child,
    record_parent,
    reserve,
)
from .logical_jat import LogicalJat
from .room_store_operations import (
    RoomStoreOperations,
    _scan_workspace,
    _validate_snapshot_entries,
    scan_workspace_for_status,
)
from .workspace_onboarding import _directory
from .workspace_policy import (
    WorkspaceIgnoreRuleLimitError,
    _read_ignore,
    load_capture_policy,
    policy_for_ignore,
)
from .workspace_state import context_status, write_stat_workspace_marker


def _outcome(path, assignments):
    matches = [
        (0 if row["path"] == "." else len(Path(row["path"]).parts),
         len(row["path"]), row["outcome"])
        for row in assignments
        if row["path"] == "."
        or path == row["path"]
        or path.startswith(row["path"] + "/")
    ]
    if not matches:
        raise ValueError("every captured path needs an explicit subtree assignment")
    return max(matches)[-1]


def _mount_id(fd):
    if not sys.platform.startswith("linux"):
        raise ValueError("mount-aware catalog admission is unavailable")
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise ValueError("mount identity unavailable")


def _catalog_directory_names(root_fd):
    aliases = {}
    directories = set()
    for name in os.listdir(root_fd):
        key = unicodedata.normalize("NFC", name).casefold()
        if key in aliases and aliases[key] != name:
            raise ValueError("workspace has immediate sibling aliases")
        aliases[key] = name
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            directories.add(name)
    return aliases, directories


def _catalog_root_scan(policy, root_fd, children):
    """Inspect only immediate root entries; selected children are scanned separately."""
    from .room_store_operations import (
        RoomStoreOperationsError,
        WorkspaceScan,
        _relative_parts,
    )

    effective = replace(policy, capture_mode="source-only")
    root_device = os.fstat(root_fd).st_dev
    aliases, _directories = _catalog_directory_names(root_fd)
    names = sorted(aliases.values())
    if ({"HEAD", "config", "objects"} <= set(names)
            and any(effective.is_excluded(name) for name in ("HEAD", "config", "objects"))):
        raise ValueError("Bare Git metadata is excluded; include the complete repository before saving")
    records = []
    paths = set()
    logical_bytes = 0
    for name in names:
        if effective.is_excluded(name):
            continue
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        try:
            _relative_parts(name)
            name.encode("utf-8")
        except (RoomStoreOperationsError, UnicodeEncodeError):
            if stat.S_ISDIR(info.st_mode) and name not in children:
                continue
            raise ValueError("workspace path cannot be restored safely") from None
        if name == ".josh-roomignore":
            continue
        if info.st_dev != root_device:
            raise ValueError("workspace crosses a filesystem boundary")
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            records.append(f"{name}\0dir\0{mode}".encode())
        elif stat.S_ISREG(info.st_mode):
            change_time = info.st_ctime_ns
            records.append(
                f"{name}\0file\0{info.st_size}\0{info.st_mtime_ns}\0{change_time}\0{mode}".encode()
            )
            logical_bytes += info.st_size
        elif stat.S_ISLNK(info.st_mode):
            raise ValueError("linked capture sources are unsupported in catalog mode")
        else:
            raise ValueError("workspace contains a special file")
        paths.add(name)
    signature = hashlib.sha256(b"\n".join(records)).hexdigest()
    return WorkspaceScan(
        signature, "josh-room-stat-v1", policy.sha256, frozenset(paths), logical_bytes
    )


@contextmanager
def _catalog_admission(root_fd, children, expected_mount=None):
    root_mount = _mount_id(root_fd)
    if expected_mount is not None and root_mount != expected_mount:
        raise ValueError("approved source mount changed")
    aliases, _directories = _catalog_directory_names(root_fd)
    descriptors = {}
    try:
        for child in children:
            parts = child.split("/")
            if len(parts) != 1 or parts[0] not in aliases.values():
                raise ValueError("selected Catalog child is not an immediate sibling")
            portable_path(child)
            info = os.stat(child, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("selected Catalog child must be a direct directory")
            fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            descriptors[child] = fd
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("selected Catalog child changed during admission")
            if _mount_id(fd) != root_mount:
                raise ValueError("selected Catalog child crosses a mount boundary")
        yield root_mount, descriptors
    finally:
        try:
            if _mount_id(root_fd) != root_mount:
                raise ValueError("approved source mount changed during Catalog admission")
            for child, fd in descriptors.items():
                current = os.stat(child, dir_fd=root_fd, follow_symlinks=False)
                opened = os.fstat(fd)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("selected Catalog child changed during admission")
                if _mount_id(fd) != root_mount:
                    raise ValueError("selected Catalog child crosses a mount boundary")
        finally:
            for fd in descriptors.values():
                os.close(fd)


def _parent_scan(source, policy, children):
    return _scan_workspace(source, replace(policy, capture_mode="source-only"),
                           opaque_directories=frozenset(children))


def _has_git(directory):
    return os.path.lexists(directory / ".git") or (
        (directory / "HEAD").is_file() and (directory / "objects").is_dir()
        and (directory / "config").is_file()
    )


def _child_inventory(source, child, root_policy, selection):
    original = source / child
    raw = _read_ignore(original)
    local = policy_for_ignore(raw)
    mode = selection or local.capture_mode
    if "source-only" in (local.capture_mode, root_policy.capture_mode):
        if selection == "full-repository" or (
            root_policy.capture_mode == "source-only" and selection is None
            and local.capture_mode == "full-repository"
            and any(line.strip().startswith(b"# josh-room capture-mode:") for line in (raw or b"").splitlines())
        ):
            raise ValueError("full repository selection conflicts with source-only policy")
        mode = "source-only"
    lines = [line for line in (raw or b"").decode("utf-8").splitlines()
             if not line.strip().startswith("# josh-room capture-mode:")]
    lines.append(f"# josh-room capture-mode: {mode}")

    def generated_policy(*, compact):
        rules = [".josh-room-split.json", *_inherited_exclusions(root_policy, child, compact=compact)]
        escaped = ["\\" + rule if rule.startswith("#") else rule for rule in rules]
        return ("\n".join([*lines, *escaped]) + "\n").encode("utf-8")

    generated = generated_policy(compact=False)
    try:
        effective = policy_for_ignore(generated)
    except WorkspaceIgnoreRuleLimitError:
        # Keep the exact policy bytes of plans that already fit. On overflow,
        # omit only residual states already covered by a retained ** transition.
        generated = generated_policy(compact=True)
        try:
            effective = policy_for_ignore(generated)
        except WorkspaceIgnoreRuleLimitError:
            raise CompositionAdmissionError(
                "composition-child-ignore-rule-limit",
                "The parent and child capture policies combine to more than 128 rules. "
                "Consolidate the .josh-roomignore rules or choose a different folder.",
            ) from None
    if effective.is_excluded(".josh-roomignore"):
        raise ValueError("the generated child policy must include .josh-roomignore")
    scan = _scan_workspace(original, effective)
    for path in scan.paths:
        if path != ".josh-roomignore":
            try:
                validate_payload_path(path)
            except ValueError as error:
                if str(error) != "reserved composition payload path":
                    raise
                raise CompositionAdmissionError(
                    "composition-child-room-metadata",
                    "A selected folder contains reserved Room metadata. Leave that folder local to continue.",
                    child=child,
                ) from None
    try:
        validate_child_git(original, scan.paths, mode)
    except ValueError as error:
        if str(error) in {
            "bare Catalog children are unsupported; save the repository as one ordinary Room",
            "Catalog child submodules are unsupported; save the repository as one ordinary Room",
            "Catalog child submodules and gitfiles are unsupported; save the repository as one ordinary Room",
            "Catalog child submodules and linked worktrees are unsupported",
            "Git lock present; finish the Git operation before saving",
        }:
            raise CompositionAdmissionError("composition-child-git-unsupported", str(error)) from None
        if str(error) in {
            "Catalog capture requires complete Git history; partial and shallow clones are unsupported",
            "Catalog capture requires complete Git history; partial clones are unsupported",
            "Catalog capture cannot verify this Git repository extension",
            "Catalog children with submodule gitlinks or sparse directory indexes are unsupported",
        }:
            raise CompositionAdmissionError("composition-child-git-files-only", str(error), child=child) from None
        raise
    evidence = {
        "signature": scan.signature,
        "policy_sha256": local.sha256,
        "effective_policy_sha256": effective.sha256,
        "approved_paths_sha256": plan_digest(sorted(scan.paths | {".josh-roomignore"})),
        "capture_mode": mode,
        "records_sha256": plan_digest(_records(original, scan.paths)),
    }
    return scan, evidence, generated


def capture_plan(workspace, dimension, namespace, parent_room, assignments, *, root_files_only=False):
    if not sys.platform.startswith("linux"):
        raise ValueError("catalog capture is not yet qualified on this platform")
    namespace_prefix(namespace)
    _identifier(parent_room)
    workspace = Path(os.path.abspath(workspace))
    descriptor = _directory(workspace)
    source_identity = [os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino]
    source_mount_id = _mount_id(descriptor) if root_files_only else None
    os.close(descriptor)
    require_uncomposed_workspace(workspace)
    if (workspace / ".josh-room.json").exists():
        raise ValueError(
            "existing Room conversion is unsupported; use an unbound source"
        )
    assignment_limit = 32 if root_files_only else 64
    if not isinstance(assignments, list) or not assignments or len(assignments) > assignment_limit:
        raise ValueError("invalid capture assignments")
    rows = []
    component_options = {}
    for row in assignments:
        if (
            not isinstance(row, dict)
            or not {"path", "outcome"} <= set(row) <= {"path", "outcome", "capture_mode", "component_options"}
            or ("component_options" in row and (row["outcome"] != "child" or not isinstance(row["component_options"], dict)))
            or ("capture_mode" in row and (row["outcome"] != "child"
                or row["capture_mode"] not in ("full-repository", "source-only")))
            or row["outcome"] not in ("child", "parent", "local-only")
        ):
            raise ValueError("invalid capture assignment")
        path = row["path"]
        if path != ".":
            portable_path(path)
            fd = (
                _directory(workspace / path)
                if (workspace / path).is_dir()
                else _directory((workspace / path).parent)
            )
            os.close(fd)
            if not (workspace / path).exists() or (workspace / path).is_symlink():
                raise ValueError("capture assignment unavailable")
        elif row["outcome"] == "child":
            raise ValueError("the source root cannot be its own child")
        if "component_options" in row:
            component_options[path] = row["component_options"]
        rows.append({key: value for key, value in row.items() if key != "component_options"})
    if len({row["path"] for row in rows}) != len(rows):
        raise ValueError("duplicate capture assignment")
    validate_paths(row["path"] for row in rows if row["path"] != ".")
    children = [row["path"] for row in rows if row["outcome"] == "child"]
    if not 1 <= len(children) <= 32:
        raise ValueError(
            "v1 publication requires one to 32 children; discovery is not limited"
        )
    validate_paths(children)
    if root_files_only and (
        len(children) != len(rows)
        or any("/" in row["path"] or row["outcome"] != "child" for row in rows)
    ):
        raise ValueError("root-files-only mode accepts selected immediate child directories only")
    for child in children:
        require_uncomposed_workspace(workspace / child)
        try:
            (workspace / child / ".josh-room.json").lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("existing child Room conversion is unsupported")
        if any(row["path"].startswith(child + "/") for row in rows):
            raise ValueError("whole-child capture cannot contain assignment overrides")
        if not (workspace / child).is_dir():
            raise ValueError("child assignment must select a directory")
    policy = load_capture_policy(workspace)
    # A parent is deliberately source-only. A cohesive Git source needs an
    # explicit source-only policy before its history can be left behind.
    if policy.capture_mode != "source-only" and (_has_git(workspace) or any(
        (directory / ".git").is_file() or (directory / ".git/HEAD").is_file()
        for directory in workspace.parents
    )):
        raise CompositionAdmissionError(
            "composition-root-git-history",
            "The Catalog source is part of a Git repository. Save it as one Room to preserve Git history, "
            "or choose a source folder outside the repository for a Catalog.",
        )
    opaque_directories = frozenset(children)
    if root_files_only:
        root_fd = _directory(workspace)
        try:
            if ([os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino] != source_identity
                    or _mount_id(root_fd) != source_mount_id):
                raise ValueError("approved source identity changed")
            _aliases, directories = _catalog_directory_names(root_fd)
            opaque_directories = frozenset(directories)
            scan = _catalog_root_scan(policy, root_fd, children)
        finally:
            os.close(root_fd)
    else:
        scan = _parent_scan(workspace, policy, children)
    for path in scan.paths:
        if path == ".josh-roomignore":
            continue
        validate_payload_path(path)
        location = workspace / path
        if location.is_symlink():
            raise ValueError("linked capture sources are unsupported in catalog mode")
        if root_files_only:
            if "/" not in path and location.is_dir() and path in children:
                outcome = "child"
            elif "/" not in path and location.is_file():
                continue
            else:
                continue
        else:
            outcome = _outcome(path, rows)
        if (policy.capture_mode != "source-only" and path not in children
                and location.is_dir() and _has_git(location)
                and (outcome == "parent" or any(
                    row["outcome"] != "local-only" and row["path"].startswith(path + "/")
                    for row in rows))):
            raise ValueError("save the cohesive Git repository as one Room or assign its entire directory to a child")
    if any(child not in scan.paths or policy.is_excluded(child) for child in children):
        raise ValueError("selected child is excluded or unavailable")
    rows.sort(key=lambda row: row["path"])
    if not dimension.encryption_domain_id:
        raise ValueError("select an initialized encryption domain before planning")
    child_evidence = {}
    if root_files_only:
        root_fd = _directory(workspace)
        try:
            if ([os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino] != source_identity
                    or _mount_id(root_fd) != source_mount_id):
                raise ValueError("approved source identity changed")
            with _catalog_admission(root_fd, children, source_mount_id):
                for row in rows:
                    child_scan, evidence, _ = _child_inventory(workspace, row["path"], policy, row.get("capture_mode"))
                    child = row["path"]
                    if child in component_options:
                        from .composition_components import component_plan

                        evidence["component_plan"] = component_plan(
                            workspace / child, dimension, "child-" + _slot(namespace, child),
                            evidence["effective_policy_sha256"], component_options[child], admitted_paths=child_scan.paths,
                        )
                    child_evidence[child] = evidence
        finally:
            os.close(root_fd)
    else:
        for row in rows:
            if row["outcome"] == "child":
                child_scan, evidence, _ = _child_inventory(workspace, row["path"], policy, row.get("capture_mode"))
                child = row["path"]
                if child in component_options:
                    from .composition_components import component_plan

                    evidence["component_plan"] = component_plan(
                        workspace / child, dimension, "child-" + _slot(namespace, child),
                        evidence["effective_policy_sha256"], component_options[child], admitted_paths=child_scan.paths,
                    )
                child_evidence[child] = evidence
    if root_files_only:
        root_fd = _directory(workspace)
        try:
            if ([os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino] != source_identity
                    or _mount_id(root_fd) != source_mount_id):
                raise ValueError("approved source identity changed")
            if _catalog_root_scan(policy, root_fd, children).signature != scan.signature:
                raise ValueError("Catalog root inventory changed during planning")
            source_records = _records(workspace, scan.paths, root_fd)
        finally:
            os.close(root_fd)
    else:
        source_records = _records(workspace, scan.paths)
    require_uncomposed_workspace(workspace)
    if load_capture_policy(workspace).sha256 != policy.sha256:
        raise ValueError("capture policy changed during planning")
    for child in children:
        require_uncomposed_workspace(workspace / child)
    request = {
        "plan_version": 4 if component_options else 3,
        "namespace": namespace,
        "source_identity": source_identity,
        **({"source_mount_id": source_mount_id} if root_files_only else {}),
        "source_records_sha256": plan_digest(source_records),
        "dimension_id": dimension.dimension_id,
        "destination_binding": dimension_binding(
            dimension, dimension.encryption_domain_id
        ),
        "child_evidence": child_evidence,
        "source_path_sha256": hashlib.sha256(str(workspace).encode()).hexdigest(),
        "source_signature": scan.signature,
        "capture_policy_sha256": policy.sha256,
        "parent_room_id": parent_room,
        "capture_mode": "source-only",
        "assignments": rows,
        **({"root_files_only": True} if root_files_only else {}),
    }
    return {
        "ok": True,
        "status": "approval-required",
        "can_apply": False,
        "request": request,
        "approval_sha256": plan_digest(request),
        "child_count": len(children),
        "local_only_acknowledgement_required": (
            bool(set(opaque_directories) - set(children))
            if root_files_only else
            any(row["outcome"] == "local-only" for row in rows)
        ),
    }


def _slot(namespace, label):
    return uuid.uuid5(uuid.UUID(hex=namespace), label).hex


def _record_stat(info):
    return [
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def _records(source, paths, root_fd=None):
    fd = _directory(source) if root_fd is None else os.dup(root_fd)
    try:
        result = {".": _record_stat(os.fstat(fd))}
        for path in paths:
            current = os.dup(fd)
            try:
                parts = Path(path).parts
                for part in parts[:-1]:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current,
                    )
                    os.close(current)
                    current = child
                result[path] = _record_stat(
                    os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
                )
            finally:
                os.close(current)
        return result
    finally:
        os.close(fd)


@contextmanager
def _admitted_source(source, request):
    fd = _directory(source)
    try:
        if [os.fstat(fd).st_dev, os.fstat(fd).st_ino] != request["source_identity"]:
            raise ValueError("approved source identity changed")
        if request.get("root_files_only") and _mount_id(fd) != request["source_mount_id"]:
            raise ValueError("approved source mount changed")
        yield fd
    finally:
        os.close(fd)


class CompositionAdmissionError(ValueError):
    def __init__(self, code, message, *, child=None):
        super().__init__(message)
        self.result = {"error_code": code}
        if child is not None:
            self.result["child_path_sha256"] = hashlib.sha256(child.encode("utf-8")).hexdigest()


def resolve_capture_destination(dimension, selected_material, *, expected_domain=None):
    """Authenticate only the selected bucket. No namespace or Restic access."""
    from .encryption_domain import validate_encryption_domain_id
    from .room_store_bridge import RoomStoreBridgeError, _resolve_material, _scope

    if expected_domain is not None:
        try:
            validate_encryption_domain_id(expected_domain)
        except (TypeError, ValueError):
            raise CompositionAdmissionError(
                "encryption-domain-invalid", "--expected-domain requires a canonical UUID4"
            ) from None
    try:
        material, session = _resolve_material(dimension, selected_material, None, allow_initialize=False)
        domain = validate_encryption_domain_id(_scope(dimension, material))
    except RoomStoreBridgeError as error:
        if error.code == "domain-mismatch":
            raise CompositionAdmissionError("encryption-domain-mismatch",
                "The configured Dimension domain differs from its enrolled keyset") from None
        raise CompositionAdmissionError(error.result.get("error_code", error.code), str(error)) from None
    if expected_domain is not None and expected_domain != domain:
        raise CompositionAdmissionError("encryption-domain-mismatch",
            "The asserted domain differs from the selected Dimension's enrolled domain")
    if not set(material.keyset.recovery_recipients) - {material.recipient}:
        raise CompositionAdmissionError("encryption-recovery-recipient-required",
            "Catalog capture requires a distinct enrolled recovery recipient")
    return replace(dimension, encryption_domain_id=domain), material, session


@contextmanager
def provider_runtime(instance, dimension, material, namespace, *, authority_session=None, authority_only=False):
    from .room_store_bridge import (
        _create_backend,
        _private_operation_directory,
        _provider_environment,
        _repository_locator,
        _resolve_material,
        _restic_store_factory,
        _s3_lookup_option,
        _scope,
        _verified_restic_executable,
    )

    material, _session = _resolve_material(
        dimension, material, authority_session, allow_initialize=False
    )
    _scope(dimension, material)
    # Validate bucket, TLS and credentials using the existing provider boundary.
    legacy = _repository_locator(dimension)
    root = legacy.rsplit("/room-store/v1", 1)[0]
    repository = root + "/" + namespace_prefix(namespace) + "payload-restic-v1"
    with _private_operation_directory() as private:
        authority = CompositionAuthority(
            _create_backend(dimension, instance),
            dimension,
            namespace,
            material,
            private,
        )
        if authority_only:
            yield authority, None, private
            return
        password = private / "password"
        cache = private / "cache"
        cache.mkdir(mode=0o700)
        executable = _verified_restic_executable(
            runtime_root=Path(instance).parent / "josh-room-runtime", install=True
        )

        def store_factory(secret):
            password.write_text(secret + "\n")
            password.chmod(0o600)
            return _restic_store_factory(
                repository=repository,
                cache_dir=cache,
                password_file=password,
                provider_env=_provider_environment(dimension),
                executable=executable,
                ca_bundle=dimension.option("ca_bundle"),
                **_s3_lookup_option(dimension),
            )

        try:
            yield authority, store_factory, private
        finally:
            password.unlink(missing_ok=True)


def _commit(authority, envelope, etag, state):
    _require_extension(envelope["state"], state.to_dict())
    updated = copy.deepcopy(envelope)
    updated["state"] = state.to_dict()
    etag = authority.put(updated, etag)
    return updated, etag


CONTENT_DIGEST_ALGORITHM = "sha256-framed-v2"
INHERITED_POLICY = "inherited-rules-v2"


def _content_digest(root):
    scan = scan_workspace_for_status(root)
    digest = hashlib.sha256(CONTENT_DIGEST_ALGORITHM.encode() + b"\0")
    for relative in sorted(scan.paths):
        path = root / relative
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(path))
            header = canonical([relative, "symlink", stat.S_IMODE(info.st_mode), len(target)])
            digest.update(len(header).to_bytes(8, "big") + header + target)
            continue
        kind = "file" if stat.S_ISREG(info.st_mode) else "directory"
        header = canonical([relative, kind, stat.S_IMODE(info.st_mode),
                            info.st_size if kind == "file" else 0])
        digest.update(len(header).to_bytes(8, "big") + header)
        if stat.S_ISREG(info.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _record_stat(opened) != _record_stat(info):
                    raise ValueError("capture digest file changed while opening")
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                if size != info.st_size or _record_stat(os.fstat(stream.fileno())) != _record_stat(info):
                    raise ValueError("capture digest file changed while reading")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("linked capture payload is unsupported")
    return digest.hexdigest()


def _capture(root, store, private, slot, dimension, domain, *, claim, parent=None, inherited_policy=None,
             component_options=None, component_workspace=None, component_claim=None):
    from .room_store_bridge import _room_version, _source_platform
    producer_version, producer_platform = _room_version(), _source_platform()
    policy = load_capture_policy(root)
    before = scan_workspace_for_status(root)
    verify_staged_git(root, before.paths, policy.capture_mode, private)
    tag = ("composition:" + slot["logical_jat_id"],)
    prior = store.snapshots(tags=tag)
    if len(prior) > 1:
        raise ValueError("ambiguous capture inventory; no snapshot was deleted")
    excludes = private / "excludes"
    excludes.write_text("\n".join(policy.resolved_restic_excludes(root)) + "\n")
    if prior:
        snapshot = store.snapshot(prior[0].snapshot_id)
        summary = store.snapshot_backup_summary(snapshot.snapshot_id)
        proof = private / ("capture-reconcile-" + uuid.uuid4().hex)
        _validate_snapshot_entries(store.entries(snapshot.snapshot_id))
        store.restore(snapshot.snapshot_id, proof)
        if _content_digest(proof) != _content_digest(root):
            raise ValueError(
                "interrupted capture differs from approved payload; preserve namespace"
            )
    else:
        claim()
        summary = store.backup(root, parent=parent.to_dict()['workspace']['snapshot_id'] if parent else None,
                               excludes=excludes, tags=tag)
        snapshot = store.snapshot(summary.snapshot_id)
    expected_parent = parent.to_dict()['workspace']['snapshot_id'] if parent else None
    if snapshot.parent_snapshot_id != expected_parent:
        raise ValueError("captured snapshot ancestry differs from selected baseline")
    entries = _validate_snapshot_entries(store.entries(snapshot.snapshot_id))
    after = scan_workspace_for_status(root)
    if (
        before.signature != after.signature
        or before.capture_policy_sha256 != after.capture_policy_sha256
    ):
        raise ValueError(
            "source changed during capture; retained snapshot requires reconciliation"
        )
    if set(entries) - {"."} != set(before.paths):
        raise ValueError("captured inventory differs from validated source")
    value = {
        "format_version": 1,
        "payload_kind": "room-store-v1",
        "logical_jat_id": slot["logical_jat_id"],
        "dimension_id": dimension.dimension_id,
        "encryption_domain_id": domain,
        "room_id": slot["room_id"],
        "created_at": snapshot.time,
        "capture_policy_sha256": policy.sha256,
        "capture_mode": policy.capture_mode,
        "workspace": {
            "engine": "restic",
            "repository_id": store.open_existing().repository_id,
            "repository_format": 2,
            "snapshot_id": snapshot.snapshot_id,
            "tree_id": snapshot.tree_id,
            "source_path": ".",
            "logical_bytes": before.logical_bytes,
            "data_added": summary.data_added,
            "data_added_packed": summary.data_added_packed,
        },
        "components": {
            "rcc_environment": None,
            "homebrew_recovery": None,
            "hauler_content": None,
        },
        "source": {},
        "producer": {
            "josh_room_version": producer_version,
            "restic_version": "0.19.1",
            "source_platform": producer_platform,
            "restore_platforms": [producer_platform],
        },
    }
    if parent:
        value.update(parent_logical_jat_id=parent.to_dict()["logical_jat_id"])
        value["workspace"]["parent_snapshot_id"] = parent.to_dict()["workspace"][
            "snapshot_id"
        ]
    if inherited_policy is not None:
        if inherited_policy != INHERITED_POLICY:
            raise ValueError("unsupported inherited policy provenance")
        value["source"]["composition_policy"] = inherited_policy
    if "component_plan" in slot or component_options is not None:
        from .composition_components import capture_components
        value["components"] = capture_components(component_workspace, root, dimension, slot, policy.sha256,
                                                 component_options, parent, store, component_claim, private)
    descriptor = LogicalJat.from_dict(value)
    from .composition_component_contract import (
        validate_component_contract,
        verify_component_snapshots,
    )
    validate_component_contract(slot, descriptor, parent)
    verify_component_snapshots(store, descriptor, private)
    return descriptor, before


def _copy_paths(source, target, paths, *, source_fd=None, records=None):
    """Read every payload through no-follow descriptors rooted at the source."""
    root_fd = _directory(source) if source_fd is None else os.dup(source_fd)
    modes = []
    try:
        records = _records(source, paths, root_fd) if records is None else records
        policy = load_capture_policy(source, directory_fd=root_fd)
        ignore = _read_ignore(source, directory_fd=root_fd) or b""
        for relative in sorted(
            paths, key=lambda value: (len(Path(value).parts), value)
        ):
            if relative == ".josh-roomignore":
                continue
            parts = Path(relative).parts
            fd = os.dup(root_fd)
            try:
                for index, part in enumerate(parts[:-1]):
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    expected = records.get("/".join(parts[: index + 1]))
                    if expected is None or _record_stat(os.fstat(child)) != expected:
                        os.close(child)
                        raise ValueError("approved ancestor identity changed")
                    os.close(fd)
                    fd = child
                info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                if records.get(relative) != _record_stat(info):
                    raise ValueError("approved payload metadata changed")
                if info.st_dev != os.fstat(root_fd).st_dev:
                    raise ValueError("capture crossed a filesystem boundary")
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if stat.S_ISDIR(info.st_mode):
                    destination.mkdir(exist_ok=True, mode=0o700)
                    modes.append((destination, stat.S_IMODE(info.st_mode)))
                elif stat.S_ISLNK(info.st_mode):
                    link = os.readlink(parts[-1], dir_fd=fd)
                    if _record_stat(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)) != records[relative]:
                        raise ValueError("approved symlink changed during capture")
                    destination.symlink_to(link)
                elif stat.S_ISREG(info.st_mode):
                    opened = os.open(
                        parts[-1],
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=fd,
                    )
                    with (
                        os.fdopen(opened, "rb") as stream,
                        destination.open("xb") as output,
                    ):
                        current = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(current.st_mode)
                            or _record_stat(current) != records[relative]
                        ):
                            raise ValueError("capture file identity changed")
                        shutil.copyfileobj(stream, output)
                        final = os.fstat(stream.fileno())
                        if (
                            final.st_size,
                            final.st_mtime_ns,
                            final.st_ctime_ns,
                            final.st_mode,
                        ) != (
                            current.st_size,
                            current.st_mtime_ns,
                            current.st_ctime_ns,
                            current.st_mode,
                        ):
                            raise ValueError("capture file changed while reading")
                    destination.chmod(stat.S_IMODE(info.st_mode))
                    os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))
                else:
                    raise ValueError("linked or special capture entry is unsupported")
            finally:
                os.close(fd)
        if load_capture_policy(source, directory_fd=root_fd).sha256 != policy.sha256:
            raise ValueError("capture policy changed while staging")
        return ignore.decode("utf-8"), modes
    finally:
        os.close(root_fd)


def _finish_view(target, expected_paths, modes):
    for directory, mode in reversed(modes):
        directory.chmod(mode)
    scan = scan_workspace_for_status(target)
    if set(scan.paths) != set(expected_paths):
        raise ValueError("private capture view differs from approved inventory")


def _child_view(source, child, target, request):
    with _admitted_source(source, request) as fd:
        return _build_child_view(source, child, target, request, fd)


def _inherited_exclusions(policy, child, *, compact=False):
    """Rebase the exclusion language, including matches not present on disk."""
    if policy.is_excluded(child):
        raise ValueError("selected child is excluded by parent policy")
    values = []
    for pattern in policy.patterns:
        if len(pattern) == 1:
            values.append("/".join(pattern))
            continue

        def closure(states, pattern=pattern):
            states = set(states)
            for index in range(len(pattern)):
                if index in states and pattern[index] == "**":
                    states.add(index + 1)
            return states

        states = closure({0})
        for part in Path(child).parts:
            states = closure({index if pattern[index] == "**" else index + 1
                for index in states if index < len(pattern)
                and (pattern[index] == "**" or fnmatch.fnmatchcase(part, pattern[index]))})
        for index in sorted(states):
            if compact and index > 0 and index - 1 in states and pattern[index - 1] == "**":
                continue
            suffix = pattern[index:]
            if not suffix or suffix == ("**",):
                raise ValueError("parent exclusion covers the whole selected child")
            # Single-component rules match anywhere; anchor a residual root
            # component by appending **, which also preserves subtree pruning.
            if len(suffix) == 1:
                suffix += ("**",)
            values.append("/".join(suffix))
    return tuple(dict.fromkeys(values))


def _build_child_view(source, child, target, request, source_fd):
    require_uncomposed_workspace(source)
    require_uncomposed_workspace(source / child)
    root_policy = load_capture_policy(source)
    if request.get("root_files_only"):
        with _catalog_admission(source_fd, [child], request["source_mount_id"]):
            root_scan = _catalog_root_scan(root_policy, source_fd, request["child_evidence"])
    else:
        root_scan = _parent_scan(source, root_policy, request["child_evidence"])
    original = source / child
    row = next(row for row in request["assignments"] if row["path"] == child)
    if request.get("root_files_only"):
        with _catalog_admission(source_fd, [child], request["source_mount_id"]):
            child_scan, evidence, generated = _child_inventory(source, child, root_policy, row.get("capture_mode"))
    else:
        child_scan, evidence, generated = _child_inventory(source, child, root_policy, row.get("capture_mode"))
    allowed = child_scan.paths
    if (
        evidence != {key: value for key, value in request["child_evidence"][child].items() if key != "component_plan"}
        or root_scan.signature != request["source_signature"]
        or root_policy.sha256 != request["capture_policy_sha256"]
    ):
        raise ValueError("approved child inventory changed before capture")
    target.mkdir(mode=0o700)
    root_records = _records(source, root_scan.paths, source_fd)
    if plan_digest(root_records) != request["source_records_sha256"]:
        raise ValueError("approved source records changed before staging")
    child_fd = os.dup(source_fd)
    try:
        for index, part in enumerate(Path(child).parts):
            opened = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=child_fd
            )
            os.close(child_fd)
            child_fd = opened
            if (
                _record_stat(os.fstat(child_fd))
                != root_records["/".join(Path(child).parts[: index + 1])]
            ):
                raise ValueError("approved child directory changed")
        child_records = _records(original, child_scan.paths, child_fd)
        if plan_digest(child_records) != evidence["records_sha256"]:
            raise ValueError("approved child records changed before staging")
        if request.get("root_files_only"):
            with _catalog_admission(source_fd, [child], request["source_mount_id"]):
                _, modes = _copy_paths(
                    original, target, allowed, source_fd=child_fd, records=child_records
                )
        else:
            _, modes = _copy_paths(
                original, target, allowed, source_fd=child_fd, records=child_records
            )
    finally:
        os.close(child_fd)
    (target / ".josh-roomignore").write_bytes(generated)
    _finish_view(target, allowed | {".josh-roomignore"}, modes)
    if request.get("root_files_only"):
        with _catalog_admission(source_fd, [child], request["source_mount_id"]):
            _, after, _ = _child_inventory(source, child, root_policy, row.get("capture_mode"))
    else:
        _, after, _ = _child_inventory(source, child, root_policy, row.get("capture_mode"))
    if after != evidence:
        raise ValueError("child source changed during private staging")
    require_uncomposed_workspace(source)
    require_uncomposed_workspace(source / child)
    return target


def _parent_view(source, target, assignments, manifest, request):
    with _admitted_source(source, request) as fd:
        return _build_parent_view(source, target, assignments, manifest, request, fd)


def _build_parent_view(source, target, assignments, manifest, request, source_fd):
    target.mkdir(mode=0o700)
    require_uncomposed_workspace(source)
    policy = load_capture_policy(source)
    if request.get("root_files_only"):
        children = [row["path"] for row in assignments]
        with _catalog_admission(source_fd, children, request["source_mount_id"]):
            scan = _catalog_root_scan(policy, source_fd, children)
    else:
        scan = _parent_scan(source, policy, request["child_evidence"])
    if (
        scan.signature != request["source_signature"]
        or policy.sha256 != request["capture_policy_sha256"]
    ):
        raise ValueError("approved parent inventory changed before capture")
    if request.get("root_files_only"):
        source_records = _records(source, scan.paths, source_fd)
        allowed = {
            path for path in scan.paths
            if path != ".josh-roomignore" and "/" not in path
            and stat.S_ISREG(source_records[path][2])
        }
    else:
        allowed = {
            path
            for path in scan.paths
            if path != ".josh-roomignore" and _outcome(path, assignments) == "parent"
        }
    for path in list(allowed) + [row["path"] for row in manifest["children"]]:
        allowed.update(
            parent.as_posix() for parent in Path(path).parents if parent != Path(".")
        )
    source_records = _records(source, scan.paths, source_fd)
    if plan_digest(source_records) != request["source_records_sha256"]:
        raise ValueError("approved parent records changed before staging")
    original, modes = _copy_paths(
        source, target, allowed, source_fd=source_fd, records=source_records
    )
    lines = [
        line
        for line in original.splitlines()
        if not line.strip().startswith("# josh-room capture-mode:")
    ]
    lines.append("# josh-room capture-mode: source-only")
    lines.extend(
        row["path"] + "/**"
        for row in assignments
        if row["outcome"] in ("child", "local-only") and row["path"] != "."
    )
    (target / ".josh-roomignore").write_text("\n".join(lines) + "\n")
    (target / MANIFEST).write_bytes(canonical(manifest))
    _finish_view(target, allowed | {".josh-roomignore", MANIFEST}, modes)
    if request.get("root_files_only"):
        children = [row["path"] for row in assignments]
        with _catalog_admission(source_fd, children, request["source_mount_id"]):
            after = _catalog_root_scan(load_capture_policy(source), source_fd, children)
    else:
        after = _parent_scan(source, load_capture_policy(source), request["child_evidence"])
    if (
        after.signature != request["source_signature"]
        or after.capture_policy_sha256 != request["capture_policy_sha256"]
    ):
        raise ValueError("parent source changed during private staging")
    require_uncomposed_workspace(source)


def save_composition(
    workspace,
    dimension,
    namespace,
    parent_room,
    assignments,
    approval,
    authority,
    store_factory,
    private,
    *,
    root_files_only=False,
):
    if namespace != authority.namespace:
        raise ValueError("requested namespace differs from selected authority")
    workspace = Path(workspace)
    if (
        workspace.resolve() == Path(private).resolve()
        or workspace.resolve() in Path(private).resolve().parents
    ):
        raise ValueError("private runtime must be outside the capture source")
    if root_files_only:
        preview = capture_plan(
            workspace, dimension, namespace, parent_room, assignments,
            root_files_only=True,
        )
    else:
        preview = capture_plan(workspace, dimension, namespace, parent_room, assignments)
    request = preview["request"]
    if approval != preview["approval_sha256"]:
        raise CompositionAdmissionError("composition-approval-stale", "capture approval is stale or missing")
    if authority.binding != request["destination_binding"]:
        raise CompositionAdmissionError("composition-destination-mismatch",
            "The selected physical destination differs from the approved binding")
    envelope, etag = authority.read()
    if envelope is None:
        envelope = {
            "format": "composition-authority-v1",
            "namespace": namespace,
            "binding": authority.binding,
            "request": request,
            "secret": secrets.token_urlsafe(32),
            "repository_id": None,
            "state": None,
            "capture_claims": {},
        }
        etag = authority.put(envelope, None)
    elif canonical(envelope["request"]) != canonical(request):
        raise ValueError("namespace already binds a different capture request")
    if envelope["binding"] != request["destination_binding"]:
        raise ValueError("approved physical destination binding differs")
    with store_factory(envelope["secret"]) as store:
        repository = (
            store.initialize()
            if envelope["repository_id"] is None
            else store.open_existing()
        )
        if (
            envelope["repository_id"] is not None
            and repository.repository_id != envelope["repository_id"]
        ):
            raise ValueError("composition repository binding differs")
        if envelope["state"] is None:
            envelope["repository_id"] = repository.repository_id
            catalog = Catalog.empty(
                dimension.dimension_id, authority.binding["encryption_domain_id"]
            ).body
            catalog["format_version"] = 3
            child_evidence = request["child_evidence"]
            children = [
                {
                    "path": row["path"],
                    "capture_mode": child_evidence[row["path"]]["capture_mode"],
                    "capture_policy_sha256": child_evidence[row["path"]]["effective_policy_sha256"],
                    "approved_paths_sha256": child_evidence[row["path"]]["approved_paths_sha256"],
                    "source_records_sha256": child_evidence[row["path"]]["records_sha256"],
                    **({"component_plan": child_evidence[row["path"]]["component_plan"]}
                       if "component_plan" in child_evidence[row["path"]] else {}),
                    "room_id": "child-" + _slot(namespace, row["path"]),
                    "logical_jat_id": "jat-"
                    + _slot(namespace, "capture:" + row["path"]),
                }
                for row in request["assignments"]
                if row["outcome"] == "child"
            ]
            plan = {
                key: request[key]
                for key in (
                    "source_path_sha256",
                    "capture_policy_sha256",
                    "parent_room_id",
                    "capture_mode",
                )
            }
            plan.update(
                plan_version=request["plan_version"],
                parent_logical_jat_id="jat-" + _slot(namespace, "parent"),
                expected_parent_logical_jat_id=None,
                children=children,
            )
            scope = {
                key: authority.binding[key]
                for key in (
                    "provider",
                    "connection_id",
                    "dimension_id",
                    "encryption_domain_id",
                )
            }
            scope.update(
                repository_id=repository.repository_id, catalog_key="catalog.jroom.age"
            )
            state = reserve(Catalog.from_body(catalog), scope, namespace, plan)
            envelope, etag = _commit(authority, envelope, etag, state)
        state = CompositionSafetyState.from_dict(envelope["state"])
        operation = state.to_dict()["operations"][namespace]
        approved = operation["plan_sha256"]

        def claim(slot):
            nonlocal envelope, etag
            key = slot["logical_jat_id"]
            if key in envelope["capture_claims"]:
                raise ValueError(
                    "capture is claimed without a completed snapshot; reconcile the stopped writer before retry"
                )
            updated = copy.deepcopy(envelope)
            updated["capture_claims"][key] = uuid.uuid4().hex
            etag = authority.put(updated, etag)
            envelope = updated

        for slot in operation["plan"]["children"]:
            if slot["path"] in state.to_dict()["operations"][namespace]["child_pins"]:
                continue
            view = _child_view(
                workspace,
                slot["path"],
                private / ("child-view-" + uuid.uuid4().hex),
                request,
            )
            row = next(row for row in assignments if row["path"] == slot["path"])
            component_args = ({
                "component_options": row["component_options"],
                "component_workspace": workspace / slot["path"],
                "component_claim": lambda name, slot=slot: claim({
                    "logical_jat_id": _slot(namespace, slot["logical_jat_id"] + ":" + name)
                }),
            } if "component_plan" in slot else {})
            descriptor, scan = _capture(
                view,
                store,
                private,
                slot,
                dimension,
                authority.binding["encryption_domain_id"],
                claim=lambda slot=slot: claim(slot),
                inherited_policy=INHERITED_POLICY,
                **component_args,
            )
            if plan_digest(sorted(scan.paths)) != slot["approved_paths_sha256"]:
                raise ValueError("captured inventory differs from the reserved child")
            ref = authority.put_descriptor(descriptor)
            state = record_child(
                state,
                namespace,
                approved,
                slot["path"],
                descriptor=descriptor,
                object_ref=ref,
                workspace_signature=scan.signature,
                display_name="Child Room",
            )
            _, current, _ = _child_inventory(workspace, slot["path"], load_capture_policy(workspace), row.get("capture_mode"))
            if "component_plan" in slot:
                from .composition_components import component_plan

                current["component_plan"] = component_plan(
                    workspace / slot["path"], dimension, slot["room_id"],
                    current["effective_policy_sha256"], row["component_options"], source=view,
                )
            if current != request["child_evidence"][slot["path"]]:
                raise ValueError("child source changed before publication")
            envelope, etag = _commit(authority, envelope, etag, state)
        manifest = manifest_for(state, namespace, approved)
        parent = private / ("parent-view-" + uuid.uuid4().hex)
        _parent_view(workspace, parent, request["assignments"], manifest, request)
        state = prepare_parent(
            state, namespace, approved, policy_sha256=load_capture_policy(parent).sha256
        )
        if canonical(state.to_dict()) != canonical(envelope["state"]):
            envelope, etag = _commit(authority, envelope, etag, state)
        if state.to_dict()["operations"][namespace]["phase"] != "committed":
            slot = {
                "room_id": parent_room,
                "logical_jat_id": operation["plan"]["parent_logical_jat_id"],
            }
            descriptor, scan = _capture(
                parent,
                store,
                private,
                slot,
                dimension,
                authority.binding["encryption_domain_id"],
                claim=lambda: claim(slot),
            )
            # Verify actual encrypted Restic bytes, not merely intended files.
            restored = private / ("parent-proof-" + uuid.uuid4().hex)
            store.restore(descriptor.to_dict()["workspace"]["snapshot_id"], restored)
            if (restored / MANIFEST).read_bytes() != canonical(manifest) or (
                restored / ".josh-roomignore"
            ).read_bytes() != (parent / ".josh-roomignore").read_bytes():
                raise ValueError("saved parent metadata verification failed")
            entries = _validate_snapshot_entries(
                store.entries(descriptor.to_dict()["workspace"]["snapshot_id"])
            )
            if any(
                path == row["path"] or path.startswith(row["path"] + "/")
                for row in manifest["children"]
                for path in entries
            ):
                raise ValueError("saved parent contains child payload")
            if (
                (capture_plan(workspace, dimension, namespace, parent_room, assignments,
                              root_files_only=True) if root_files_only else
                 capture_plan(workspace, dimension, namespace, parent_room, assignments))[
                    "approval_sha256"
                ]
                != approval
            ):
                raise ValueError(
                    "source changed before parent commit; child history retained"
                )
            state = record_parent(
                state,
                namespace,
                approved,
                descriptor=descriptor,
                object_ref=authority.put_descriptor(descriptor),
                workspace_signature=scan.signature,
                display_name="Catalog Room",
            )
            envelope, etag = _commit(authority, envelope, etag, state)
        catalog = Catalog.from_body(state.to_dict()["dimension_catalog"])
        saved_children = []
        for child in manifest["children"]:
            record = catalog.resolve_snapshot(child["room_id"], child["snapshot_id"])
            saved_children.append({
                "path": child["path"], "snapshot_id": child["snapshot_id"],
                "capture_mode": request["child_evidence"][child["path"]]["capture_mode"],
                "components": sorted(name for name, ref in record["component_refs"].items() if ref),
            })
        return {
            "ok": True,
            "status": "saved",
            "namespace": namespace,
            "parent_room_id": parent_room,
            "parent_snapshot_id": operation["plan"]["parent_logical_jat_id"],
            "child_count": len(manifest["children"]),
            "children": saved_children,
            "source_modified": False,
            "retention": "append-only-composition-namespace",
        }


def _verify_descriptor_contract(state, descriptor):
    from .composition_safety import snapshot_capture_contract, verify_snapshot_artifacts

    value = descriptor.to_dict()
    mode, policy_digest = snapshot_capture_contract(state, value["room_id"], value["logical_jat_id"])
    verify_snapshot_artifacts(state, descriptor)
    if value.get("capture_mode") != mode or (
        policy_digest is not None and value["capture_policy_sha256"] != policy_digest
    ):
        raise ValueError("descriptor mode or policy differs from authenticated capture")


def _restore(
    store,
    repository,
    descriptor,
    destination,
    private,
    dimension,
    display_name,
    before_promote=None,
    child_namespace=None,
    namespace=None,
):
    metadata = descriptor.to_dict()
    operations = RoomStoreOperations(
        workspace=Path(destination).parent,
        repository="unused",
        cache_dir=private,
        password_dir=private,
        dimension=dimension,
        backend=None,
        read_latest=lambda: (descriptor, None),
        publish_descriptor=lambda *_a, **_k: None,
        write_marker=lambda *_a, **_k: None,
        descriptor_metadata={
            "dimension_id": dimension.dimension_id,
            "encryption_domain_id": metadata["encryption_domain_id"],
            "room_id": metadata["room_id"],
        },
    )

    def marker(stage, target, selected):
        if before_promote:
            before_promote(stage.parent)
        if child_namespace:
            with (stage / ".josh-room-split.json").open("xb") as guard:
                guard.write(
                    canonical(
                        {
                            "namespace": child_namespace,
                            "room_id": metadata["room_id"],
                            "snapshot_id": metadata["logical_jat_id"],
                        }
                    )
                )
        scan = scan_workspace_for_status(stage)
        if child_namespace:
            validate_child_git(stage.resolve(strict=True), scan.paths, metadata["capture_mode"])
            verify_staged_git(stage, scan.paths, metadata["capture_mode"], private)
        if (load_capture_policy(stage).capture_mode != metadata["capture_mode"]
                or scan.capture_policy_sha256 != metadata["capture_policy_sha256"]):
            raise ValueError("restored policy differs from descriptor")
        write_stat_workspace_marker(
            stage,
            dimension_id=dimension.dimension_id,
            encryption_domain_id=metadata["encryption_domain_id"],
            project_id=metadata["room_id"],
            snapshot_id=metadata["logical_jat_id"],
            display_name=display_name,
            workspace_signature=scan.signature,
            signature_algorithm=scan.signature_algorithm,
            capture_policy_sha256=scan.capture_policy_sha256,
            path_binding=target,
            composition_namespace=namespace or child_namespace,
        )

    def prepare_components(stage, selected, opened):
        from .cli import _jat_root
        from .composition_oci import verify_oci_archive
        from .room_store_bridge import _prepare_restored_components
        _prepare_restored_components(stage, selected, opened, private, _jat_root(), verify_hauler=verify_oci_archive)

    destination = Path(os.path.abspath(destination))
    parent_fd = _directory(destination.parent)
    try:
        operations.restore(
            descriptor,
            destination,
            restic_store=store,
            repository_info=repository,
            write_restore_marker=marker,
            prepare_restored_workspace=prepare_components if any(metadata["components"].values()) else None,
            destination_parent_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    result = context_status(destination)
    scan = scan_workspace_for_status(destination)
    if not result["ok"] or result.get("workspace_signature") != scan.signature:
        raise ValueError("restored marker is unverified; preserve destination")
    return result


def hydrate_composition(
    dimension,
    namespace,
    room,
    snapshot,
    destination,
    authority,
    store_factory,
    private,
    *,
    child_path=None,
):
    if not sys.platform.startswith("linux"):
        raise ValueError("catalog entry is not yet qualified on this platform")
    if namespace != authority.namespace:
        raise ValueError("requested namespace differs from selected authority")
    _identifier(snapshot)
    if snapshot.casefold() == "latest":
        raise ValueError("composition entry requires an immutable parent pin")
    envelope, _etag = authority.read()
    if envelope is None or envelope["state"] is None:
        raise ValueError("composition namespace is unavailable")
    state = CompositionSafetyState.from_dict(envelope["state"])
    body = state.to_dict()
    operation = next(
        (
            op
            for op in body["operations"].values()
            if op["phase"] == "committed"
            and op["parent_pin"]["room_id"] == room
            and op["parent_pin"]["logical_jat_id"] == snapshot
        ),
        None,
    )
    if operation is None:
        raise ValueError("parent pin is not committed in this namespace")
    catalog = Catalog.from_body(body["dimension_catalog"])
    parent_record = catalog.resolve_snapshot(room, snapshot)
    parent_descriptor = authority.descriptor(parent_record)
    corroborate_logical_snapshot(room, parent_record, parent_descriptor)
    _verify_descriptor_contract(state, parent_descriptor)
    with store_factory(envelope["secret"]) as store:
        repository = store.open_existing()
        if repository.repository_id != envelope["repository_id"]:
            raise ValueError("composition repository differs")
        if child_path is None:
            result = _restore(
                store,
                repository,
                parent_descriptor,
                Path(destination),
                private,
                dimension,
                "Catalog Room",
                namespace=namespace,
            )
        else:
            portable_path(child_path)
            root = Path(os.path.abspath(destination))
            fd = _directory(root)
            os.close(fd)
            original = context_status(root)
            if (
                not original["ok"]
                or original.get("composition_namespace") != namespace
                or original.get("project_id") != room
                or original.get("snapshot_id") != snapshot
                or original.get("dimension_id") != dimension.dimension_id
                or original.get("encryption_domain_id")
                != authority.binding["encryption_domain_id"]
            ):
                raise ValueError("parent marker does not match selected immutable pin")

            def parent_current(transient=None):
                current = context_status(root)
                policy = load_capture_policy(root)
                if transient is not None:
                    policy = replace(
                        policy,
                        active_runtime_relative=transient.resolve()
                        .relative_to(root.resolve())
                        .as_posix(),
                    )
                scan = _scan_workspace(root, policy)
                if current != original or scan.signature != original.get(
                    "workspace_signature"
                ):
                    raise ValueError("parent has changed; no child was promoted")

            parent_current()
            # Recover the manifest from the authenticated immutable parent, then
            # compare local bytes. Local metadata alone never grants authority.
            proof = private / ("parent-entry-proof-" + uuid.uuid4().hex)
            parent_workspace = parent_descriptor.to_dict()["workspace"]
            if (
                store.snapshot(parent_workspace["snapshot_id"]).tree_id
                != parent_workspace["tree_id"]
            ):
                raise ValueError("parent snapshot tree differs from descriptor")
            _validate_snapshot_entries(store.entries(parent_workspace["snapshot_id"]))
            store.restore(parent_workspace["snapshot_id"], proof)
            raw = (proof / MANIFEST).read_bytes()
            if hashlib.sha256(raw).hexdigest() != operation["manifest_sha256"]:
                raise ValueError("parent manifest digest differs from committed state")
            local = root / MANIFEST
            if local.is_symlink() or local.read_bytes() != raw:
                raise ValueError("local parent manifest differs from saved parent")
            manifest = parse_manifest(raw)
            child = next(
                (row for row in manifest["children"] if row["path"] == child_path), None
            )
            if child is None or any(
                child[k] != authority.binding[k]
                for k in ("provider", "connection_id", "dimension_id")
            ):
                raise ValueError("child is not in the selected authority")
            pin = operation["child_pins"].get(child_path)
            if (
                pin is None
                or pin["room_id"] != child["room_id"]
                or pin["logical_jat_id"] != child["snapshot_id"]
            ):
                raise ValueError("child pin differs from committed operation")
            policy = load_capture_policy(root)
            if not policy.is_excluded(child_path):
                raise ValueError("child payload is not excluded from parent capture")
            target = root / child_path
            fd = _directory(target.parent)
            os.close(fd)
            if target.is_symlink():
                raise ValueError(
                    "child destination must be absent or an exact clean materialization"
                )
            record = catalog.resolve_snapshot(child["room_id"], child["snapshot_id"])
            if record["ciphertext_sha256"] != pin["descriptor_sha256"]:
                raise ValueError("child descriptor differs from historical pin")
            descriptor = authority.descriptor(record)
            corroborate_logical_snapshot(child["room_id"], record, descriptor)
            _verify_descriptor_contract(state, descriptor)
            if target.exists():
                existing = context_status(target)
                guard_fd = _directory(target)
                try:
                    guard = os.open(
                        ".josh-room-split.json",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=guard_fd,
                    )
                    try:
                        if not stat.S_ISREG(os.fstat(guard).st_mode):
                            raise ValueError("child namespace guard is invalid")
                        guard_bytes = os.read(guard, 1025)
                    finally:
                        os.close(guard)
                finally:
                    os.close(guard_fd)
                expected_guard = canonical(
                    {
                        "namespace": namespace,
                        "room_id": child["room_id"],
                        "snapshot_id": child["snapshot_id"],
                    }
                )
                if (
                    guard_bytes != expected_guard
                    or existing.get("composition_namespace") != namespace
                ):
                    raise ValueError(
                        "child namespace guard or marker differs; no ordinary Save is permitted"
                    )
                scan = scan_workspace_for_status(target)
                if (
                    not existing["ok"]
                    or existing.get("project_id") != child["room_id"]
                    or existing.get("snapshot_id") != child["snapshot_id"]
                    or existing.get("dimension_id") != dimension.dimension_id
                    or existing.get("encryption_domain_id")
                    != authority.binding["encryption_domain_id"]
                    or existing.get("workspace_signature") != scan.signature
                    or scan.capture_policy_sha256 != descriptor.to_dict()["capture_policy_sha256"]
                    or existing.get("capture_policy_sha256")
                    != scan.capture_policy_sha256
                ):
                    raise ValueError(
                        "child destination has local changes or a different marker; no clobber or unload"
                    )
                return {
                    "ok": True,
                    "status": "loaded",
                    "namespace": namespace,
                    "room_id": child["room_id"],
                    "snapshot_id": child["snapshot_id"],
                    "marker_verified": True,
                    "unit": "whole-child",
                }
            result = _restore(
                store,
                repository,
                descriptor,
                target,
                private,
                dimension,
                "Child Room",
                parent_current,
                namespace,
            )
        return {
            "ok": True,
            "status": "hydrated",
            "namespace": namespace,
            "room_id": result["project_id"],
            "snapshot_id": result["snapshot_id"],
            "marker_verified": True,
            "unit": "whole-child" if child_path else "parent",
        }
