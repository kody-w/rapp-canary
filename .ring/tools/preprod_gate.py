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
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
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
PREPROD_CERT_IDENTITY = (
    "https://github.com/kody-w/rapp-canary/"
    ".github/workflows/stage-preprod.yml@refs/heads/main"
)
RAPP1_AUTHORITY_PIN = {
    "repository": "kody-w/rapp-1",
    "revision": "rev-10",
    "commit": "be742eb97c36a705df0ee250e163e20c6d1cee76",
    "spec_path": "SPEC.md",
    "spec_sha256": "78f9cae4cbed02c7b5cd2a648350ccb4ffeacebb49fcbbe34ac6c80a4e507cf0",
    "constitution_path": "CONSTITUTION.md",
    "constitution_sha256": "6ea14f72152892120e2192670331235be3808c436dd1e37aa00f754695a0cbf5",
    "immutable_grail_section": "11.1",
    "constitutional_article": "15",
}
GRAIL_KERNEL_PIN = {
    "repository": "kody-w/rapp-installer",
    "release_scope": "https://github.com/kody-w/rapp-canary",
    "grail_id": "grail:1a501dd7a01f05698abcf5f9bbe0273ebb9d09f5d6ec444aa71edccff947c8c7",
    "immutable_ref": "refs/tags/brainstem-v0.6.16",
    "object_format": "sha1",
    "commit": "5fbde1776a72715935c3d597a9ddfce28a04032b",
    "path": "rapp_brainstem/brainstem.py",
    "mode": "100644",
    "blob": "3f7102ff508c813bb6494511fc32a421a633e418",
    "sha256": "bd55a7f0bcf5efd3f7966ca39bb146da3c25fda9a0b1ce5ba587919d3c3775f4",
    "size_bytes": 154059,
    "policy": "immutable-forever",
}
REQUIRED_MATERIALS = {
    "dependency-material-linux",
    "dependency-material-macos",
    "dependency-material-windows",
}
MAX_ARCHIVE_BYTES = 104857600
MAX_ARCHIVE_FILES = 10000
MAX_ARCHIVE_UNPACKED_BYTES = 536870912
MAX_CANDIDATE_AGE_HOURS = 168
MINIMUM_SOAK_MINUTES = 15
REQUIRED_CONTROL_CHECKS = (
    "pre-grail-attestation-chain",
    "rapp1-immutable-grail-law",
    "beta-main-preflight",
    "immutable-artifact",
    "immutable-grail-kernel",
    "critical-brainstem-hash",
    "explicit-model-evidence",
    "sealed-dependency-materials",
    "vulnerability-scan",
    "license-scan",
    "cross-platform-artifact-verification",
    "fresh-install",
    "upgrade",
    "destructive-repair",
    "live-writer-quiescence",
    "unrelated-process-survival",
    "real-auth-soak",
    "brainstem-rollback-frame",
    "rollback-ready",
    "protected-environment-approval",
)
SOAK_URL_PATTERN = re.compile(
    r"https://raw\.githubusercontent\.com/kody-w/rapp-canary/"
    r"(?P<commit>[0-9a-f]{40})/\.ring/soak/[A-Za-z0-9._-]+\.json"
)
REQUIREMENT_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*\])?"
    r"(?:\s*(?:===|==|~=|!=|<=|>=|<|>)\s*"
    r"[A-Za-z0-9](?:[A-Za-z0-9.*+!_-]*[A-Za-z0-9*+!_-])?"
    r"(?:\s*,\s*(?:===|==|~=|!=|<=|>=|<|>)\s*"
    r"[A-Za-z0-9](?:[A-Za-z0-9.*+!_-]*[A-Za-z0-9*+!_-])?)*"
    r")?"
)
LOCK_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"==[A-Za-z0-9](?:[A-Za-z0-9.*+!_-]*[A-Za-z0-9*+!_-])?"
)
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}
KERNEL_BOOTSTRAP = """\
import os
import sys

path = sys.argv[1]
source_dir = os.path.dirname(path)
os.chdir(source_dir)
sys.path.insert(0, source_dir)
source = sys.stdin.buffer.read()
sys.argv = [path]
namespace = {
    "__name__": "__main__",
    "__file__": path,
    "__package__": None,
    "__cached__": None,
}
exec(compile(source, path, "exec"), namespace, namespace)
"""


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


def _grail_id(payload: bytes) -> str:
    return "grail:" + hashlib.sha256(b"rapp/1:grail\n" + payload).hexdigest()


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


def _requirement_lines(path: Path, *, exact: bool = False) -> list[str]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PreprodError(f"cannot read requirements file {path}: {error}") from error
    pattern = LOCK_PATTERN if exact else REQUIREMENT_PATTERN
    requirements = []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not pattern.fullmatch(line):
            raise PreprodError(
                f"requirements must use registry package names and "
                f"{'exact pins' if exact else 'version specifiers'} only: {line}"
            )
        requirements.append(line)
    if not requirements:
        raise PreprodError(f"requirements file is empty: {path}")
    return requirements


