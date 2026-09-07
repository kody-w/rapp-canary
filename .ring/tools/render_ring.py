#!/usr/bin/env python3
"""Render a ring deployment from canonical shared blobs plus scoped ring rewrites."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


KERNEL_PATH = "rapp_brainstem/brainstem.py"
KERNEL_SHA256 = "bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4"


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
    protected = value.get("protected_paths")
    if not isinstance(protected, list) or not all(
        isinstance(item, str)
        and item
        and not item.startswith(("/", "\\"))
        and ".." not in Path(item).parts
        for item in protected
    ):
        raise RenderError("invalid protected_paths")
    excluded = value.get("rewrite_excluded_prefixes", [])
    if not isinstance(excluded, list) or not all(
        isinstance(item, str)
        and item
        and not item.startswith(("/", "\\"))
        and ".." not in Path(item).parts
        for item in excluded
    ):
        raise RenderError("invalid rewrite_excluded_prefixes")
    return value


def _materialize(
    repo: Path,
    output: Path,
    protected_paths: tuple[str, ...],
) -> dict[str, str]:
    if output.exists() and any(output.iterdir()):
        raise RenderError("render output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    modes = {}
    for record in (item for item in tree.split("\0") if item):
        metadata, relative = record.split("\t", 1)
        mode, object_type, object_id = metadata.split()
        if any(
            relative == prefix.rstrip("/") or relative.startswith(prefix)
            for prefix in protected_paths
        ):
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


def _text_files(
    output: Path,
    excluded_prefixes: tuple[str, ...],
    excluded_paths: tuple[str, ...] = (),
):
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
        relative = path.relative_to(output).as_posix()
        if relative in excluded_paths:
            continue
        if any(
            relative == prefix.rstrip("/") or relative.startswith(prefix)
            for prefix in excluded_prefixes
        ):
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


def _profile_exclusions(output: Path, modes: dict[str, str]) -> tuple[str, ...]:
    relative = "rapp_brainstem/runtime_profile.json"
    if relative not in modes:
        if "rapp_brainstem/launch.py" in modes or "rapp_brainstem/provider_plugins/plugins.json" in modes:
            raise RenderError("provider runtime is missing its explicit profile")
        return ()
    # Read profile data only; rendering must never import candidate Python code.
    try:
        profile = json.loads((output / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RenderError("invalid provider runtime profile") from error
    if (
        not isinstance(profile, dict)
        or set(profile) != {
            "schema", "entrypoint", "kernel_sha256", "provider_api", "providers", "support_repository"
        }
        or profile.get("schema") != "brainstem-runtime-profile/1"
        or profile.get("entrypoint") != "launch.py"
        or profile.get("kernel_sha256") != KERNEL_SHA256
        or type(profile.get("provider_api")) is not int
        or profile["provider_api"] != 1
        or not isinstance(profile.get("providers"), list)
        or not all(
            isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name)
            for name in profile["providers"]
        )
        or len(set(profile["providers"])) != len(profile["providers"])
        or not isinstance(profile.get("support_repository"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", profile["support_repository"])
        or "rapp_brainstem/launch.py" not in modes
        or KERNEL_PATH not in modes
    ):
        raise RenderError("incompatible provider runtime profile")
    if hashlib.sha256((output / KERNEL_PATH).read_bytes()).hexdigest() != KERNEL_SHA256:
        raise RenderError("kernel-drift: provider runtime must render the pinned kernel unchanged")
    return (KERNEL_PATH,)


def render(repo: Path, config_path: Path, output: Path) -> dict:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RenderError("ring source worktree must be clean")
    config = _config(config_path)
    modes = _materialize(
        repo,
        output,
        tuple(
            item.replace("\\", "/")
            for item in config["protected_paths"]
        ),
    )
    applied = []
    kernel_exclusions = _profile_exclusions(output, modes)
    excluded_prefixes = tuple(
        item.replace("\\", "/")
        for item in config.get("rewrite_excluded_prefixes", [])
    )
    for rule in config["rewrites"]:
        needle = rule["from"].encode("utf-8")
        replacement = rule["to"].encode("utf-8")
        files = list(_text_files(output, excluded_prefixes, kernel_exclusions))
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
        remaining = sum(
            data.count(needle)
            for _, data in _text_files(output, excluded_prefixes, kernel_exclusions)
        )
        if remaining:
            raise RenderError(
                f"rewrite source remains in production files: {rule['from']!r}"
            )
    result = {
        "schema": "rapp-ring-render/1",
        "ring": config["name"],
        "source_commit": _git(repo, "rev-parse", "HEAD^{commit}").strip(),
        "rendered_sha256": _digest(output, modes),
        "rewrites": applied,
    }
    if kernel_exclusions:
        result["kernel_sha256"] = hashlib.sha256((output / KERNEL_PATH).read_bytes()).hexdigest()
    return result


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
