#!/usr/bin/env python3
"""Build and verify immutable RAPP/1 preprod readiness artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import brainstem_history  # noqa: E402


SCHEMA = "rapp/1:readiness"
POLICY_SCHEMA = "rapp-preprod-policy/1"
BETA_REPOSITORY = "kody-w/rapp-beta"
QUALIFICATION_REPOSITORY = "kody-w/rapp-canary"
QUALIFICATION_WORKFLOW = "Test Pre-Grail Rings"
BETA_PREFLIGHT_WORKFLOW = "preflight"
REQUIRED_MATERIALS = {
    "dependency-material-linux",
    "dependency-material-macos",
    "dependency-material-windows",
}


class PreprodError(RuntimeError):
    pass


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PreprodError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise PreprodError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: dict) -> None:
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


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PreprodError(f"invalid timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise PreprodError(f"timestamp lacks timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_material_specs(values: list[str]) -> dict[str, Path]:
    materials = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name)
            or name in materials
            or not raw_path
        ):
            raise PreprodError(f"invalid deployment material: {value}")
        materials[name] = Path(raw_path).resolve()
    return materials


def _material_manifest(materials: dict[str, Path]) -> dict:
    if set(materials) != REQUIRED_MATERIALS:
        raise PreprodError(
            "sealed readiness requires Linux, macOS, and Windows dependency materials"
        )
    result = {}
    for name, path in sorted(materials.items()):
        if not path.is_file():
            raise PreprodError(f"deployment material is missing: {path}")
        sbom = _validate_dependency_material(name, path)
        result[name] = {
            "file": path.name,
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
            "platform": sbom["platform"],
            "python_version": sbom["python_version"],
            "architecture": sbom["architecture"],
        }
    return result


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise PreprodError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout


def _hash_object(repo: Path, payload: bytes) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise PreprodError(
            "cannot write Grail Git object: "
            + result.stderr.decode("utf-8", errors="replace").strip()
        )
    object_id = result.stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", object_id):
        raise PreprodError("git hash-object returned an invalid object id")
    return object_id


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


def _verify_github_provenance(
    manifest: dict,
    subjects: tuple[Path, ...],
) -> None:
    for subject in subjects:
        result = subprocess.run(
            [
                "gh",
                "attestation",
                "verify",
                str(subject),
                "-R",
                QUALIFICATION_REPOSITORY,
                "--signer-workflow",
                "kody-w/rapp-canary/.github/workflows/stage-preprod.yml",
                "--source-ref",
                "refs/heads/main",
                "--signer-digest",
                manifest["evidence"]["control_plane"]["commit"],
                "--deny-self-hosted-runners",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise PreprodError(
                f"GitHub provenance verification failed for {subject.name}: "
                f"{result.stderr.strip()}"
            )


def _candidate_files(source: Path) -> list[Path]:
    tracked = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if tracked.returncode == 0:
        files = []
        for relative in (
            item for item in tracked.stdout.decode("utf-8").split("\0") if item
        ):
            path = source / relative
            if path.is_symlink():
                raise PreprodError(f"candidate contains a symlink: {relative}")
            if not path.is_file():
                raise PreprodError(f"tracked candidate file is missing: {relative}")
            files.append(path)
        return sorted(files, key=lambda item: item.relative_to(source).as_posix())

    files = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise PreprodError(f"candidate contains a symlink: {relative.as_posix()}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise PreprodError(f"candidate contains a non-regular entry: {relative.as_posix()}")
    return sorted(files, key=lambda item: item.relative_to(source).as_posix())


def build_artifact(source: Path, artifact: Path) -> str:
    source = source.resolve()
    if not source.is_dir():
        raise PreprodError(f"candidate directory does not exist: {source}")
    try:
        artifact.resolve().relative_to(source)
    except ValueError:
        pass
    else:
        raise PreprodError("artifact output must live outside the candidate tree")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    temporary = artifact.with_name(f".{artifact.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for path in _candidate_files(source):
                        relative = path.relative_to(source).as_posix()
                        info = archive.gettarinfo(str(path), arcname=relative)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
        os.replace(temporary, artifact)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _sha256(artifact)


def _validate_policy(policy: dict) -> None:
    if policy.get("schema") != POLICY_SCHEMA:
        raise PreprodError("unsupported preprod policy schema")
    if policy.get("environment") != "preprod":
        raise PreprodError("preprod policy must target the preprod environment")
    if policy.get("control_plane_only") is not True:
        raise PreprodError("preprod must remain a Canary-owned control-plane feature")
    if policy.get("deployment_branch") != "main":
        raise PreprodError("preprod deployments must come from main")
    reviewers = policy.get("minimum_required_reviewers")
    if not isinstance(reviewers, int) or reviewers < 1:
        raise PreprodError("preprod must require at least one reviewer")
    if policy.get("same_artifact_to_grail") is not True:
        raise PreprodError("policy must require the same artifact to reach Grail")
    if policy.get("human_approval_required") is not True:
        raise PreprodError("policy must require human approval")
    if policy.get("require_pinned_model") is not True:
        raise PreprodError("policy must require a pinned model")
    age = policy.get("max_candidate_age_hours")
    if not isinstance(age, int) or not 1 <= age <= 720:
        raise PreprodError("invalid max_candidate_age_hours")
    for key in ("max_artifact_bytes", "max_unpacked_bytes", "max_files"):
        value = policy.get(key)
        if not isinstance(value, int) or value < 1:
            raise PreprodError(f"invalid {key}")
    checks = policy.get("required_checks")
    if (
        not isinstance(checks, list)
        or not checks
        or len(set(checks)) != len(checks)
        or not all(isinstance(item, str) and item for item in checks)
    ):
        raise PreprodError("invalid required_checks")


def package_candidate(
    source: Path,
    artifact: Path,
    manifest_path: Path,
    policy_path: Path,
    beta_commit: str,
    qualification_run_id: str,
    qualification_url: str,
    beta_preflight_run_id: str,
    beta_preflight_url: str,
    soak_evidence_url: str,
    owner: str,
    control_plane_commit: str,
    model_id: str,
    rollback_ref: str,
    rollback_frame_path: Path,
    expires_hours: int | None = None,
    issued_at: datetime | None = None,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", beta_commit):
        raise PreprodError("beta commit must be a full lowercase SHA")
    for label, value in (
        ("qualification run id", qualification_run_id),
        ("beta preflight run id", beta_preflight_run_id),
    ):
        if not re.fullmatch(r"[0-9]+", value):
            raise PreprodError(f"{label} must be numeric")
    if not rollback_ref.strip():
        raise PreprodError("rollback ref is required")
    if not owner.strip():
        raise PreprodError("candidate owner is required")
    if not re.fullmatch(r"[0-9a-f]{40}", control_plane_commit):
        raise PreprodError("control-plane commit must be a full lowercase SHA")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model_id)
        or model_id.lower() == "auto"
    ):
        raise PreprodError("Preprod requires an explicit model id")
    for label, value in (
        ("qualification URL", qualification_url),
        ("Beta preflight URL", beta_preflight_url),
        ("soak evidence URL", soak_evidence_url),
    ):
        if not value.startswith("https://github.com/"):
            raise PreprodError(f"{label} must be a GitHub HTTPS URL")

    policy = _read_json(policy_path)
    _validate_policy(policy)
    try:
        manifest_path.resolve().relative_to(source.resolve())
    except ValueError:
        pass
    else:
        raise PreprodError("readiness manifest must live outside the candidate tree")
    lifetime = expires_hours or policy["max_candidate_age_hours"]
    if not isinstance(lifetime, int) or not 1 <= lifetime <= policy["max_candidate_age_hours"]:
        raise PreprodError("candidate lifetime exceeds preprod policy")
    version_path = source / "rapp_brainstem" / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise PreprodError(f"cannot read candidate version: {error}") from error
    if not version:
        raise PreprodError("candidate version is empty")
    brainstem_path = source / "rapp_brainstem" / "brainstem.py"
    if not brainstem_path.is_file():
        raise PreprodError("candidate brainstem.py is missing")
    rollback_frame = _read_json(rollback_frame_path)
    try:
        brainstem_history._validate_shape(rollback_frame)
    except brainstem_history.HistoryError as error:
        raise PreprodError(f"invalid rollback brainstem frame: {error}") from error
    if rollback_frame["release_ref"] != rollback_ref:
        raise PreprodError("rollback frame does not match rollback ref")
    origin = _git(source, "remote", "get-url", "origin").strip()
    if _repo_slug(origin) != "kody-w/rapp-installer":
        raise PreprodError("candidate is not a Grail-shaped repository")
    grail_base_commit = _git(source, "rev-parse", "HEAD^{commit}").strip()
    expected_tree = _git(source, "write-tree").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", grail_base_commit):
        raise PreprodError("invalid Grail base commit")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_tree):
        raise PreprodError("invalid expected Grail tree")

    artifact_sha256 = build_artifact(source, artifact)
    if artifact.stat().st_size > policy["max_artifact_bytes"]:
        raise PreprodError("candidate artifact exceeds preprod policy")
    now = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = now + timedelta(hours=lifetime)
    passed_controls = {
        "pre-grail-attestation-chain": [qualification_url],
        "beta-main-preflight": [beta_preflight_url],
        "immutable-artifact": [f"sha256:{artifact_sha256}"],
        "critical-brainstem-hash": [f"sha256:{_sha256(brainstem_path)}"],
        "pinned-model-policy": [f"model:{model_id}"],
        "real-auth-soak": [soak_evidence_url],
        "brainstem-rollback-frame": [
            f"sha256:{brainstem_history.frame_sha256(rollback_frame)}"
        ],
    }
    control_results = {
        check: {
            "status": "passed" if check in passed_controls else "pending",
            "evidence": passed_controls.get(check, []),
        }
        for check in policy["required_checks"]
    }
    manifest = {
        "schema": SCHEMA,
        "status": "preprod-candidate",
        "owner": owner,
        "subject": {
            "artifact": artifact.name,
            "artifact_sha256": artifact_sha256,
            "size_bytes": artifact.stat().st_size,
            "format": "tar+gzip",
            "version": version,
            "brainstem_sha256": _sha256(brainstem_path),
            "grail_base_commit": grail_base_commit,
            "expected_grail_tree": expected_tree,
            "beta_repository": BETA_REPOSITORY,
            "beta_commit": beta_commit,
        },
        "runtime": {
            "model_id": model_id,
            "data_classification": "synthetic",
        },
        "evidence": {
            "qualification": {
                "repository": QUALIFICATION_REPOSITORY,
                "workflow": QUALIFICATION_WORKFLOW,
                "run_id": qualification_run_id,
                "url": qualification_url,
            },
            "beta_preflight": {
                "repository": BETA_REPOSITORY,
                "workflow": BETA_PREFLIGHT_WORKFLOW,
                "run_id": beta_preflight_run_id,
                "url": beta_preflight_url,
            },
            "soak": {
                "url": soak_evidence_url,
            },
            "control_plane": {
                "repository": QUALIFICATION_REPOSITORY,
                "commit": control_plane_commit,
                "workflow": ".github/workflows/stage-preprod.yml",
            },
            "required_checks": policy["required_checks"],
            "controls": control_results,
        },
        "policy": {
            "same_artifact_to_grail": True,
            "human_approval_required": True,
        },
        "rollback": {
            "ref": rollback_ref,
            "commit": rollback_frame["commit"],
            "brainstem_sha256": rollback_frame["brainstem"]["sha256"],
            "frame_sha256": brainstem_history.frame_sha256(rollback_frame),
        },
        "issued_at": _format_time(now),
        "expires_at": _format_time(expires),
    }
    _write_json(manifest_path, manifest)
    return manifest


def _validate_archive(artifact: Path) -> list[tarfile.TarInfo]:
    try:
        with tarfile.open(artifact, "r:gz") as archive:
            members = archive.getmembers()
            seen = set()
            for member in members:
                path = PurePosixPath(member.name)
                normalized_parts = {
                    part.rstrip(" .").casefold()
                    for part in path.parts
                }
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or ".git" in normalized_parts
                    or any(":" in part for part in path.parts)
                    or member.issym()
                    or member.islnk()
                    or not member.isfile()
                ):
                    raise PreprodError(f"unsafe artifact member: {member.name}")
                if member.name in seen:
                    raise PreprodError(f"duplicate artifact member: {member.name}")
                seen.add(member.name)
            return members
    except (OSError, tarfile.TarError) as error:
        raise PreprodError(f"cannot inspect artifact: {error}") from error


def _validate_dependency_material(name: str, artifact: Path) -> dict:
    expected_platform = name.removeprefix("dependency-material-")
    members = _validate_archive(artifact)
    by_name = {member.name: member for member in members}
    required_metadata = {
        "requirements.lock",
        "sbom.json",
        "vulnerability-report.json",
        "licenses.json",
    }
    if not required_metadata.issubset(by_name):
        raise PreprodError(f"{name} lacks required lock, SBOM, or scan evidence")
    wheel_members = {
        member.name: member
        for member in members
        if member.name.startswith("wheelhouse/")
    }
    if not wheel_members:
        raise PreprodError(f"{name} contains no wheels")
    with tarfile.open(artifact, "r:gz") as archive:
        lock_handle = archive.extractfile(by_name["requirements.lock"])
        sbom_handle = archive.extractfile(by_name["sbom.json"])
        vulnerability_handle = archive.extractfile(by_name["vulnerability-report.json"])
        license_handle = archive.extractfile(by_name["licenses.json"])
        if any(
            handle is None
            for handle in (
                lock_handle,
                sbom_handle,
                vulnerability_handle,
                license_handle,
            )
        ):
            raise PreprodError(f"{name} metadata cannot be read")
        lock = lock_handle.read().decode("utf-8").splitlines()
        try:
            sbom = json.loads(sbom_handle.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PreprodError(f"{name} has an invalid SBOM") from error
        try:
            vulnerability_report = json.loads(
                vulnerability_handle.read().decode("utf-8")
            )
            license_report = json.loads(license_handle.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PreprodError(f"{name} has invalid scan evidence") from error
        dependencies = (
            vulnerability_report.get("dependencies")
            if isinstance(vulnerability_report, dict)
            else None
        )
        if not isinstance(dependencies, list):
            raise PreprodError(f"{name} vulnerability report is invalid")
        vulnerable = [
            item.get("name", "unknown")
            for item in dependencies
            if isinstance(item, dict) and item.get("vulns")
        ]
        if vulnerable:
            raise PreprodError(
                f"{name} contains vulnerable dependencies: {', '.join(vulnerable)}"
            )
        expected_files = {
            path.removeprefix("wheelhouse/") for path in wheel_members
        }
        if (
            not isinstance(license_report, dict)
            or license_report.get("schema") != "rapp-license-report/1"
            or license_report.get("blocked")
            or set(license_report.get("licenses", {})) != expected_files
        ):
            raise PreprodError(f"{name} license report is not approved")
        if (
            sbom.get("schema") != "rapp-dependency-materials/1"
            or sbom.get("platform") != expected_platform
            or not re.fullmatch(r"[0-9]+\.[0-9]+", str(sbom.get("python_version", "")))
            or not isinstance(sbom.get("architecture"), str)
            or not sbom["architecture"]
            or sbom.get("requirements") != lock
            or not isinstance(sbom.get("files"), dict)
        ):
            raise PreprodError(f"{name} SBOM does not match its lock")
        if set(sbom["files"]) != expected_files:
            raise PreprodError(f"{name} SBOM does not enumerate its wheelhouse")
        for path, member in wheel_members.items():
            handle = archive.extractfile(member)
            if handle is None:
                raise PreprodError(f"{name} cannot read {path}")
            filename = path.removeprefix("wheelhouse/")
            if hashlib.sha256(handle.read()).hexdigest() != sbom["files"][filename]:
                raise PreprodError(f"{name} wheel hash mismatch: {filename}")
    return sbom


def verify_candidate(
    artifact: Path,
    manifest_path: Path,
    policy_path: Path,
    now: datetime | None = None,
    expected_beta_commit: str | None = None,
    expected_qualification_run: str | None = None,
    allow_expired: bool = False,
    materials: dict[str, Path] | None = None,
) -> dict:
    manifest = _read_json(manifest_path)
    policy = _read_json(policy_path)
    _validate_policy(policy)
    if manifest.get("schema") != SCHEMA:
        raise PreprodError("unsupported readiness schema")
    if manifest.get("status") not in {"preprod-candidate", "seaworthy"}:
        raise PreprodError("readiness status is not deployable")
    if not isinstance(manifest.get("owner"), str) or not manifest["owner"].strip():
        raise PreprodError("readiness owner is missing")

    subject = manifest.get("subject")
    runtime = manifest.get("runtime")
    evidence = manifest.get("evidence")
    controls = manifest.get("policy")
    rollback = manifest.get("rollback")
    if not all(
        isinstance(item, dict)
        for item in (subject, runtime, evidence, controls, rollback)
    ):
        raise PreprodError("readiness manifest has invalid sections")
    if subject.get("artifact") != artifact.name:
        raise PreprodError("artifact filename does not match readiness manifest")
    digest = subject.get("artifact_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
        raise PreprodError("invalid artifact digest")
    if _sha256(artifact) != digest:
        raise PreprodError("artifact digest does not match readiness manifest")
    if artifact.stat().st_size != subject.get("size_bytes"):
        raise PreprodError("artifact size does not match readiness manifest")
    if artifact.stat().st_size > policy["max_artifact_bytes"]:
        raise PreprodError("artifact exceeds preprod policy")
    if subject.get("format") != "tar+gzip":
        raise PreprodError("unsupported artifact format")
    if not re.fullmatch(r"[0-9a-f]{64}", str(subject.get("brainstem_sha256", ""))):
        raise PreprodError("invalid brainstem digest")
    if subject.get("beta_repository") != BETA_REPOSITORY:
        raise PreprodError("readiness subject is not the Beta repository")
    if not re.fullmatch(r"[0-9a-f]{40}", str(subject.get("beta_commit", ""))):
        raise PreprodError("invalid Beta commit")
    if expected_beta_commit and subject["beta_commit"] != expected_beta_commit:
        raise PreprodError("readiness manifest targets a different Beta commit")
    for key in ("grail_base_commit", "expected_grail_tree"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(subject.get(key, ""))):
            raise PreprodError(f"invalid subject {key}")
    if (
        not isinstance(runtime.get("model_id"), str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}",
            runtime["model_id"],
        )
        or runtime["model_id"].lower() == "auto"
        or runtime.get("data_classification") != "synthetic"
    ):
        raise PreprodError("runtime configuration is not production-safe")

    qualification = evidence.get("qualification")
    beta_preflight = evidence.get("beta_preflight")
    soak = evidence.get("soak")
    control_plane = evidence.get("control_plane")
    if not all(
        isinstance(item, dict)
        for item in (qualification, beta_preflight, soak, control_plane)
    ):
        raise PreprodError("readiness evidence is incomplete")
    if (
        qualification.get("repository") != QUALIFICATION_REPOSITORY
        or qualification.get("workflow") != QUALIFICATION_WORKFLOW
        or not re.fullmatch(r"[0-9]+", str(qualification.get("run_id", "")))
    ):
        raise PreprodError("invalid qualification evidence")
    if (
        beta_preflight.get("repository") != BETA_REPOSITORY
        or beta_preflight.get("workflow") != BETA_PREFLIGHT_WORKFLOW
        or not re.fullmatch(r"[0-9]+", str(beta_preflight.get("run_id", "")))
    ):
        raise PreprodError("invalid Beta preflight evidence")
    if not str(soak.get("url", "")).startswith("https://github.com/"):
        raise PreprodError("invalid soak evidence")
    if (
        control_plane.get("repository") != QUALIFICATION_REPOSITORY
        or control_plane.get("workflow") != ".github/workflows/stage-preprod.yml"
        or not re.fullmatch(r"[0-9a-f]{40}", str(control_plane.get("commit", "")))
    ):
        raise PreprodError("invalid control-plane evidence")
    if (
        expected_qualification_run
        and qualification["run_id"] != expected_qualification_run
    ):
        raise PreprodError("readiness manifest references another qualification run")
    if evidence.get("required_checks") != policy["required_checks"]:
        raise PreprodError("readiness checks do not match current preprod policy")
    control_results = evidence.get("controls")
    if not isinstance(control_results, dict):
        raise PreprodError("readiness control results are missing")
    if set(control_results) != set(policy["required_checks"]):
        raise PreprodError("readiness control results do not match policy")
    for control, result in control_results.items():
        if not isinstance(result, dict):
            raise PreprodError(f"invalid control result: {control}")
        status = result.get("status")
        proof = result.get("evidence")
        if status not in {"passed", "pending", "failed", "unknown"}:
            raise PreprodError(f"invalid control status: {control}")
        if not isinstance(proof, list) or not all(
            isinstance(item, str) for item in proof
        ):
            raise PreprodError(f"invalid control evidence: {control}")
        if status == "passed" and not proof:
            raise PreprodError(f"passed control lacks evidence: {control}")
        if status in {"failed", "unknown"}:
            raise PreprodError(f"blocking control is {status}: {control}")
    if controls != {
        "same_artifact_to_grail": True,
        "human_approval_required": True,
    }:
        raise PreprodError("readiness policy controls are not enforced")
    if not isinstance(rollback.get("ref"), str) or not rollback["ref"].strip():
        raise PreprodError("rollback ref is missing")
    if not re.fullmatch(r"[0-9a-f]{40}", str(rollback.get("commit", ""))):
        raise PreprodError("rollback commit is invalid")
    for key in ("brainstem_sha256", "frame_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(rollback.get(key, ""))):
            raise PreprodError(f"rollback {key} is invalid")

    issued = _parse_time(str(manifest.get("issued_at", "")))
    expires = _parse_time(str(manifest.get("expires_at", "")))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires <= issued:
        raise PreprodError("readiness expiry must follow issuance")
    if current > expires and not allow_expired:
        raise PreprodError("preprod readiness has expired; re-qualify")
    if expires - issued > timedelta(hours=policy["max_candidate_age_hours"]):
        raise PreprodError("readiness lifetime exceeds policy")
    if manifest["status"] == "seaworthy":
        preprod = evidence.get("preprod")
        if (
            not isinstance(preprod, dict)
            or preprod.get("environment") != "preprod"
            or not re.fullmatch(r"[0-9]+", str(preprod.get("run_id", "")))
            or not str(preprod.get("url", "")).startswith("https://github.com/")
            or preprod.get("approval_authority") != "github-environment:preprod"
        ):
            raise PreprodError("seaworthy readiness lacks approved preprod evidence")
        sealed = _parse_time(str(manifest.get("sealed_at", "")))
        if not issued <= sealed <= expires:
            raise PreprodError("sealed_at falls outside the readiness lifetime")
        pending = [
            name for name, result in control_results.items()
            if result["status"] != "passed"
        ]
        if pending:
            raise PreprodError(
                "seaworthy readiness has incomplete controls: "
                + ", ".join(sorted(pending))
            )
        declared_materials = manifest.get("deployment_materials")
        if not isinstance(declared_materials, dict) or not declared_materials:
            raise PreprodError("seaworthy readiness has no deployment materials")
        if materials is None:
            raise PreprodError(
                "deployment materials are required to verify seaworthiness"
            )
        if _material_manifest(materials) != declared_materials:
            raise PreprodError(
                "deployment materials do not match readiness manifest"
            )
    members = _validate_archive(artifact)
    if len(members) > policy["max_files"]:
        raise PreprodError("artifact contains too many files")
    if sum(member.size for member in members) > policy["max_unpacked_bytes"]:
        raise PreprodError("artifact expands beyond preprod policy")
    with tarfile.open(artifact, "r:gz") as archive:
        try:
            member = archive.getmember("rapp_brainstem/brainstem.py")
            handle = archive.extractfile(member)
        except KeyError as error:
            raise PreprodError("artifact has no brainstem.py") from error
        if handle is None:
            raise PreprodError("cannot read artifact brainstem.py")
        if hashlib.sha256(handle.read()).hexdigest() != subject["brainstem_sha256"]:
            raise PreprodError("artifact brainstem.py does not match readiness manifest")
    return manifest


def seal_candidate(
    artifact: Path,
    manifest_path: Path,
    output_path: Path,
    policy_path: Path,
    preprod_run_id: str,
    preprod_run_url: str,
    approval_authority: str,
    materials: dict[str, Path],
    sealed_at: datetime | None = None,
) -> dict:
    if not re.fullmatch(r"[0-9]+", preprod_run_id):
        raise PreprodError("preprod run id must be numeric")
    if not preprod_run_url.startswith("https://github.com/"):
        raise PreprodError("preprod run URL must be a GitHub HTTPS URL")
    if approval_authority != "github-environment:preprod":
        raise PreprodError("invalid preprod approval authority")
    now = (sealed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        now=now,
    )
    if manifest["status"] != "preprod-candidate":
        raise PreprodError("only a preprod candidate can be sealed")
    sealed = json.loads(json.dumps(manifest))
    sealed["status"] = "seaworthy"
    sealed["sealed_at"] = _format_time(now)
    sealed["deployment_materials"] = _material_manifest(materials)
    sealed["evidence"]["preprod"] = {
        "environment": "preprod",
        "run_id": preprod_run_id,
        "url": preprod_run_url,
        "approval_authority": approval_authority,
    }
    for result in sealed["evidence"]["controls"].values():
        if result["status"] == "pending":
            result["status"] = "passed"
            result["evidence"] = [preprod_run_url]
    _write_json(output_path, sealed)
    verify_candidate(
        artifact,
        output_path,
        policy_path,
        now=now,
        materials=materials,
    )
    return sealed


def export_candidate(
    artifact: Path,
    manifest_path: Path,
    rollback_frame_path: Path,
    target: Path,
    policy_path: Path,
    now: datetime | None = None,
    verify_provenance: bool = True,
    materials: dict[str, Path] | None = None,
) -> int:
    manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        now=now,
        materials=materials,
    )
    if manifest["status"] != "seaworthy":
        raise PreprodError("only a seaworthy artifact can be exported to Grail")
    target = target.resolve()
    top = Path(_git(target, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != target:
        raise PreprodError("Grail target must be its repository root")
    origin = _git(target, "remote", "get-url", "origin").strip()
    if _repo_slug(origin) != "kody-w/rapp-installer":
        raise PreprodError("export target is not kody-w/rapp-installer")
    branch = _git(target, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch in {"main", "HEAD"}:
        raise PreprodError("Grail export requires a release branch")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PreprodError("Grail target must be clean")
    current_commit = _git(target, "rev-parse", "HEAD^{commit}").strip()
    if current_commit != manifest["subject"]["grail_base_commit"]:
        raise PreprodError(
            "Grail base moved since Preprod; rebuild the candidate from current Grail"
        )
    rollback_frame = _read_json(rollback_frame_path)
    try:
        brainstem_history.verify_frame(target, rollback_frame_path)
    except brainstem_history.HistoryError as error:
        raise PreprodError(f"rollback Brainstem frame is invalid: {error}") from error
    if (
        rollback_frame["release_ref"] != manifest["rollback"]["ref"]
        or rollback_frame["commit"] != manifest["rollback"]["commit"]
        or rollback_frame["brainstem"]["sha256"]
        != manifest["rollback"]["brainstem_sha256"]
        or brainstem_history.frame_sha256(rollback_frame)
        != manifest["rollback"]["frame_sha256"]
    ):
        raise PreprodError("rollback Brainstem frame does not match readiness manifest")
    if verify_provenance:
        _verify_github_provenance(
            manifest,
            (artifact, manifest_path, *(materials or {}).values()),
        )

    members = _validate_archive(artifact)
    candidate_paths = {member.name for member in members}
    tracked = {
        item for item in _git(target, "ls-files", "-z").split("\0") if item
    }
    changed = 0
    for relative in sorted(tracked - candidate_paths):
        destination = target / relative
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
            _git(target, "update-index", "--force-remove", "--", relative)
            changed += 1
        elif destination.exists():
            raise PreprodError(f"cannot replace non-file Grail path: {relative}")

    with tarfile.open(artifact, "r:gz") as archive:
        for member in members:
            destination = target / member.name
            current = destination.parent
            while current != target:
                if current.is_symlink():
                    raise PreprodError(
                        f"Grail target parent is a symlink: {current.relative_to(target)}"
                    )
                if current.exists() and not current.is_dir():
                    raise PreprodError(
                        f"Grail target parent is not a directory: {current.relative_to(target)}"
                    )
                current = current.parent
            if destination.is_symlink() or destination.is_dir():
                raise PreprodError(f"cannot overwrite non-file Grail path: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise PreprodError(f"cannot read artifact member: {member.name}")
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            try:
                payload = source.read()
                with temporary.open("wb") as handle:
                    handle.write(payload)
                os.chmod(
                    temporary,
                    0o755 if member.mode & stat.S_IXUSR else 0o644,
                )
                os.replace(temporary, destination)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            object_id = _hash_object(target, payload)
            mode = "100755" if member.mode & stat.S_IXUSR else "100644"
            _git(
                target,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                object_id,
                member.name,
            )
            changed += 1
    actual_tree = _git(target, "write-tree").strip()
    if actual_tree != manifest["subject"]["expected_grail_tree"]:
        raise PreprodError(
            f"staged Grail tree {actual_tree} does not match sealed "
            f"{manifest['subject']['expected_grail_tree']}"
        )
    return changed


def verify_staged_tree(manifest_path: Path, target: Path) -> str:
    manifest = _read_json(manifest_path)
    if manifest.get("status") != "seaworthy":
        raise PreprodError("only a seaworthy manifest can authorize a Grail tree")
    target = target.resolve()
    top = Path(_git(target, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != target:
        raise PreprodError("Grail target must be its repository root")
    if _repo_slug(_git(target, "remote", "get-url", "origin").strip()) != "kody-w/rapp-installer":
        raise PreprodError("target is not the Grail repository")
    if _git(target, "rev-parse", "--abbrev-ref", "HEAD").strip() in {"main", "HEAD"}:
        raise PreprodError("Grail verification requires a release branch")
    if _git(target, "rev-parse", "HEAD^{commit}").strip() != manifest["subject"]["grail_base_commit"]:
        raise PreprodError("Grail base moved since Preprod")
    unstaged = subprocess.run(
        ["git", "-C", str(target), "diff", "--quiet"],
        check=False,
    )
    if unstaged.returncode != 0:
        raise PreprodError("Grail worktree has unstaged changes after export")
    untracked = _git(
        target,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if untracked:
        raise PreprodError("Grail worktree has untracked files after export")
    tree = _git(target, "write-tree").strip()
    if tree != manifest["subject"]["expected_grail_tree"]:
        raise PreprodError("staged Grail tree no longer matches sealed Preprod")
    return tree


def _extract_archive(artifact: Path, destination: Path) -> None:
    members = _validate_archive(artifact)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(artifact, "r:gz") as archive:
        for member in members:
            output = destination / member.name
            output.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise PreprodError(f"cannot read artifact member: {member.name}")
            with output.open("wb") as handle:
                while chunk := source.read(1024 * 1024):
                    handle.write(chunk)
            os.chmod(output, 0o755 if member.mode & stat.S_IXUSR else 0o644)


def prepare_runtime(
    artifact: Path,
    manifest_path: Path,
    destination: Path,
    state_dir: Path,
    policy_path: Path,
    materials: dict[str, Path],
    platform_name: str | None = None,
    verify_provenance: bool = True,
    install_dependencies: bool = True,
) -> dict:
    manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        materials=materials,
    )
    if manifest["status"] != "seaworthy":
        raise PreprodError("only a seaworthy artifact can prepare a runtime")
    if verify_provenance:
        _verify_github_provenance(
            manifest,
            (artifact, manifest_path, *materials.values()),
        )

    platform_key = platform_name or {
        "linux": "linux",
        "darwin": "macos",
        "win32": "windows",
    }.get(sys.platform)
    material_name = f"dependency-material-{platform_key}"
    if material_name not in materials:
        raise PreprodError(f"no sealed dependency material for {platform_key}")
    destination = destination.resolve()
    state_dir = state_dir.resolve()
    if destination.exists():
        raise PreprodError("runtime destination must not already exist")
    try:
        state_dir.relative_to(destination)
    except ValueError:
        pass
    else:
        raise PreprodError("runtime state must live outside the release directory")
    state_dir.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.prepare")
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        source_dir = temporary / "src"
        dependencies = temporary / "dependencies"
        _extract_archive(artifact, source_dir)
        _extract_archive(materials[material_name], dependencies)
        lock = dependencies / "requirements.lock"
        wheelhouse = dependencies / "wheelhouse"
        if not lock.is_file() or not wheelhouse.is_dir():
            raise PreprodError("sealed dependency material is incomplete")
        sbom = json.loads((dependencies / "sbom.json").read_text(encoding="utf-8"))
        current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        current_arch = platform.machine().lower()
        if sbom.get("python_version") != current_python:
            raise PreprodError(
                f"dependency material requires Python {sbom.get('python_version')}, "
                f"current runtime is {current_python}"
            )
        if str(sbom.get("architecture", "")).lower() != current_arch:
            raise PreprodError(
                f"dependency material requires {sbom.get('architecture')}, "
                f"current architecture is {current_arch}"
            )

        if install_dependencies:
            venv = temporary / "venv"
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise PreprodError(f"cannot create sealed runtime venv: {result.stderr}")
            python = (
                venv / "Scripts" / "python.exe"
                if platform_key == "windows"
                else venv / "bin" / "python"
            )
            result = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheelhouse),
                    "-r",
                    str(lock),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise PreprodError(
                    "cannot install sealed dependencies: " + result.stderr.strip()
                )

        _write_json(
            temporary / "deployment.json",
            {
                "schema": "rapp-seaworthy-deployment/1",
                "artifact_sha256": manifest["subject"]["artifact_sha256"],
                "brainstem_sha256": manifest["subject"]["brainstem_sha256"],
                "material": material_name,
                "material_sha256": manifest["deployment_materials"][material_name]["sha256"],
                "model_id": manifest["runtime"]["model_id"],
                "state_dir": str(state_dir),
            },
        )
        (temporary / "runtime.env").write_text(
            f"BRAINSTEM_STATE_DIR={state_dir}\n"
            f"GITHUB_MODEL={manifest['runtime']['model_id']}\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {
        "source": destination / "src",
        "venv": destination / "venv",
        "deployment": destination / "deployment.json",
    }


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy",
        type=Path,
        default=root.parent / "preprod-policy.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    package = subparsers.add_parser("package")
    package.add_argument("--source", type=Path, required=True)
    package.add_argument("--artifact", type=Path, required=True)
    package.add_argument("--manifest", type=Path, required=True)
    package.add_argument("--beta-commit", required=True)
    package.add_argument("--qualification-run-id", required=True)
    package.add_argument("--qualification-url", required=True)
    package.add_argument("--beta-preflight-run-id", required=True)
    package.add_argument("--beta-preflight-url", required=True)
    package.add_argument("--soak-evidence-url", required=True)
    package.add_argument("--owner", required=True)
    package.add_argument("--control-plane-commit", required=True)
    package.add_argument("--model-id", required=True)
    package.add_argument("--rollback-ref", required=True)
    package.add_argument("--rollback-frame", type=Path, required=True)
    package.add_argument("--expires-hours", type=int)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--artifact", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-beta-commit")
    verify.add_argument("--expected-qualification-run")
    verify.add_argument("--allow-expired", action="store_true")
    verify.add_argument("--material", action="append", default=[])

    seal = subparsers.add_parser("seal")
    seal.add_argument("--artifact", type=Path, required=True)
    seal.add_argument("--manifest", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)
    seal.add_argument("--preprod-run-id", required=True)
    seal.add_argument("--preprod-run-url", required=True)
    seal.add_argument("--approval-authority", required=True)
    seal.add_argument("--material", action="append", default=[])

    export = subparsers.add_parser("export")
    export.add_argument("--artifact", type=Path, required=True)
    export.add_argument("--manifest", type=Path, required=True)
    export.add_argument("--rollback-frame", type=Path, required=True)
    export.add_argument("--target", type=Path, required=True)
    export.add_argument("--material", action="append", default=[])

    verify_tree = subparsers.add_parser("verify-staged-tree")
    verify_tree.add_argument("--manifest", type=Path, required=True)
    verify_tree.add_argument("--target", type=Path, required=True)

    bundle = subparsers.add_parser("bundle")
    bundle.add_argument("--source", type=Path, required=True)
    bundle.add_argument("--artifact", type=Path, required=True)

    prepare = subparsers.add_parser("prepare-runtime")
    prepare.add_argument("--artifact", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--destination", type=Path, required=True)
    prepare.add_argument("--state-dir", type=Path, required=True)
    prepare.add_argument("--platform", choices=("linux", "macos", "windows"))
    prepare.add_argument("--material", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == "package":
            manifest = package_candidate(
                args.source.resolve(),
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.policy.resolve(),
                args.beta_commit,
                args.qualification_run_id,
                args.qualification_url,
                args.beta_preflight_run_id,
                args.beta_preflight_url,
                args.soak_evidence_url,
                args.owner,
                args.control_plane_commit,
                args.model_id,
                args.rollback_ref,
                args.rollback_frame.resolve(),
                expires_hours=args.expires_hours,
            )
            print(
                "PREPROD CANDIDATE — "
                f"{manifest['subject']['artifact_sha256']} "
                f"(beta {manifest['subject']['beta_commit'][:12]})"
            )
        elif args.command == "verify":
            manifest = verify_candidate(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.policy.resolve(),
                expected_beta_commit=args.expected_beta_commit,
                expected_qualification_run=args.expected_qualification_run,
                allow_expired=args.allow_expired,
                materials=_parse_material_specs(args.material) if args.material else None,
            )
            print(
                "SEAWORTHINESS VERIFIED — "
                f"{manifest['subject']['artifact_sha256']} "
                f"(expires {manifest['expires_at']})"
            )
        elif args.command == "seal":
            manifest = seal_candidate(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.output.resolve(),
                args.policy.resolve(),
                args.preprod_run_id,
                args.preprod_run_url,
                args.approval_authority,
                _parse_material_specs(args.material),
            )
            print(
                "SEAWORTHY — "
                f"{manifest['subject']['artifact_sha256']} "
                f"({manifest['evidence']['preprod']['approval_authority']})"
            )
        elif args.command == "export":
            changed = export_candidate(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.rollback_frame.resolve(),
                args.target.resolve(),
                args.policy.resolve(),
                materials=_parse_material_specs(args.material),
            )
            print(f"GRAIL HANDOFF — {changed} paths staged from exact preprod artifact")
        elif args.command == "verify-staged-tree":
            tree = verify_staged_tree(
                args.manifest.resolve(),
                args.target.resolve(),
            )
            print(f"GRAIL TREE VERIFIED — {tree}")
        elif args.command == "bundle":
            digest = build_artifact(args.source.resolve(), args.artifact.resolve())
            print(f"DEPLOYMENT MATERIAL — {digest} ({args.artifact.name})")
        else:
            result = prepare_runtime(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.destination.resolve(),
                args.state_dir.resolve(),
                args.policy.resolve(),
                _parse_material_specs(args.material),
                platform_name=args.platform,
            )
            print(f"SEALED RUNTIME — source={result['source']} venv={result['venv']}")
    except (OSError, PreprodError) as error:
        print(f"preprod gate failed: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