def _validate_soak_evidence(
    evidence: dict,
    *,
    beta_commit: str,
    qualification_commit: str,
    qualification_run_id: str,
    model_id: str,
    policy: dict,
    reference_time: datetime,
) -> tuple[datetime, datetime]:
    expected_keys = {
        "schema",
        "result",
        "canary_commit",
        "beta_commit",
        "qualification_run_id",
        "model_id",
        "started_at",
        "completed_at",
        "probe_interval_seconds",
        "health_probe_count",
        "authenticated_chat_count",
        "authenticated_chat_times",
        "probes",
        "checks",
    }
    expected_checks = {
        "authenticated_chat": True,
        "state_isolated": True,
        "health_stable": True,
        "no_critical_events": True,
    }
    if (
        set(evidence) != expected_keys
        or evidence.get("schema") != "rapp/1:soak"
        or evidence.get("result") != "passed"
        or evidence.get("canary_commit") != qualification_commit
        or evidence.get("beta_commit") != beta_commit
        or evidence.get("qualification_run_id") != qualification_run_id
        or evidence.get("model_id") != model_id
        or evidence.get("checks") != expected_checks
    ):
        raise PreprodError("soak evidence does not match the candidate and model")
    started = _parse_time(str(evidence.get("started_at", "")))
    completed = _parse_time(str(evidence.get("completed_at", "")))
    if completed <= started:
        raise PreprodError("soak evidence has an invalid duration")
    if completed - started < timedelta(minutes=policy["minimum_soak_minutes"]):
        raise PreprodError("soak evidence is shorter than policy")
    probe_interval = evidence.get("probe_interval_seconds")
    health_probe_count = evidence.get("health_probe_count")
    probes = evidence.get("probes")
    chat_times = evidence.get("authenticated_chat_times")
    if (
        not isinstance(probe_interval, int)
        or not 15 <= probe_interval <= 300
        or not isinstance(health_probe_count, int)
        or not isinstance(probes, list)
        or health_probe_count != len(probes)
        or health_probe_count < 2
        or evidence.get("authenticated_chat_count") != 2
        or not isinstance(chat_times, list)
        or len(chat_times) != 2
    ):
        raise PreprodError("soak evidence does not cover the authenticated interval")
    probe_times = []
    for probe in probes:
        if (
            not isinstance(probe, dict)
            or set(probe) != {"at", "status", "model_id"}
            or probe.get("status") != "ok"
            or probe.get("model_id") != model_id
        ):
            raise PreprodError("soak evidence contains an unhealthy probe")
        probe_times.append(_parse_time(str(probe.get("at", ""))))
    if probe_times != sorted(probe_times):
        raise PreprodError("soak evidence probes are not ordered")
    if (
        probe_times[0] < started
        or probe_times[-1] != completed
        or probe_times[0] - started
        > max(timedelta(minutes=3), timedelta(seconds=probe_interval * 2))
        or any(
            right - left > timedelta(seconds=probe_interval * 2)
            for left, right in zip(probe_times, probe_times[1:])
        )
    ):
        raise PreprodError("soak evidence has a gap in health coverage")
    parsed_chat_times = [_parse_time(str(value)) for value in chat_times]
    if (
        parsed_chat_times[0] < started
        or parsed_chat_times[0] > completed
        or parsed_chat_times[0] - started > timedelta(minutes=3)
        or parsed_chat_times[1] != completed
        or parsed_chat_times != sorted(parsed_chat_times)
    ):
        raise PreprodError("soak evidence does not bind both authenticated chats")
    if completed > reference_time:
        raise PreprodError("soak evidence is future-dated")
    if reference_time - completed > timedelta(hours=policy["max_candidate_age_hours"]):
        raise PreprodError("soak evidence is stale")
    return started, completed


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


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise PreprodError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
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
                "--cert-identity",
                PREPROD_CERT_IDENTITY,
                "--source-ref",
                "refs/heads/main",
                "--source-digest",
                manifest["evidence"]["control_plane"]["commit"],
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
    soak_minutes = policy.get("minimum_soak_minutes")
    if not isinstance(soak_minutes, int) or soak_minutes < MINIMUM_SOAK_MINUTES:
        raise PreprodError("invalid minimum_soak_minutes")
    if policy.get("same_artifact_to_grail") is not True:
        raise PreprodError("policy must require the same artifact to reach Grail")
    if policy.get("human_approval_required") is not True:
        raise PreprodError("policy must require human approval")
    if policy.get("require_explicit_model_evidence") is not True:
        raise PreprodError("policy must require explicit model evidence")
    authority = policy.get("rapp1_authority")
    if authority != RAPP1_AUTHORITY_PIN:
        raise PreprodError("invalid RAPP/1 immutable Grail authority")
    kernel = policy.get("grail_kernel")
    if kernel != GRAIL_KERNEL_PIN:
        raise PreprodError("invalid immutable Grail kernel policy")
    age = policy.get("max_candidate_age_hours")
    if not isinstance(age, int) or not 1 <= age <= MAX_CANDIDATE_AGE_HOURS:
        raise PreprodError("invalid max_candidate_age_hours")
    if soak_minutes > age * 60:
        raise PreprodError("minimum soak duration exceeds candidate lifetime")
    hard_limits = {
        "max_artifact_bytes": MAX_ARCHIVE_BYTES,
        "max_unpacked_bytes": MAX_ARCHIVE_UNPACKED_BYTES,
        "max_files": MAX_ARCHIVE_FILES,
    }
    for key, hard_limit in hard_limits.items():
        value = policy.get(key)
        if not isinstance(value, int) or not 1 <= value <= hard_limit:
            raise PreprodError(f"invalid {key}")
    checks = policy.get("required_checks")
    if checks != list(REQUIRED_CONTROL_CHECKS):
        raise PreprodError("invalid required_checks")


def verify_grail_kernel_bytes(repo: Path, policy: dict) -> dict:
    _validate_policy(policy)
    kernel = policy["grail_kernel"]
    repo = repo.resolve()
    path = repo / kernel["path"]
    if not path.is_file():
        raise PreprodError("kernel-drift: candidate Brainstem is missing")
    payload = path.read_bytes()
    if (
        _sha256(path) != kernel["sha256"]
        or _grail_id(payload) != kernel["grail_id"]
        or len(payload) != kernel["size_bytes"]
    ):
        raise PreprodError("kernel-drift: candidate Brainstem differs from Grail")
    index_entry = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "--", kernel["path"]],
        capture_output=True,
        text=True,
        check=False,
    )
    if index_entry.returncode == 0 and index_entry.stdout.strip():
        match = re.fullmatch(
            r"(?P<mode>[0-9]{6}) (?P<blob>[0-9a-f]+) [0-3]\t"
            + re.escape(kernel["path"]),
            index_entry.stdout.strip(),
        )
        if (
            not match
            or match.group("mode") != kernel["mode"]
            or match.group("blob") != kernel["blob"]
        ):
            raise PreprodError(
                "kernel-drift: candidate Brainstem Git mode or blob differs"
            )
    elif os.name != "nt":
        executable = bool(path.stat().st_mode & stat.S_IXUSR)
        if executable != (kernel["mode"] == "100755"):
            raise PreprodError("kernel-drift: candidate Brainstem mode differs")
    return kernel


