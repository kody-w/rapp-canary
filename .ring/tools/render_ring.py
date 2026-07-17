#!/usr/bin/env python3
"""Render a ring deployment from canonical shared blobs plus scoped ring rewrites."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


class RenderError(RuntimeError):
    pass


def _git(repo: Path, *args: str, binary=False):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RenderError(
            result.stderr.decode("utf-8", errors="replace").strip()
        )
    return result.stdout if binary else result.stdout.decode()


def _config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RenderError(f"invalid ring config: {error}") from error
    if value.get("schema") != "rapp-ring/1":
        raise RenderError("unsupported ring config schema")
    rewrites = value.get("rewrites")
    if not isinstance(rewrites, list):
        raise RenderError("ring config must define rewrites")
    for rule in rewrites:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"from", "to", "expected_count"}
            or not isinstance(rule["from"], str)
            or not rule["from"]
            or not isinstance(rule["to"], str)
            or not isinstance(rule["expected_count"], int)
            or rule["expected_count"] < 0
        ):
            raise RenderError("invalid ring rewrite rule")
    return value


def _materialize(repo: Path, output: Path) -> dict[str, str]:
    if output.exists() and any(output.iterdir()):
        raise RenderError("render output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    modes = {}
    for record in (item for item in tree.split("\0") if item):
        metadata, relative = record.split("\t", 1)
        mode, object_type, object_id = metadata.split()
        if relative == ".ring" or relative.startswith(".ring/"):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise RenderError(
                f"shared tree contains unsupported {object_type} {mode}: {relative}"
            )
        destination = output / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            _git(repo, "cat-file", "blob", object_id, binary=True)
        )
        bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        current = destination.stat().st_mode
        os.chmod(
            destination,
            current | bits if mode == "100755" else current & ~bits,
        )
        modes[relative] = mode
    return modes


def _text_files(output: Path):
    for path in output.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield path, data


def _digest(output: Path, modes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, mode in sorted(modes.items()):
        data = (output / Path(relative)).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(mode.encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def render(repo: Path, config_path: Path, output: Path) -> dict:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RenderError("ring source worktree must be clean")
    config = _config(config_path)
    modes = _materialize(repo, output)
    applied = []
    for rule in config["rewrites"]:
        needle = rule["from"].encode("utf-8")
        replacement = rule["to"].encode("utf-8")
        files = list(_text_files(output))
        count = sum(data.count(needle) for _, data in files)
        if count != rule["expected_count"]:
            raise RenderError(
                f"rewrite count drift for {rule['from']!r}: "
                f"expected {rule['expected_count']}, found {count}"
            )
        for path, data in files:
            rewritten = data.replace(needle, replacement)
            if rewritten != data:
                path.write_bytes(rewritten)
        applied.append({
            "from": rule["from"],
            "to": rule["to"],
            "count": count,
        })
    return {
        "schema": "rapp-ring-render/1",
        "ring": config["name"],
        "source_commit": _git(repo, "rev-parse", "HEAD^{commit}").strip(),
        "rendered_sha256": _digest(output, modes),
        "rewrites": applied,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = render(
            args.repo.resolve(),
            args.config.resolve(),
            args.output.resolve(),
        )
    except RenderError as error:
        print(f"render failed: {error}", file=sys.stderr)
        return 1
    if args.report:
        args.report.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(
        f"Rendered {result['ring']} at {result['rendered_sha256'][:12]} "
        f"with {sum(item['count'] for item in result['rewrites'])} rewrites."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
