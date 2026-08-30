#!/usr/bin/env python3
"""Build one platform's sealed runtime and test dependency material."""

from __future__ import annotations

import argparse
import email
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import preprod_gate  # noqa: E402


class MaterialError(RuntimeError):
    pass


def _run(arguments: list[str]) -> None:
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = "\n".join(
            value for value in (result.stdout.strip(), result.stderr.strip()) if value
        )
        raise MaterialError(
            f"{' '.join(arguments[:5])} failed"
            + (f":\n{detail}" if detail else "")
        )


def _python_in(venv: Path) -> Path:
    return (
        venv / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv / "bin" / "python"
    )


def _pins_from_report(report_path: Path) -> list[str]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MaterialError(f"cannot read pip resolution report: {error}") from error
    pins = sorted(
        {
            f"{item['metadata']['name']}=={item['metadata']['version']}"
            for item in report.get("install", [])
            if isinstance(item, dict) and isinstance(item.get("metadata"), dict)
        },
        key=str.casefold,
    )
    if not pins or not all(preprod_gate.LOCK_PATTERN.fullmatch(pin) for pin in pins):
        raise MaterialError("pip resolution did not produce exact registry pins")
    return pins


def _write_lock(path: Path, pins: list[str]) -> None:
    path.write_text("\n".join(pins) + "\n", encoding="utf-8", newline="\n")


def _combined_pins(*locks: list[str]) -> list[str]:
    by_name: dict[str, str] = {}
    for pin in (item for lock in locks for item in lock):
        name, version = pin.split("==", 1)
        normalized = re.sub(r"[-_.]+", "-", name).casefold()
        if normalized in by_name and by_name[normalized] != version:
            raise MaterialError(
                f"runtime and test tools require conflicting {name} versions"
            )
        by_name[normalized] = version
    return [
        f"{name}=={by_name[name]}"
        for name in sorted(by_name)
    ]


def _wheel_metadata(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_names = [
                name
                for name in wheel.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise MaterialError(f"{path.name} has ambiguous wheel metadata")
            info = wheel.getinfo(metadata_names[0])
            if info.file_size > 1024 * 1024:
                raise MaterialError(f"{path.name} metadata exceeds 1 MiB")
            metadata = email.message_from_bytes(wheel.read(info))
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise MaterialError(f"cannot inspect wheel {path.name}: {error}") from error
    license_classifiers = [
        value
        for value in metadata.get_all("Classifier", [])
        if value.startswith("License ::")
    ]
    declared = (
        metadata.get("License-Expression")
        or metadata.get("License")
        or "; ".join(license_classifiers)
    ).strip()
    if not declared or declared.upper() in {"UNKNOWN", "NONE"}:
        raise MaterialError(f"{path.name} has no declared license")
    if "AGPL" in declared.upper():
        raise MaterialError(f"{path.name} has a blocked license: {declared}")
    return declared


def build(
    requirements: Path,
    output: Path,
    artifact: Path,
    platform_name: str,
    audit_lock: Path,
    test_requirement: str,
) -> str:
    preprod_gate._requirement_lines(requirements)
    if not preprod_gate.LOCK_PATTERN.fullmatch(test_requirement):
        raise MaterialError("test tooling must be one exact registry package pin")
    if output.exists() and (
        not output.is_dir() or any(output.iterdir())
    ):
        raise MaterialError("dependency output must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    runtime_wheelhouse = output / "wheelhouse"
    test_wheelhouse = output / "test-wheelhouse"
    runtime_wheelhouse.mkdir()
    test_wheelhouse.mkdir()

    runtime_report = output / "resolution.json"
    test_report = output / "test-resolution.json"
    _run([
        sys.executable,
        "-I",
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--only-binary=:all:",
        "--report",
        str(runtime_report),
        "-r",
        str(requirements),
    ])
    _run([
        sys.executable,
        "-I",
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--only-binary=:all:",
        "--report",
        str(test_report),
        test_requirement,
    ])
    runtime_pins = _pins_from_report(runtime_report)
    test_pins = _pins_from_report(test_report)
    runtime_lock = output / "requirements.lock"
    test_lock = output / "test-requirements.lock"
    _write_lock(runtime_lock, runtime_pins)
    _write_lock(test_lock, test_pins)
    _run([
        sys.executable,
        "-I",
        "-m",
        "pip",
        "download",
        "--quiet",
        "--only-binary=:all:",
        "--dest",
        str(runtime_wheelhouse),
        "-r",
        str(runtime_lock),
    ])
    _run([
        sys.executable,
        "-I",
        "-m",
        "pip",
        "download",
        "--quiet",
        "--only-binary=:all:",
        "--dest",
        str(test_wheelhouse),
        "-r",
        str(test_lock),
    ])

    combined_lock = output.parent / f".{output.name}-audit.lock"
    _write_lock(combined_lock, _combined_pins(runtime_pins, test_pins))
    try:
        with tempfile.TemporaryDirectory(
            prefix="rapp-pip-audit-",
            dir=output.parent,
        ) as raw_audit_venv:
            audit_venv = Path(raw_audit_venv)
            _run([sys.executable, "-I", "-m", "venv", str(audit_venv)])
            audit_python = _python_in(audit_venv)
            _run([
                str(audit_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--quiet",
                "--force-reinstall",
                "--only-binary=:all:",
                "--require-hashes",
                "-r",
                str(audit_lock),
            ])
            _run([
                str(audit_python),
                "-I",
                "-m",
                "pip_audit",
                "-r",
                str(combined_lock),
                "--no-deps",
                "--format",
                "json",
                "--output",
                str(output / "vulnerability-report.json"),
            ])
    finally:
        combined_lock.unlink(missing_ok=True)

    files = {}
    licenses = {}
    for wheelhouse in (runtime_wheelhouse, test_wheelhouse):
        for path in sorted(wheelhouse.iterdir()):
            if path.suffix != ".whl":
                raise MaterialError(f"non-wheel dependency downloaded: {path.name}")
            relative = path.relative_to(output).as_posix()
            files[relative] = preprod_gate._sha256(path)
            licenses[relative] = _wheel_metadata(path)
    sbom = {
        "schema": "rapp-dependency-materials/1",
        "platform": platform_name,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "architecture": platform.machine().lower(),
        "runtime_requirements": runtime_pins,
        "test_requirements": test_pins,
        "files": files,
    }
    preprod_gate._write_json(output / "sbom.json", sbom)
    preprod_gate._write_json(
        output / "licenses.json",
        {
            "schema": "rapp-license-report/1",
            "platform": platform_name,
            "licenses": licenses,
            "blocked": [],
        },
    )
    digest = preprod_gate.build_artifact(output, artifact)
    preprod_gate._validate_dependency_material(
        f"dependency-material-{platform_name}",
        artifact,
    )
    return digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument(
        "--platform",
        choices=("linux", "macos", "windows"),
        required=True,
    )
    parser.add_argument(
        "--audit-lock",
        type=Path,
        default=TOOLS.parent / "tooling" / "pip-audit.lock",
    )
    parser.add_argument("--test-requirement", default="pytest==9.1.1")
    args = parser.parse_args()
    try:
        digest = build(
            args.requirements.resolve(),
            args.output.resolve(),
            args.artifact.resolve(),
            args.platform,
            args.audit_lock.resolve(),
            args.test_requirement,
        )
    except (MaterialError, OSError, preprod_gate.PreprodError) as error:
        print(f"dependency material failed: {error}", file=sys.stderr)
        return 1
    print(
        f"DEPENDENCY MATERIAL — {args.platform} sha256:{digest} "
        f"({args.artifact.name})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