def verify_grail_kernel(repo: Path, policy: dict) -> dict:
    kernel = verify_grail_kernel_bytes(repo, policy)
    origin = _git(repo, "remote", "get-url", "origin").strip()
    if _repo_slug(origin) != kernel["repository"]:
        raise PreprodError("kernel verification target is not the Grail repository")
    release_commit = _git(
        repo,
        "rev-parse",
        f"{kernel['immutable_ref']}^{{commit}}",
    ).strip()
    if release_commit != kernel["commit"]:
        raise PreprodError("immutable Grail release ref moved")
    tree_entry = _git(
        repo,
        "ls-tree",
        kernel["commit"],
        "--",
        kernel["path"],
    ).strip()
    match = re.fullmatch(
        r"(?P<mode>[0-9]{6}) blob (?P<blob>[0-9a-f]+)\t"
        + re.escape(kernel["path"]),
        tree_entry,
    )
    if (
        not match
        or match.group("mode") != kernel["mode"]
        or match.group("blob") != kernel["blob"]
    ):
        raise PreprodError("immutable Grail path does not resolve to its pinned blob")
    release_blob = match.group("blob")
    payload = _git_bytes(repo, "cat-file", "blob", release_blob)
    if (
        hashlib.sha256(payload).hexdigest() != kernel["sha256"]
        or _grail_id(payload) != kernel["grail_id"]
        or len(payload) != kernel["size_bytes"]
    ):
        raise PreprodError("immutable Grail bytes do not match their constitutional pin")
    return kernel


def package_candidate(
    source: Path,
    artifact: Path,
    manifest_path: Path,
    policy_path: Path,
    beta_commit: str,
    qualification_run_id: str,
    qualification_url: str,
    qualification_commit: str,
    beta_preflight_run_id: str,
    beta_preflight_url: str,
    soak_evidence_path: Path,
    soak_evidence_url: str,
    soak_evidence_sha256: str,
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
    if not re.fullmatch(r"[0-9a-f]{40}", qualification_commit):
        raise PreprodError("qualification commit must be a full lowercase SHA")
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
    ):
        if not value.startswith("https://github.com/"):
            raise PreprodError(f"{label} must be a GitHub HTTPS URL")
    if qualification_url != (
        f"https://github.com/{QUALIFICATION_REPOSITORY}/actions/runs/"
        f"{qualification_run_id}"
    ):
        raise PreprodError("qualification URL does not match its run id")
    if beta_preflight_url != (
        f"https://github.com/{BETA_REPOSITORY}/actions/runs/{beta_preflight_run_id}"
    ):
        raise PreprodError("Beta preflight URL does not match its run id")
    if not SOAK_URL_PATTERN.fullmatch(soak_evidence_url):
        raise PreprodError("soak evidence must be a commit-pinned Canary raw URL")
    if not re.fullmatch(r"[0-9a-f]{64}", soak_evidence_sha256):
        raise PreprodError("invalid soak evidence digest")
    if _sha256(soak_evidence_path) != soak_evidence_sha256:
        raise PreprodError("soak evidence digest does not match")

    policy = _read_json(policy_path)
    _validate_policy(policy)
    try:
        manifest_path.resolve().relative_to(source.resolve())
    except ValueError:
        pass
    else:
        raise PreprodError("readiness manifest must live outside the candidate tree")
    lifetime = (
        policy["max_candidate_age_hours"]
        if expires_hours is None
        else expires_hours
    )
    if not isinstance(lifetime, int) or not 1 <= lifetime <= policy["max_candidate_age_hours"]:
        raise PreprodError("candidate lifetime exceeds preprod policy")
    now = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    soak_evidence = _read_json(soak_evidence_path)
    soak_started, soak_completed = _validate_soak_evidence(
        soak_evidence,
        beta_commit=beta_commit,
        qualification_commit=qualification_commit,
        qualification_run_id=qualification_run_id,
        model_id=model_id,
        policy=policy,
        reference_time=now,
    )
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
    rollback_history = rollback_frame_path.parent
    history_count, history_tip = brainstem_history.verify_chain(
        source,
        rollback_history,
    )
    origin = _git(source, "remote", "get-url", "origin").strip()
    if _repo_slug(origin) != "kody-w/rapp-installer":
        raise PreprodError("candidate is not a Grail-shaped repository")
    grail_kernel = verify_grail_kernel(source, policy)
    grail_base_commit = _git(source, "rev-parse", "HEAD^{commit}").strip()
    expected_tree = _git(source, "write-tree").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", grail_base_commit):
        raise PreprodError("invalid Grail base commit")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_tree):
        raise PreprodError("invalid expected Grail tree")

    artifact_sha256 = build_artifact(source, artifact)
    if artifact.stat().st_size > policy["max_artifact_bytes"]:
        raise PreprodError("candidate artifact exceeds preprod policy")
    expires = now + timedelta(hours=lifetime)
    passed_controls = {
        "pre-grail-attestation-chain": [qualification_url],
        "rapp1-immutable-grail-law": [
            "https://github.com/kody-w/rapp-1/blob/"
            f"{policy['rapp1_authority']['commit']}/SPEC.md#111-immutable-grail-kernel-conformance"
        ],
        "beta-main-preflight": [beta_preflight_url],
        "immutable-artifact": [f"sha256:{artifact_sha256}"],
        "critical-brainstem-hash": [f"sha256:{_sha256(brainstem_path)}"],
        "immutable-grail-kernel": [
            f"{grail_kernel['immutable_ref']}:{grail_kernel['sha256']}"
        ],
        "explicit-model-evidence": [f"model:{model_id}"],
        "real-auth-soak": [f"sha256:{soak_evidence_sha256}", soak_evidence_url],
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
            "grail_id": grail_kernel["grail_id"],
            "release_scope": grail_kernel["release_scope"],
            "grail_kernel_ref": grail_kernel["immutable_ref"],
            "grail_kernel_commit": grail_kernel["commit"],
            "grail_base_commit": grail_base_commit,
            "expected_grail_tree": expected_tree,
            "beta_repository": BETA_REPOSITORY,
            "beta_commit": beta_commit,
        },
        "runtime": {
            "model_id": model_id,
            "model_policy": "observed-at-qualification",
            "data_classification": "synthetic",
            "kernel_entrypoint": grail_kernel["path"],
            "grail_id": grail_kernel["grail_id"],
            "release_scope": grail_kernel["release_scope"],
        },
        "evidence": {
            "qualification": {
                "repository": QUALIFICATION_REPOSITORY,
                "workflow": QUALIFICATION_WORKFLOW,
                "run_id": qualification_run_id,
                "url": qualification_url,
                "commit": qualification_commit,
            },
            "beta_preflight": {
                "repository": BETA_REPOSITORY,
                "workflow": BETA_PREFLIGHT_WORKFLOW,
                "run_id": beta_preflight_run_id,
                "url": beta_preflight_url,
            },
            "soak": {
                "schema": soak_evidence["schema"],
                "result": soak_evidence["result"],
                "url": soak_evidence_url,
                "sha256": soak_evidence_sha256,
                "started_at": _format_time(soak_started),
                "completed_at": _format_time(soak_completed),
                "canary_commit": qualification_commit,
                "beta_commit": beta_commit,
                "qualification_run_id": qualification_run_id,
                "model_id": model_id,
                "probe_interval_seconds": soak_evidence["probe_interval_seconds"],
                "health_probe_count": soak_evidence["health_probe_count"],
                "authenticated_chat_count": soak_evidence[
                    "authenticated_chat_count"
                ],
                "authenticated_chat_times": soak_evidence[
                    "authenticated_chat_times"
                ],
                "probes": soak_evidence["probes"],
                "checks": soak_evidence["checks"],
            },
            "control_plane": {
                "repository": QUALIFICATION_REPOSITORY,
                "commit": control_plane_commit,
                "workflow": ".github/workflows/stage-preprod.yml",
            },
            "rapp1_authority": policy["rapp1_authority"],
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
            "history_sha256": brainstem_history.history_sha256(rollback_history),
            "history_count": history_count,
            "history_tip": history_tip["release_ref"],
        },
        "issued_at": _format_time(now),
        "expires_at": _format_time(expires),
    }
    _write_json(manifest_path, manifest)
    return manifest


