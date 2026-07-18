#!/usr/bin/env python3
"""Advance shared Git blobs between rings while preserving target-owned overlays."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import ring_attestation as attestation


class PromotionError(RuntimeError):
    pass


def _git(repo: Path, *args: str, binary=False):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PromotionError(
            f"git {' '.join(args)} failed in {repo}: {detail}"
        )
    return result.stdout if binary else result.stdout.decode()


def _validate_repo(repo: Path, expected: str, expected_commit: str) -> None:
    top = Path(_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if os.path.normcase(str(top)) != os.path.normcase(str(repo.resolve())):
        raise PromotionError("ring checkout must be its repository root")
    origin = _git(repo, "remote", "get-url", "origin").strip()
    if attestation._repo_slug(origin) != expected.lower():
        raise PromotionError(f"origin must be github.com/{expected}")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise PromotionError(f"{expected} worktree must be clean")
    commit = _git(repo, "rev-parse", "HEAD^{commit}").strip()
    if commit != expected_commit:
        raise PromotionError(
            f"{expected} HEAD {commit} does not match {expected_commit}"
        )


def _entries(
    repo: Path,
    owned_prefixes: tuple[str, ...],
) -> dict[str, tuple[str, str]]:
    output = _git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "HEAD",
    )
    entries = {}
    for record in (item for item in output.split("\0") if item):
        metadata, path = record.split("\t", 1)
        mode, object_type, object_id = metadata.split()
        if attestation._is_ring_owned(path, owned_prefixes):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise PromotionError(
                f"shared payload contains unsupported {object_type} {mode}: {path}"
            )
        entries[path] = (mode, object_id)
    return entries


def _tracked_shared(
    repo: Path,
    owned_prefixes: tuple[str, ...],
) -> set[str]:
    output = _git(repo, "ls-files", "-z")
    return {
        path for path in output.split("\0") if path
        and not attestation._is_ring_owned(path, owned_prefixes)
    }


def _write_blob(
    source: Path,
    target: Path,
    path: str,
    mode: str,
    object_id: str,
) -> None:
    destination = target / Path(path)
    current = destination.parent
    while current != target:
        if current.is_symlink():
            raise PromotionError(f"target parent is a symlink: {current}")
        current = current.parent
    if destination.is_symlink() or destination.is_dir():
        raise PromotionError(f"target path is not a regular file: {path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = _git(source, "cat-file", "blob", object_id, binary=True)
    destination.write_bytes(data)
    bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    current_mode = destination.stat().st_mode
    os.chmod(
        destination,
        current_mode | bits if mode == "100755" else current_mode & ~bits,
    )


def _preflight_transition(
    target: Path,
    source_paths: set[str],
    target_paths: set[str],
) -> None:
    deletions = target_paths.difference(source_paths)
    for path in sorted(source_paths):
        destination = target / Path(path)
        if destination.is_symlink():
            raise PromotionError(f"target path is a symlink: {path}")
        if destination.is_file() and path not in target_paths:
            raise PromotionError(
                f"refusing to overwrite untracked or ignored file: {path}"
            )
        if destination.is_dir():
            remaining = []
            for child in destination.rglob("*"):
                if not child.is_file() and not child.is_symlink():
                    continue
                relative = child.relative_to(target).as_posix()
                if relative not in deletions:
                    remaining.append(relative)
            if remaining:
                raise PromotionError(
                    f"directory-to-file transition would overwrite: {remaining}"
                )
        current = destination.parent
        while current != target:
            if current.is_symlink():
                raise PromotionError(f"target parent is a symlink: {current}")
            if current.is_file():
                relative = current.relative_to(target).as_posix()
                if relative not in deletions:
                    raise PromotionError(
                        f"file-to-directory transition would overwrite: {relative}"
                    )
            current = current.parent


def _remove_empty_parents(path: Path, target: Path) -> None:
    current = path
    while current != target:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _safe_lock_path(target: Path) -> Path:
    ring_dir = target / ".ring"
    target_real = target.resolve()
    if ring_dir.is_symlink():
        raise PromotionError(".ring must not be a symlink")
    if ring_dir.exists() and not ring_dir.is_dir():
        raise PromotionError(".ring must be a directory")
    ring_dir.mkdir(parents=True, exist_ok=True)
    ring_real = ring_dir.resolve()
    if os.path.commonpath((str(target_real), str(ring_real))) != str(target_real):
        raise PromotionError(".ring resolves outside the target checkout")
    lock_path = ring_dir / "upstream.lock.json"
    if lock_path.is_symlink():
        raise PromotionError("promotion lock must not be a symlink")
    if lock_path.exists() and not lock_path.is_file():
        raise PromotionError("promotion lock must be a regular file")
    return lock_path


def _stage_raw(target: Path, relative: str, mode: str) -> None:
    object_id = _git(
        target,
        "hash-object",
        "-w",
        "--no-filters",
        "--",
        relative,
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", object_id):
        raise PromotionError(f"cannot hash promoted path: {relative}")
    _git(
        target,
        "update-index",
        "--add",
        "--cacheinfo",
        mode,
        object_id,
        relative,
    )


def _write_lock(target: Path, lock_path: Path, lock: dict) -> None:
    payload = (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".upstream-lock-",
        suffix=".tmp",
        dir=lock_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, lock_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    _stage_raw(target, lock_path.relative_to(target).as_posix(), "100644")


def promote(
    source: Path,
    target: Path,
    source_ring: str,
    target_ring: str,
    source_commit: str,
    target_commit: str,
    config_path: Path,
) -> dict:
    config = attestation._read_json(config_path)
    rings = attestation._ring_map(config)
    prefixes = attestation._ring_owned_prefixes(config)
    if source_ring not in rings or target_ring not in rings:
        raise PromotionError("unknown source or target ring")
    if rings[target_ring].get("parent") != source_ring:
        raise PromotionError(
            f"{source_ring} cannot promote directly to {target_ring}"
        )
    if (
        not rings[target_ring].get("automated_promotion", False)
        or rings[target_ring].get("human_merge_required", False)
    ):
        raise PromotionError(
            f"{target_ring} requires an explicit human-controlled promotion"
        )
    source_repository = rings[source_ring].get("repository")
    target_repository = rings[target_ring].get("repository")
    if not source_repository or not target_repository:
        raise PromotionError("both rings require repository identities")
    _validate_repo(source, source_repository, source_commit)
    _validate_repo(target, target_repository, target_commit)
    lock_path = _safe_lock_path(target)

    source_entries = _entries(source, prefixes)
    required_paths = config.get("required_shared_paths", [])
    if not isinstance(required_paths, list) or not all(
        isinstance(item, str) and item for item in required_paths
    ):
        raise PromotionError("invalid required_shared_paths")
    missing_required = sorted(set(required_paths).difference(source_entries))
    if missing_required:
        raise PromotionError(
            "required shared paths are missing: " + ", ".join(missing_required)
        )
    target_entries = _tracked_shared(target, prefixes)
    _preflight_transition(
        target,
        set(source_entries),
        target_entries,
    )
    for path in sorted(target_entries.difference(source_entries)):
        destination = target / Path(path)
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
            _git(target, "update-index", "--force-remove", "--", path)
            _remove_empty_parents(destination.parent, target)
        elif destination.exists():
            raise PromotionError(f"cannot delete non-file shared path: {path}")
    for path, (mode, object_id) in sorted(source_entries.items()):
        destination = target / Path(path)
        if destination.is_dir():
            shutil.rmtree(destination)
        _write_blob(source, target, path, mode, object_id)
        _stage_raw(target, path, mode)

    shared_sha256 = attestation._payload_tree_sha256(source, prefixes)
    lock = {
        "schema": "rapp-ring-promotion/1",
        "source": {
            "ring": source_ring,
            "repository": source_repository,
            "commit": source_commit,
            "tree": _git(source, "rev-parse", "HEAD^{tree}").strip(),
            "shared_sha256": shared_sha256,
        },
        "target": {
            "ring": target_ring,
            "repository": target_repository,
            "base_commit": target_commit,
        },
    }
    _write_lock(target, lock_path, lock)
    return lock


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--source-ring", required=True)
    parser.add_argument("--target-ring", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=root.parent / "train.json",
    )
    args = parser.parse_args()
    for value in (args.source_commit, args.target_commit):
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            print("promotion failed: commits must be full SHAs", file=sys.stderr)
            return 1
    try:
        lock = promote(
            args.source.resolve(),
            args.target.resolve(),
            args.source_ring,
            args.target_ring,
            args.source_commit,
            args.target_commit,
            args.config.resolve(),
        )
    except (PromotionError, attestation.AttestationError) as error:
        print(f"promotion failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Prepared {lock['source']['ring']} -> {lock['target']['ring']} "
        f"for {lock['source']['shared_sha256'][:12]}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
