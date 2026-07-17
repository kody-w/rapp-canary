#!/usr/bin/env python3
"""Create and verify deterministic attestations as one payload moves through rings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


class AttestationError(RuntimeError):
    pass


def _git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise AttestationError(
            f"git {' '.join(args)} failed in {repo}: {detail}"
        )
    return result.stdout if binary else result.stdout.decode("utf-8")


def _repo_slug(remote_url: str) -> str | None:
    patterns = (
        r"^https://github\.com/([^/\s]+/[^/\s]+?)(?:\.git)?/?$",
        r"^ssh://git@github\.com/([^/\s]+/[^/\s]+?)(?:\.git)?/?$",
        r"^git@github\.com:([^/\s]+/[^/\s]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, remote_url.strip(), re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AttestationError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise AttestationError(f"{path} must contain a JSON object")
    return value


def _canonical_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _attestation_sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _ring_map(config: dict) -> dict[str, dict]:
    if config.get("schema") != "rapp-release-train/1":
        raise AttestationError("unsupported release-train schema")
    rings = config.get("rings")
    if not isinstance(rings, list):
        raise AttestationError("release train must define rings")
    result = {}
    for ring in rings:
        if not isinstance(ring, dict) or not isinstance(ring.get("name"), str):
            raise AttestationError("invalid ring definition")
        name = ring["name"]
        if name in result:
            raise AttestationError(f"duplicate ring: {name}")
        result[name] = ring
    for name, ring in result.items():
        parent = ring.get("parent")
        if parent is not None and parent not in result:
            raise AttestationError(f"{name} has unknown parent {parent}")
    return result


def _ring_owned_prefixes(config: dict) -> tuple[str, ...]:
    prefixes = config.get("ring_owned_prefixes")
    if not isinstance(prefixes, list) or not all(
        isinstance(item, str)
        and item
        and not item.startswith(("/", "\\"))
        and ".." not in Path(item).parts
        for item in prefixes
    ):
        raise AttestationError("invalid ring_owned_prefixes")
    return tuple(item.replace("\\", "/") for item in prefixes)


def _is_ring_owned(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in prefixes
    )


def _payload_tree_sha256(
    repo: Path,
    ring_owned_prefixes: tuple[str, ...],
) -> str:
    output = _git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "HEAD",
    )
    digest = hashlib.sha256()
    for record in (item for item in output.split("\0") if item):
        try:
            metadata, path = record.split("\t", 1)
            mode, object_type, object_id = metadata.split()
            if _is_ring_owned(path, ring_owned_prefixes):
                continue
        except ValueError as error:
            raise AttestationError(
                f"cannot parse Git tree record: {record!r}"
            ) from error
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise AttestationError(
                f"payload contains unsupported {object_type} mode {mode}: {path}"
            )
        blob = _git(repo, "cat-file", "blob", object_id, binary=True)
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(mode.encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(blob).digest())
    return digest.hexdigest()


def _payload(
    repo: Path,
    expected_repository: str,
    expected_commit: str,
    ring_owned_prefixes: tuple[str, ...],
) -> dict:
    top = Path(_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if os.path.normcase(str(top)) != os.path.normcase(str(repo.resolve())):
        raise AttestationError("payload checkout must be its repository root")
    origin = _git(repo, "remote", "get-url", "origin").strip()
    if _repo_slug(origin) != expected_repository.lower():
        raise AttestationError(
            f"payload origin must be github.com/{expected_repository}"
        )
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise AttestationError("payload worktree must be clean")
    commit = _git(repo, "rev-parse", "HEAD^{commit}").strip()
    if commit != expected_commit:
        raise AttestationError(
            f"payload HEAD {commit} does not match expected {expected_commit}"
        )
    return {
        "repository": expected_repository,
        "commit": commit,
        "tree": _git(repo, "rev-parse", "HEAD^{tree}").strip(),
        "shared_sha256": _payload_tree_sha256(repo, ring_owned_prefixes),
    }


def _validate_attestation(value: dict, rings: dict[str, dict]) -> None:
    if value.get("schema") != "rapp-ring-attestation/1":
        raise AttestationError("unsupported ring attestation schema")
    ring_name = value.get("ring")
    if ring_name not in rings:
        raise AttestationError(f"unknown attested ring: {ring_name}")
    if value.get("result") != "passed":
        raise AttestationError("ring result is not passed")
    payload = value.get("payload")
    if not isinstance(payload, dict) or set(payload) != {
        "repository",
        "commit",
        "tree",
        "shared_sha256",
    }:
        raise AttestationError("invalid attested payload")
    configured_repository = rings[ring_name].get("repository")
    if (
        not isinstance(configured_repository, str)
        or payload.get("repository") != configured_repository
    ):
        raise AttestationError(
            f"{ring_name} attestation repository does not match train config"
        )
    for key in ("commit", "tree"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get(key, ""))):
            raise AttestationError(f"invalid payload {key}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("shared_sha256", ""))):
        raise AttestationError("invalid payload shared_sha256")

    expected_parent = rings[ring_name].get("parent")
    parent = value.get("parent")
    if expected_parent is None:
        if parent is not None:
            raise AttestationError(f"{ring_name} cannot have a parent attestation")
    elif (
        not isinstance(parent, dict)
        or parent.get("ring") != expected_parent
        or not re.fullmatch(r"[0-9a-f]{64}", str(parent.get("sha256", "")))
    ):
        raise AttestationError(
            f"{ring_name} requires a valid {expected_parent} parent"
        )


def create_attestation(
    ring_name: str,
    repo: Path,
    repository: str,
    commit: str,
    config_path: Path,
    output: Path,
    parent_path: Path | None,
) -> dict:
    config = _read_json(config_path)
    rings = _ring_map(config)
    ring_owned_prefixes = _ring_owned_prefixes(config)
    if ring_name not in rings:
        raise AttestationError(f"unknown ring: {ring_name}")
    configured_repository = rings[ring_name].get("repository")
    if repository != configured_repository:
        raise AttestationError(
            f"{ring_name} repository must be {configured_repository}"
        )
    payload = _payload(
        repo,
        repository,
        commit,
        ring_owned_prefixes,
    )
    expected_parent = rings[ring_name].get("parent")
    parent_ref = None
    if expected_parent is not None:
        if parent_path is None:
            raise AttestationError(
                f"{ring_name} requires a {expected_parent} parent attestation"
            )
        parent = _read_json(parent_path)
        _validate_attestation(parent, rings)
        if parent["ring"] != expected_parent:
            raise AttestationError(
                f"{ring_name} requires parent {expected_parent}, got {parent['ring']}"
            )
        if (
            parent["payload"]["shared_sha256"]
            != payload["shared_sha256"]
        ):
            raise AttestationError(
                "payload changed between rings; rebuilds cannot be promoted"
            )
        parent_ref = {
            "ring": parent["ring"],
            "sha256": _attestation_sha256(parent),
        }
    elif parent_path is not None:
        raise AttestationError(f"{ring_name} does not accept a parent")

    attestation = {
        "schema": "rapp-ring-attestation/1",
        "ring": ring_name,
        "payload": payload,
        "parent": parent_ref,
        "result": "passed",
    }
    _validate_attestation(attestation, rings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(
        json.dumps(attestation, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    return attestation


def verify_attestation(
    attestation_path: Path,
    repo: Path,
    repository: str,
    commit: str,
    config_path: Path,
    parent_path: Path | None,
) -> dict:
    config = _read_json(config_path)
    rings = _ring_map(config)
    ring_owned_prefixes = _ring_owned_prefixes(config)
    attestation = _read_json(attestation_path)
    _validate_attestation(attestation, rings)
    if attestation["payload"] != _payload(
        repo,
        repository,
        commit,
        ring_owned_prefixes,
    ):
        raise AttestationError("attested payload does not match checkout")
    expected_parent = rings[attestation["ring"]].get("parent")
    if expected_parent is not None:
        if parent_path is None:
            raise AttestationError("parent attestation is required for verification")
        parent = _read_json(parent_path)
        _validate_attestation(parent, rings)
        expected_ref = {
            "ring": parent["ring"],
            "sha256": _attestation_sha256(parent),
        }
        if attestation["parent"] != expected_ref:
            raise AttestationError("parent attestation digest does not match")
        if (
            parent["payload"]["shared_sha256"]
            != attestation["payload"]["shared_sha256"]
        ):
            raise AttestationError(
                "parent and child attest different shared payloads"
            )
    return attestation


def _parse_args():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=root.parent / "train.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "verify"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--repo", type=Path, required=True)
        sub.add_argument("--repository", required=True)
        sub.add_argument("--commit", required=True)
        sub.add_argument("--parent", type=Path)
        sub.add_argument("--attestation", type=Path, required=True)
        if name == "create":
            sub.add_argument("--ring", required=True)
    return parser.parse_args()


def main():
    args = _parse_args()
    try:
        if args.command == "create":
            value = create_attestation(
                args.ring,
                args.repo.resolve(),
                args.repository,
                args.commit,
                args.config.resolve(),
                args.attestation.resolve(),
                args.parent.resolve() if args.parent else None,
            )
        else:
            value = verify_attestation(
                args.attestation.resolve(),
                args.repo.resolve(),
                args.repository,
                args.commit,
                args.config.resolve(),
                args.parent.resolve() if args.parent else None,
            )
    except AttestationError as error:
        print(f"attestation failed: {error}", file=sys.stderr)
        return 1
    print(
        f"{value['ring']} passed for "
        f"{value['payload']['commit'][:12]} "
        f"({value['payload']['shared_sha256'][:12]})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