def _validate_archive(artifact: Path) -> list[tarfile.TarInfo]:
    try:
        if artifact.stat().st_size > MAX_ARCHIVE_BYTES:
            raise PreprodError("archive exceeds the hard compressed-size limit")
        with tarfile.open(artifact, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_FILES:
                raise PreprodError("archive contains too many files")
            if sum(member.size for member in members) > MAX_ARCHIVE_UNPACKED_BYTES:
                raise PreprodError("archive exceeds the hard expanded-size limit")
            seen = set()
            normalized_seen = set()
            for member in members:
                path = PurePosixPath(member.name)
                normalized_parts = []
                unsafe_component = False
                for part in path.parts:
                    normalized = unicodedata.normalize("NFC", part).casefold()
                    basename = normalized.split(".", 1)[0]
                    if (
                        not part
                        or part != part.rstrip(" .")
                        or any(ord(character) < 32 or ord(character) == 127 for character in part)
                        or any(character in '<>:"|?*' for character in part)
                        or basename in WINDOWS_RESERVED_NAMES
                        or len(part.encode("utf-8")) > 255
                    ):
                        unsafe_component = True
                    normalized_parts.append(normalized)
                normalized_name = "/".join(normalized_parts)
                if (
                    not member.name
                    or "\\" in member.name
                    or path.is_absolute()
                    or not path.parts
                    or "." in path.parts
                    or ".." in path.parts
                    or member.name != path.as_posix()
                    or len(member.name.encode("utf-8")) > 4096
                    or ".git" in normalized_parts
                    or unsafe_component
                    or member.issym()
                    or member.islnk()
                    or not member.isfile()
                    or member.size < 0
                ):
                    raise PreprodError(f"unsafe artifact member: {member.name}")
                if member.name in seen:
                    raise PreprodError(f"duplicate artifact member: {member.name}")
                if normalized_name in normalized_seen:
                    raise PreprodError(
                        f"cross-platform duplicate artifact member: {member.name}"
                    )
                seen.add(member.name)
                normalized_seen.add(normalized_name)
            return members
    except (OSError, tarfile.TarError) as error:
        raise PreprodError(f"cannot inspect artifact: {error}") from error


def _validate_dependency_material(name: str, artifact: Path) -> dict:
    expected_platform = name.removeprefix("dependency-material-")
    members = _validate_archive(artifact)
    by_name = {member.name: member for member in members}
    required_metadata = {
        "requirements.lock",
        "test-requirements.lock",
        "sbom.json",
        "vulnerability-report.json",
        "licenses.json",
    }
    if not required_metadata.issubset(by_name):
        raise PreprodError(f"{name} lacks required lock, SBOM, or scan evidence")
    runtime_wheels = {
        member.name: member
        for member in members
        if member.name.startswith("wheelhouse/")
    }
    test_wheels = {
        member.name: member
        for member in members
        if member.name.startswith("test-wheelhouse/")
    }
    wheel_members = runtime_wheels | test_wheels
    if not runtime_wheels or not test_wheels:
        raise PreprodError(f"{name} must contain runtime and test wheels")
    if any(not path.endswith(".whl") for path in wheel_members):
        raise PreprodError(f"{name} contains a non-wheel dependency")
    with tarfile.open(artifact, "r:gz") as archive:
        lock_handle = archive.extractfile(by_name["requirements.lock"])
        test_lock_handle = archive.extractfile(by_name["test-requirements.lock"])
        sbom_handle = archive.extractfile(by_name["sbom.json"])
        vulnerability_handle = archive.extractfile(by_name["vulnerability-report.json"])
        license_handle = archive.extractfile(by_name["licenses.json"])
        if any(
            handle is None
            for handle in (
                lock_handle,
                test_lock_handle,
                sbom_handle,
                vulnerability_handle,
                license_handle,
            )
        ):
            raise PreprodError(f"{name} metadata cannot be read")
        lock = lock_handle.read().decode("utf-8").splitlines()
        test_lock = test_lock_handle.read().decode("utf-8").splitlines()
        if (
            not lock
            or not test_lock
            or not all(LOCK_PATTERN.fullmatch(item) for item in lock + test_lock)
        ):
            raise PreprodError(f"{name} contains a non-exact dependency lock")
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
        expected_files = set(wheel_members)
        if (
            not isinstance(license_report, dict)
            or license_report.get("schema") != "rapp-license-report/1"
            or license_report.get("platform") != expected_platform
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
            or sbom.get("runtime_requirements") != lock
            or sbom.get("test_requirements") != test_lock
            or not isinstance(sbom.get("files"), dict)
        ):
            raise PreprodError(f"{name} SBOM does not match its lock")
        if set(sbom["files"]) != expected_files:
            raise PreprodError(f"{name} SBOM does not enumerate its wheelhouse")
        for path, member in wheel_members.items():
            handle = archive.extractfile(member)
            if handle is None:
                raise PreprodError(f"{name} cannot read {path}")
            if hashlib.sha256(handle.read()).hexdigest() != sbom["files"][path]:
                raise PreprodError(f"{name} wheel hash mismatch: {path}")
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
    kernel = policy["grail_kernel"]
    if (
        subject.get("brainstem_sha256") != kernel["sha256"]
        or subject.get("grail_id") != kernel["grail_id"]
        or subject.get("release_scope") != kernel["release_scope"]
        or subject.get("grail_kernel_ref") != kernel["immutable_ref"]
        or subject.get("grail_kernel_commit") != kernel["commit"]
    ):
        raise PreprodError("kernel-drift: readiness does not use the immutable Grail")
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
        or runtime.get("model_policy") != "observed-at-qualification"
        or runtime.get("data_classification") != "synthetic"
        or runtime.get("kernel_entrypoint") != kernel["path"]
        or runtime.get("grail_id") != kernel["grail_id"]
        or runtime.get("release_scope") != kernel["release_scope"]
    ):
        raise PreprodError("runtime configuration is not production-safe")

    qualification = evidence.get("qualification")
    beta_preflight = evidence.get("beta_preflight")
    soak = evidence.get("soak")
    control_plane = evidence.get("control_plane")
    rapp1_authority = evidence.get("rapp1_authority")
    if not all(
        isinstance(item, dict)
        for item in (
            qualification,
            beta_preflight,
            soak,
            control_plane,
            rapp1_authority,
        )
    ):
        raise PreprodError("readiness evidence is incomplete")
    if (
        qualification.get("repository") != QUALIFICATION_REPOSITORY
        or qualification.get("workflow") != QUALIFICATION_WORKFLOW
        or not re.fullmatch(r"[0-9]+", str(qualification.get("run_id", "")))
        or not re.fullmatch(r"[0-9a-f]{40}", str(qualification.get("commit", "")))
        or qualification.get("url")
        != (
            f"https://github.com/{QUALIFICATION_REPOSITORY}/actions/runs/"
            f"{qualification.get('run_id')}"
        )
    ):
        raise PreprodError("invalid qualification evidence")
    if (
        beta_preflight.get("repository") != BETA_REPOSITORY
        or beta_preflight.get("workflow") != BETA_PREFLIGHT_WORKFLOW
        or not re.fullmatch(r"[0-9]+", str(beta_preflight.get("run_id", "")))
        or beta_preflight.get("url")
        != (
            f"https://github.com/{BETA_REPOSITORY}/actions/runs/"
            f"{beta_preflight.get('run_id')}"
        )
    ):
        raise PreprodError("invalid Beta preflight evidence")
    if not SOAK_URL_PATTERN.fullmatch(str(soak.get("url", ""))):
        raise PreprodError("invalid soak evidence")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(soak.get("sha256", "")))
        or set(soak) != {
            "schema",
            "result",
            "url",
            "sha256",
            "started_at",
            "completed_at",
            "canary_commit",
            "beta_commit",
            "qualification_run_id",
            "model_id",
            "probe_interval_seconds",
            "health_probe_count",
            "authenticated_chat_count",
            "authenticated_chat_times",
            "probes",
            "checks",
        }
    ):
        raise PreprodError("invalid soak evidence")
    if (
        control_plane.get("repository") != QUALIFICATION_REPOSITORY
        or control_plane.get("workflow") != ".github/workflows/stage-preprod.yml"
        or not re.fullmatch(r"[0-9a-f]{40}", str(control_plane.get("commit", "")))
    ):
        raise PreprodError("invalid control-plane evidence")
    if rapp1_authority != policy["rapp1_authority"]:
        raise PreprodError("readiness does not match the RAPP/1 authority pin")
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
    for key in ("brainstem_sha256", "frame_sha256", "history_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(rollback.get(key, ""))):
            raise PreprodError(f"rollback {key} is invalid")
    if not isinstance(rollback.get("history_count"), int) or rollback["history_count"] < 1:
        raise PreprodError("rollback history count is invalid")
    if not re.fullmatch(
        r"brainstem-v[0-9]+\.[0-9]+\.[0-9]+",
        str(rollback.get("history_tip", "")),
    ):
        raise PreprodError("rollback history tip is invalid")

    issued = _parse_time(str(manifest.get("issued_at", "")))
    expires = _parse_time(str(manifest.get("expires_at", "")))
    _validate_soak_evidence(
        {key: value for key, value in soak.items() if key not in {"url", "sha256"}},
        beta_commit=subject["beta_commit"],
        qualification_commit=qualification["commit"],
        qualification_run_id=qualification["run_id"],
        model_id=runtime["model_id"],
        policy=policy,
        reference_time=issued,
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires <= issued:
        raise PreprodError("readiness expiry must follow issuance")
    if issued > current:
        raise PreprodError("preprod readiness is future-dated")
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
            or preprod.get("url")
            != (
                f"https://github.com/{QUALIFICATION_REPOSITORY}/actions/runs/"
                f"{preprod.get('run_id')}"
            )
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
        expected_executable = policy["grail_kernel"]["mode"] == "100755"
        if bool(member.mode & stat.S_IXUSR) != expected_executable:
            raise PreprodError("artifact brainstem.py mode differs from Grail")
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
    if preprod_run_url != (
        f"https://github.com/{QUALIFICATION_REPOSITORY}/actions/runs/"
        f"{preprod_run_id}"
    ):
        raise PreprodError("preprod run URL does not match its run id")
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
        history_count, history_tip = brainstem_history.verify_chain(
            target,
            rollback_frame_path.parent,
        )
    except brainstem_history.HistoryError as error:
        raise PreprodError(f"rollback Brainstem frame is invalid: {error}") from error
    if (
        rollback_frame["release_ref"] != manifest["rollback"]["ref"]
        or rollback_frame["commit"] != manifest["rollback"]["commit"]
        or rollback_frame["brainstem"]["sha256"]
        != manifest["rollback"]["brainstem_sha256"]
        or brainstem_history.frame_sha256(rollback_frame)
        != manifest["rollback"]["frame_sha256"]
        or brainstem_history.history_sha256(rollback_frame_path.parent)
        != manifest["rollback"]["history_sha256"]
        or history_count != manifest["rollback"]["history_count"]
        or history_tip["release_ref"] != manifest["rollback"]["history_tip"]
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


def _validate_grail_target(target: Path) -> tuple[Path, str]:
    target = target.resolve()
    top = Path(_git(target, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != target:
        raise PreprodError("Grail target must be its repository root")
    if _repo_slug(_git(target, "remote", "get-url", "origin").strip()) != "kody-w/rapp-installer":
        raise PreprodError("target is not the Grail repository")
    return target, _git(target, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _verify_release_commit_shape(manifest: dict, target: Path, commit: str) -> None:
    record = _git(target, "rev-list", "--parents", "-n", "1", commit).split()
    if len(record) != 2 or record[1] != manifest["subject"]["grail_base_commit"]:
        raise PreprodError("release commit is not based directly on the sealed Grail base")
    if _git(target, "rev-parse", f"{commit}^{{tree}}").strip() != manifest["subject"]["expected_grail_tree"]:
        raise PreprodError("release commit tree does not match sealed Preprod")


def verify_staged_tree(
    artifact: Path,
    manifest_path: Path,
    target: Path,
    policy_path: Path,
    materials: dict[str, Path],
    now: datetime | None = None,
    verify_provenance: bool = True,
) -> str:
    manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        now=now,
        materials=materials,
    )
    if manifest["status"] != "seaworthy":
        raise PreprodError("only a seaworthy manifest can authorize a Grail tree")
    if verify_provenance:
        _verify_github_provenance(
            manifest,
            (artifact, manifest_path, *materials.values()),
        )
    target, branch = _validate_grail_target(target)
    if branch in {"main", "HEAD"}:
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


def verify_release_commit(
    artifact: Path,
    manifest_path: Path,
    target: Path,
    policy_path: Path,
    materials: dict[str, Path],
    now: datetime | None = None,
    verify_provenance: bool = True,
) -> str:
    manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        now=now,
        materials=materials,
    )
    if manifest["status"] != "seaworthy":
        raise PreprodError("only a seaworthy manifest can authorize a release commit")
    if verify_provenance:
        _verify_github_provenance(
            manifest,
            (artifact, manifest_path, *materials.values()),
        )
    target, branch = _validate_grail_target(target)
    if branch in {"main", "HEAD"}:
        raise PreprodError("release commit verification requires a release branch")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PreprodError("release branch must be clean after commit")
    commit = _git(target, "rev-parse", "HEAD^{commit}").strip()
    _verify_release_commit_shape(manifest, target, commit)
    return commit


def verify_final_merge(
    artifact: Path,
    manifest_path: Path,
    target: Path,
    release_commit: str,
    policy_path: Path,
    materials: dict[str, Path],
    now: datetime | None = None,
    verify_provenance: bool = True,
) -> str:
    manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        now=now,
        materials=materials,
    )
    if manifest["status"] != "seaworthy":
        raise PreprodError("only a seaworthy manifest can authorize a Grail merge")
    if verify_provenance:
        _verify_github_provenance(
            manifest,
            (artifact, manifest_path, *materials.values()),
        )
    if not re.fullmatch(r"[0-9a-f]{40}", release_commit):
        raise PreprodError("release commit must be a full lowercase SHA")
    target, branch = _validate_grail_target(target)
    if branch != "main":
        raise PreprodError("final merge verification requires the main branch")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PreprodError("Grail main must be clean after merge")
    _verify_release_commit_shape(manifest, target, release_commit)
    merge_commit = _git(target, "rev-parse", "HEAD^{commit}").strip()
    record = _git(target, "rev-list", "--parents", "-n", "1", merge_commit).split()
    expected_parents = [
        manifest["subject"]["grail_base_commit"],
        release_commit,
    ]
    if len(record) != 3 or record[1:] != expected_parents:
        raise PreprodError("final merge parents do not preserve the sealed release")
    if _git(target, "rev-parse", "HEAD^{tree}").strip() != manifest["subject"]["expected_grail_tree"]:
        raise PreprodError("final Grail merge tree does not match sealed Preprod")
    return merge_commit


def _extract_archive(artifact: Path, destination: Path) -> None:
    members = _validate_archive(artifact)
    destination = destination.resolve()
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise PreprodError("archive destination must be absent or empty")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(artifact, "r:gz") as archive:
        for member in members:
            output = destination.joinpath(*PurePosixPath(member.name).parts)
            try:
                output.resolve(strict=False).relative_to(destination)
            except ValueError as error:
                raise PreprodError(
                    f"artifact member escapes extraction root: {member.name}"
                ) from error
            output.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise PreprodError(f"cannot read artifact member: {member.name}")
            temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
            try:
                with temporary.open("wb") as handle:
                    while chunk := source.read(1024 * 1024):
                        handle.write(chunk)
                os.chmod(temporary, 0o755 if member.mode & stat.S_IXUSR else 0o644)
                os.replace(temporary, output)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


def _snapshot_regular_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int,
) -> Path:
    source = source.expanduser().absolute()
    if source.is_symlink():
        raise PreprodError(f"release input must not be a symlink: {source}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise PreprodError(f"release input is invalid or too large: {source}")
        with (
            os.fdopen(descriptor, "rb", closefd=False) as reader,
            destination.open("xb") as writer,
        ):
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if destination.stat().st_size != metadata.st_size:
            raise PreprodError(f"release input changed while snapshotting: {source}")
        os.chmod(destination, 0o444)
    finally:
        os.close(descriptor)
    return destination


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
    allow_candidate: bool = False,
    now: datetime | None = None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="rapp-release-inputs-") as raw_inputs:
        inputs = Path(raw_inputs)
        artifact_snapshot = _snapshot_regular_file(
            artifact,
            inputs / "artifact" / artifact.name,
            max_bytes=MAX_ARCHIVE_BYTES,
        )
        manifest_snapshot = _snapshot_regular_file(
            manifest_path,
            inputs / "manifest" / manifest_path.name,
            max_bytes=4 * 1024 * 1024,
        )
        material_snapshots = {
            name: _snapshot_regular_file(
                path,
                inputs / "materials" / name / path.name,
                max_bytes=MAX_ARCHIVE_BYTES,
            )
            for name, path in materials.items()
        }
        return _prepare_runtime_from_snapshots(
            artifact_snapshot,
            manifest_snapshot,
            destination,
            state_dir,
            policy_path,
            material_snapshots,
            platform_name=platform_name,
            verify_provenance=verify_provenance,
            install_dependencies=install_dependencies,
            allow_candidate=allow_candidate,
            now=now,
        )


def _prepare_runtime_from_snapshots(
    artifact: Path,
    manifest_path: Path,
    destination: Path,
    state_dir: Path,
    policy_path: Path,
    materials: dict[str, Path],
    platform_name: str | None = None,
    verify_provenance: bool = True,
    install_dependencies: bool = True,
    allow_candidate: bool = False,
    now: datetime | None = None,
) -> dict:
    policy = _read_json(policy_path)
    _validate_policy(policy)
    manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        now=now,
        materials=None if allow_candidate else materials,
    )
    if manifest["status"] != "seaworthy" and not (
        allow_candidate and manifest["status"] == "preprod-candidate"
    ):
        raise PreprodError("artifact status cannot prepare this runtime")
    if allow_candidate and verify_provenance:
        raise PreprodError("candidate runtime preparation cannot claim provenance")
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
    if platform_key not in {"linux", "macos", "windows"}:
        raise PreprodError(f"unsupported runtime platform: {platform_key}")
    material_name = f"dependency-material-{platform_key}"
    if material_name not in materials:
        raise PreprodError(f"no sealed dependency material for {platform_key}")
    selected_material = materials[material_name]
    _validate_dependency_material(material_name, selected_material)
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
        verify_grail_kernel_bytes(source_dir, policy)
        _extract_archive(selected_material, dependencies)
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
                [sys.executable, "-I", "-m", "venv", str(venv)],
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
                    "-I",
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
                "schema": (
                    "rapp-preprod-deployment/1"
                    if allow_candidate
                    else "rapp-seaworthy-deployment/1"
                ),
                "artifact_sha256": manifest["subject"]["artifact_sha256"],
                "brainstem_sha256": manifest["subject"]["brainstem_sha256"],
                "grail_id": manifest["subject"]["grail_id"],
                "release_scope": manifest["subject"]["release_scope"],
                "kernel_entrypoint": manifest["runtime"]["kernel_entrypoint"],
                "resolved_kernel_path": str(
                    (
                        destination
                        / "src"
                        / manifest["runtime"]["kernel_entrypoint"]
                    ).resolve(strict=False)
                ),
                "material": material_name,
                "material_sha256": (
                    _sha256(selected_material)
                    if allow_candidate
                    else manifest["deployment_materials"][material_name]["sha256"]
                ),
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
        "manifest": manifest,
        "material_name": material_name,
        "material_sha256": _sha256(selected_material),
    }


