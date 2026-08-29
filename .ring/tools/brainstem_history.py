#!/usr/bin/env python3
"""Record and verify immutable frames for known-good Grail brainstems."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


SCHEMA = "rapp/1:brainstem"
REPOSITORY = "kody-w/rapp-installer"
BRAINSTEM_PATH = "rapp_brainstem/brainstem.py"
VERSION_PATH = "rapp_brainstem/VERSION"


class HistoryError(RuntimeError):
    pass


def _git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise HistoryError(f"git {' '.join(args)} failed: {detail}")
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


def _canonical_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def frame_sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_frame(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HistoryError(f"cannot read frame {path}: {error}") from error
    if not isinstance(value, dict):
        raise HistoryError("brainstem frame must be a JSON object")
    return value


def _write_frame(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_shape(frame: dict) -> None:
    if frame.get("schema") != SCHEMA:
        raise HistoryError("unsupported brainstem frame schema")
    if frame.get("repository") != REPOSITORY:
        raise HistoryError("brainstem frame is not bound to the Grail repository")
    if frame.get("result") != "known-good":
        raise HistoryError("brainstem frame is not marked known-good")
    if not re.fullmatch(r"brainstem-v[0-9]+\.[0-9]+\.[0-9]+", str(frame.get("release_ref", ""))):
        raise HistoryError("brainstem frame requires an immutable release tag")
    if frame.get("release_ref") != f"brainstem-v{frame.get('version', '')}":
        raise HistoryError("release tag and VERSION do not match")
    for key in ("commit", "tree"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(frame.get(key, ""))):
            raise HistoryError(f"invalid frame {key}")
    brainstem = frame.get("brainstem")
    if not isinstance(brainstem, dict):
        raise HistoryError("brainstem material is missing")
    if brainstem.get("path") != BRAINSTEM_PATH:
        raise HistoryError("brainstem frame points at the wrong path")
    if not re.fullmatch(r"[0-9a-f]{40}", str(brainstem.get("blob", ""))):
        raise HistoryError("invalid brainstem blob")
    if not re.fullmatch(r"[0-9a-f]{64}", str(brainstem.get("sha256", ""))):
        raise HistoryError("invalid brainstem sha256")
    if not isinstance(brainstem.get("size_bytes"), int) or brainstem["size_bytes"] < 1:
        raise HistoryError("invalid brainstem size")
    parent = frame.get("parent")
    if parent is not None and (
        not isinstance(parent, dict)
        or not isinstance(parent.get("release_ref"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(parent.get("sha256", "")))
    ):
        raise HistoryError("invalid parent brainstem frame")


def create_frame(
    repo: Path,
    release_ref: str,
    output: Path,
    parent_path: Path | None = None,
) -> dict:
    repo = repo.resolve()
    origin = _git(repo, "remote", "get-url", "origin").strip()
    if _repo_slug(origin) != REPOSITORY:
        raise HistoryError(f"repository origin is not {REPOSITORY}")
    commit = _git(repo, "rev-parse", f"{release_ref}^{{commit}}").strip()
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").strip()
    blob = _git(repo, "rev-parse", f"{commit}:{BRAINSTEM_PATH}").strip()
    brainstem = _git(repo, "show", f"{commit}:{BRAINSTEM_PATH}", binary=True)
    version = _git(repo, "show", f"{commit}:{VERSION_PATH}").strip()
    if not version:
        raise HistoryError("release has no brainstem version")

    parent = None
    if parent_path:
        parent_frame = _read_frame(parent_path)
        _validate_shape(parent_frame)
        parent = {
            "release_ref": parent_frame["release_ref"],
            "sha256": frame_sha256(parent_frame),
        }
    frame = {
        "schema": SCHEMA,
        "repository": REPOSITORY,
        "release_ref": release_ref,
        "version": version,
        "commit": commit,
        "tree": tree,
        "brainstem": {
            "path": BRAINSTEM_PATH,
            "blob": blob,
            "sha256": hashlib.sha256(brainstem).hexdigest(),
            "size_bytes": len(brainstem),
        },
        "parent": parent,
        "result": "known-good",
    }
    _validate_shape(frame)
    _write_frame(output, frame)
    return frame


def verify_frame(
    repo: Path,
    frame_path: Path,
    parent_path: Path | None = None,
) -> dict:
    repo = repo.resolve()
    frame = _read_frame(frame_path)
    _validate_shape(frame)
    origin = _git(repo, "remote", "get-url", "origin").strip()
    if _repo_slug(origin) != REPOSITORY:
        raise HistoryError(f"repository origin is not {REPOSITORY}")
    resolved = _git(repo, "rev-parse", f"{frame['release_ref']}^{{commit}}").strip()
    if resolved != frame["commit"]:
        raise HistoryError("release ref moved away from its recorded commit")
    if frame.get("parent") is not None and parent_path is None:
        parent_path = (
            frame_path.parent
            / f"{frame['parent']['release_ref']}.json"
        )
        if not parent_path.is_file():
            raise HistoryError("parent brainstem frame is missing")
    expected = create_frame(repo, frame["release_ref"], frame_path.with_suffix(".verify.tmp"), parent_path)
    try:
        if expected != frame:
            raise HistoryError("brainstem frame does not match the release")
    finally:
        try:
            frame_path.with_suffix(".verify.tmp").unlink()
        except FileNotFoundError:
            pass
    return frame


def verify_history(repo: Path, directory: Path) -> tuple[int, dict]:
    tags = [
        value
        for value in _git(
            repo,
            "for-each-ref",
            "--sort=version:refname",
            "--format=%(refname:short)",
            "refs/tags/brainstem-v*",
        ).splitlines()
        if value
    ]
    if not tags:
        raise HistoryError("Grail has no brainstem release tags")
    expected_files = {f"{tag}.json" for tag in tags}
    actual_files = {path.name for path in directory.glob("brainstem-v*.json")}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise HistoryError(
            f"brainstem history does not match Grail tags; missing={missing}, extra={extra}"
        )

    parent_path = None
    previous = None
    for tag in tags:
        frame_path = directory / f"{tag}.json"
        frame = verify_frame(repo, frame_path, parent_path)
        expected_parent = None if previous is None else {
            "release_ref": previous["release_ref"],
            "sha256": frame_sha256(previous),
        }
        if frame["parent"] != expected_parent:
            raise HistoryError(f"{tag} does not link to the previous Grail frame")
        previous = frame
        parent_path = frame_path
    return len(tags), previous


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("record", "verify"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--repo", type=Path, required=True)
        sub.add_argument("--frame", type=Path, required=True)
        sub.add_argument("--parent", type=Path)
        if command == "record":
            sub.add_argument("--release-ref", required=True)
    verify_all = subparsers.add_parser("verify-all")
    verify_all.add_argument("--repo", type=Path, required=True)
    verify_all.add_argument("--directory", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == "record":
            frame = create_frame(
                args.repo,
                args.release_ref,
                args.frame,
                args.parent,
            )
            print(
                f"BRAINSTEM FRAME — {frame['release_ref']} "
                f"{frame['brainstem']['sha256'][:12]}"
            )
        elif args.command == "verify":
            frame = verify_frame(args.repo, args.frame, args.parent)
            print(
                f"BRAINSTEM VERIFIED — {frame['release_ref']} "
                f"{frame['brainstem']['sha256'][:12]}"
            )
        else:
            count, tip = verify_history(args.repo, args.directory)
            print(
                f"BRAINSTEM HISTORY VERIFIED — {count} frames "
                f"(tip {tip['release_ref']})"
            )
    except (HistoryError, OSError, ValueError) as error:
        print(f"brainstem history failed: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