def launch_runtime(
    artifact: Path,
    manifest_path: Path,
    state_dir: Path,
    policy_path: Path,
    materials: dict[str, Path],
    evidence_path: Path,
    *,
    platform_name: str | None = None,
    allow_candidate: bool = False,
    verify_provenance: bool = True,
    now: datetime | None = None,
) -> int:
    policy = _read_json(policy_path)
    _validate_policy(policy)
    kernel = policy["grail_kernel"]
    raw_state_dir = state_dir.expanduser()
    if raw_state_dir.is_symlink():
        raise PreprodError("runtime state directory is invalid")
    state_dir = raw_state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    preliminary_manifest = verify_candidate(
        artifact,
        manifest_path,
        policy_path,
        now=now,
        materials=None if allow_candidate else materials,
    )
    model_state = state_dir / ".brainstem_model"
    if model_state.exists():
        persisted_model = _read_json(model_state).get("model")
        if persisted_model != preliminary_manifest["runtime"]["model_id"]:
            raise PreprodError(
                "persisted model conflicts with the sealed production model"
            )

    with tempfile.TemporaryDirectory(prefix="rapp-verified-launch-") as raw_snapshot:
        snapshot = Path(raw_snapshot) / "runtime"
        prepared = prepare_runtime(
            artifact,
            manifest_path,
            snapshot,
            state_dir,
            policy_path,
            materials,
            platform_name=platform_name,
            verify_provenance=verify_provenance,
            allow_candidate=allow_candidate,
            now=now,
        )
        manifest = prepared["manifest"]
        if model_state.exists():
            persisted_model = _read_json(model_state).get("model")
            if persisted_model != manifest["runtime"]["model_id"]:
                raise PreprodError(
                    "persisted model conflicts with the sealed production model"
                )
        platform_key = platform_name or {
            "linux": "linux",
            "darwin": "macos",
            "win32": "windows",
        }.get(sys.platform)
        material_name = prepared["material_name"]
        kernel_path = (
            prepared["source"] / PurePosixPath(kernel["path"])
        ).resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(kernel_path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise PreprodError("kernel entrypoint is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read()
        finally:
            os.close(descriptor)
        if (
            len(payload) != kernel["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != kernel["sha256"]
            or _grail_id(payload) != kernel["grail_id"]
        ):
            raise PreprodError("kernel-drift: runtime entrypoint differs from Grail")

        python = (
            prepared["venv"] / "Scripts" / "python.exe"
            if os.name == "nt"
            else prepared["venv"] / "bin" / "python"
        )
        if not python.is_file():
            raise PreprodError("runtime Python interpreter is missing")
        environment = os.environ.copy()
        environment["BRAINSTEM_STATE_DIR"] = str(state_dir)
        environment["GITHUB_MODEL"] = manifest["runtime"]["model_id"]
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            [
                str(python),
                "-I",
                "-c",
                KERNEL_BOOTSTRAP,
                str(kernel_path),
            ],
            stdin=subprocess.PIPE,
            env=environment,
        )
        if process.stdin is None:
            process.kill()
            raise PreprodError("cannot stream verified kernel bytes to the runtime")
        previous_handlers = {}

        def forward_signal(signum, _frame):
            process.send_signal(signum)

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, forward_signal)
        try:
            process.stdin.write(payload)
            process.stdin.close()
            _write_json(
                evidence_path,
                {
                    "schema": "rapp-kernel-launch/1",
                    "release_scope": kernel["release_scope"],
                    "grail_id": kernel["grail_id"],
                    "artifact_sha256": manifest["subject"]["artifact_sha256"],
                    "kernel_sha256": kernel["sha256"],
                    "kernel_size_bytes": len(payload),
                    "resolved_kernel_path": str(kernel_path),
                    "execution_mode": "verified-memory-snapshot",
                    "material": material_name,
                    "material_sha256": prepared["material_sha256"],
                    "runtime_python": str(python.resolve()),
                    "process_id": process.pid,
                    "verified_at": _format_time(datetime.now(timezone.utc)),
                },
            )
            return process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                process.wait()
            raise
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


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
    package.add_argument("--qualification-commit", required=True)
    package.add_argument("--beta-preflight-run-id", required=True)
    package.add_argument("--beta-preflight-url", required=True)
    package.add_argument("--soak-evidence", type=Path, required=True)
    package.add_argument("--soak-evidence-url", required=True)
    package.add_argument("--soak-evidence-sha256", required=True)
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
    verify.add_argument("--verify-provenance", action="store_true")
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

    def add_tree_verification_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--target", type=Path, required=True)
        command.add_argument("--material", action="append", default=[])

    verify_tree = subparsers.add_parser("verify-staged-tree")
    add_tree_verification_arguments(verify_tree)

    verify_release = subparsers.add_parser("verify-release-commit")
    add_tree_verification_arguments(verify_release)

    verify_merge = subparsers.add_parser("verify-final-merge")
    add_tree_verification_arguments(verify_merge)
    verify_merge.add_argument("--release-commit", required=True)

    bundle = subparsers.add_parser("bundle")
    bundle.add_argument("--source", type=Path, required=True)
    bundle.add_argument("--artifact", type=Path, required=True)

    extract = subparsers.add_parser("extract")
    extract.add_argument("--artifact", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)

    validate_requirements = subparsers.add_parser("validate-requirements")
    validate_requirements.add_argument("--requirements", type=Path, required=True)

    verify_kernel = subparsers.add_parser("verify-kernel")
    verify_kernel.add_argument("--repo", type=Path, required=True)

    subparsers.add_parser("verify-policy")

    def add_launch_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--platform", choices=("linux", "macos", "windows"))
        command.add_argument("--evidence", type=Path, required=True)
        command.add_argument("--material", action="append", default=[])

    launch = subparsers.add_parser("launch-runtime")
    add_launch_arguments(launch)
    launch_candidate = subparsers.add_parser("launch-candidate-runtime")
    add_launch_arguments(launch_candidate)

    def add_runtime_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--destination", type=Path, required=True)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--platform", choices=("linux", "macos", "windows"))
        command.add_argument("--material", action="append", default=[])

    prepare = subparsers.add_parser("prepare-runtime")
    add_runtime_arguments(prepare)
    prepare_candidate = subparsers.add_parser("prepare-candidate-runtime")
    add_runtime_arguments(prepare_candidate)
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
                args.qualification_commit,
                args.beta_preflight_run_id,
                args.beta_preflight_url,
                args.soak_evidence.resolve(),
                args.soak_evidence_url,
                args.soak_evidence_sha256,
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
            materials = (
                _parse_material_specs(args.material)
                if args.material
                else None
            )
            manifest = verify_candidate(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.policy.resolve(),
                expected_beta_commit=args.expected_beta_commit,
                expected_qualification_run=args.expected_qualification_run,
                allow_expired=args.allow_expired,
                materials=materials,
            )
            if args.verify_provenance:
                if manifest["status"] != "seaworthy" or materials is None:
                    raise PreprodError(
                        "provenance verification requires a seaworthy "
                        "manifest and all deployment materials"
                    )
                _verify_github_provenance(
                    manifest,
                    (
                        args.artifact.resolve(),
                        args.manifest.resolve(),
                        *materials.values(),
                    ),
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
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.target.resolve(),
                args.policy.resolve(),
                _parse_material_specs(args.material),
            )
            print(f"GRAIL TREE VERIFIED — {tree}")
        elif args.command == "verify-release-commit":
            commit = verify_release_commit(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.target.resolve(),
                args.policy.resolve(),
                _parse_material_specs(args.material),
            )
            print(f"GRAIL RELEASE COMMIT VERIFIED — {commit}")
        elif args.command == "verify-final-merge":
            commit = verify_final_merge(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.target.resolve(),
                args.release_commit,
                args.policy.resolve(),
                _parse_material_specs(args.material),
            )
            print(f"GRAIL FINAL MERGE VERIFIED — {commit}")
        elif args.command == "bundle":
            digest = build_artifact(args.source.resolve(), args.artifact.resolve())
            print(f"DEPLOYMENT MATERIAL — {digest} ({args.artifact.name})")
        elif args.command == "extract":
            _extract_archive(args.artifact.resolve(), args.destination.resolve())
            print(f"ARCHIVE EXTRACTED — {args.destination.resolve()}")
        elif args.command == "validate-requirements":
            requirements = _requirement_lines(args.requirements.resolve())
            print(f"REGISTRY REQUIREMENTS VERIFIED — {len(requirements)} entries")
        elif args.command == "verify-kernel":
            kernel = verify_grail_kernel_bytes(
                args.repo.resolve(),
                _read_json(args.policy.resolve()),
            )
            print(
                "IMMUTABLE GRAIL VERIFIED — "
                f"{kernel['immutable_ref']} sha256:{kernel['sha256']}"
            )
        elif args.command == "verify-policy":
            _validate_policy(_read_json(args.policy.resolve()))
            print("PREPROD POLICY VERIFIED — immutable RAPP/1 and Grail pins")
        elif args.command in {"launch-runtime", "launch-candidate-runtime"}:
            return launch_runtime(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.state_dir,
                args.policy.resolve(),
                _parse_material_specs(args.material),
                args.evidence.resolve(),
                platform_name=args.platform,
                allow_candidate=args.command == "launch-candidate-runtime",
                verify_provenance=args.command == "launch-runtime",
            )
        elif args.command in {"prepare-runtime", "prepare-candidate-runtime"}:
            result = prepare_runtime(
                args.artifact.resolve(),
                args.manifest.resolve(),
                args.destination.resolve(),
                args.state_dir.resolve(),
                args.policy.resolve(),
                _parse_material_specs(args.material),
                platform_name=args.platform,
                verify_provenance=args.command == "prepare-runtime",
                allow_candidate=args.command == "prepare-candidate-runtime",
            )
            print(f"SEALED RUNTIME — source={result['source']} venv={result['venv']}")
        else:
            raise PreprodError(f"unsupported command: {args.command}")
    except (OSError, PreprodError) as error:
        print(f"preprod gate failed: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
