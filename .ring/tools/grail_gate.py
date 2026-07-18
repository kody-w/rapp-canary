#!/usr/bin/env python3
"""The last gate before the human-only Grail merge.

Verifies a green "Test Pre-Grail Rings" run end-to-end — the attestation
chain is internally consistent, Beta's main has not moved since it was
qualified, and the attested payload digest recomputes from a fresh clone —
then optionally exports the exact qualified shared payload onto a Grail
feature branch. It never commits and never pushes; the merge stays human.

    grail_gate.py verify --run-id 123456
    grail_gate.py verify --run-id 123456 --export-to ~/src/rapp-installer-release
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import promote_ring  # noqa: E402
import ring_attestation as attestation  # noqa: E402

HUB_REPOSITORY = "kody-w/rapp-canary"
WORKFLOW_NAME = "Test Pre-Grail Rings"
RING_ORDER = ("canary", "nightly", "alpha", "beta")


class GateError(RuntimeError):
    pass


def _run(*argv: str) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        raise GateError(
            f"{' '.join(argv[:3])}... failed: {result.stderr.strip()}"
        )
    return result.stdout


def _validate_run_record(run: dict) -> None:
    """The qualification run must be the hub workflow, and green."""
    if run.get("name") != WORKFLOW_NAME:
        raise GateError(
            f"run is '{run.get('name')}', not '{WORKFLOW_NAME}'"
        )
    if run.get("conclusion") != "success":
        raise GateError(
            f"run conclusion is '{run.get('conclusion')}', not success"
        )
    repository = (run.get("repository") or {}).get("full_name", "")
    if repository.lower() != HUB_REPOSITORY:
        raise GateError(f"run belongs to {repository}, not {HUB_REPOSITORY}")


def _validate_chain(chain: dict[str, dict], rings: dict[str, dict]) -> str:
    """Attestations must link canary -> nightly -> alpha -> beta over ONE payload."""
    for name in RING_ORDER:
        if name not in chain:
            raise GateError(f"attestation chain is missing {name}.json")
        attestation._validate_attestation(chain[name], rings)
        if chain[name]["ring"] != name:
            raise GateError(f"{name}.json attests ring {chain[name]['ring']}")
    digest = chain["canary"]["payload"]["shared_sha256"]
    for name in RING_ORDER[1:]:
        if chain[name]["payload"]["shared_sha256"] != digest:
            raise GateError(f"{name} attests a different shared payload")
    for child, parent in zip(RING_ORDER[1:], RING_ORDER[:-1]):
        expected = {
            "ring": parent,
            "sha256": attestation._attestation_sha256(chain[parent]),
        }
        if chain[child]["parent"] != expected:
            raise GateError(
                f"{child} parent link does not match {parent} attestation digest"
            )
    return digest


def _load_chain(directory: Path) -> dict[str, dict]:
    return {
        name: attestation._read_json(directory / f"{name}.json")
        for name in RING_ORDER
    }


def _beta_still_current(beta: dict) -> None:
    repository = beta["payload"]["repository"]
    commit = beta["payload"]["commit"]
    output = _run(
        "git", "ls-remote", f"https://github.com/{repository}.git",
        "refs/heads/main",
    )
    tip = output.split()[0] if output.split() else ""
    if tip != commit:
        raise GateError(
            f"{repository} main is {tip[:12]} but the qualification attested "
            f"{commit[:12]} — the ring moved after qualification; re-qualify"
        )


def _fresh_beta_clone(beta: dict, workdir: Path) -> Path:
    repository = beta["payload"]["repository"]
    commit = beta["payload"]["commit"]
    clone = workdir / "beta"
    _run(
        "git", "clone", "--quiet", "--branch", "main",
        f"https://github.com/{repository}.git", str(clone),
    )
    head = _run("git", "-C", str(clone), "rev-parse", "HEAD^{commit}").strip()
    if head != commit:
        raise GateError(f"fresh beta clone is at {head[:12]}, not {commit[:12]}")
    return clone


def _export(clone: Path, grail: Path, config: dict) -> int:
    """Copy the verified shared payload onto a Grail feature branch. No commits."""
    prefixes = attestation._ring_owned_prefixes(config)
    origin = _run("git", "-C", str(grail), "remote", "get-url", "origin").strip()
    if attestation._repo_slug(origin) != "kody-w/rapp-installer":
        raise GateError(f"--export-to origin is {origin}, not the grail")
    branch = _run(
        "git", "-C", str(grail), "rev-parse", "--abbrev-ref", "HEAD"
    ).strip()
    if branch in ("main", "HEAD"):
        raise GateError(
            "grail checkout must be on a release feature branch, never main"
        )
    status = _run(
        "git", "-C", str(grail), "status", "--porcelain=v1",
        "--untracked-files=all",
    )
    if status:
        raise GateError("grail worktree must be clean before export")

    source_entries = promote_ring._entries(clone, prefixes)
    target_entries = promote_ring._tracked_shared(grail, prefixes)
    promote_ring._preflight_transition(grail, set(source_entries), target_entries)
    changed = 0
    for path in sorted(target_entries.difference(source_entries)):
        destination = grail / Path(path)
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
            promote_ring._git(grail, "update-index", "--force-remove", "--", path)
            promote_ring._remove_empty_parents(destination.parent, grail)
            changed += 1
    for path, (mode, object_id) in sorted(source_entries.items()):
        promote_ring._write_blob(clone, grail, path, mode, object_id)
        promote_ring._stage_raw(grail, path, mode)
        changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--export-to", type=Path)
    verify.add_argument(
        "--config", type=Path, default=TOOLS.parent / "train.json"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+", args.run_id):
        print("gate failed: --run-id must be numeric", file=sys.stderr)
        return 1
    try:
        config = attestation._read_json(args.config.resolve())
        rings = attestation._ring_map(config)
        run = json.loads(
            _run("gh", "api", f"repos/{HUB_REPOSITORY}/actions/runs/{args.run_id}")
        )
        _validate_run_record(run)
        with tempfile.TemporaryDirectory(prefix="grail-gate-") as scratch:
            workdir = Path(scratch)
            artifacts = workdir / "attestations"
            _run(
                "gh", "run", "download", args.run_id, "-R", HUB_REPOSITORY,
                "-p", "pre-grail-attestations-*", "-D", str(artifacts),
            )
            candidates = sorted(artifacts.glob("**/beta.json"))
            if len(candidates) != 1:
                raise GateError(
                    f"expected exactly one attestation set, found {len(candidates)}"
                )
            chain = _load_chain(candidates[0].parent)
            digest = _validate_chain(chain, rings)
            _beta_still_current(chain["beta"])
            clone = _fresh_beta_clone(chain["beta"], workdir)
            _run(
                sys.executable, str(TOOLS / "ring_attestation.py"),
                "--config", str(args.config.resolve()),
                "verify",
                "--repo", str(clone),
                "--repository", chain["beta"]["payload"]["repository"],
                "--commit", chain["beta"]["payload"]["commit"],
                "--parent", str(candidates[0].parent / "alpha.json"),
                "--attestation", str(candidates[0]),
            )
            version = (
                clone / "rapp_brainstem" / "VERSION"
            ).read_text(encoding="utf-8").strip()
            print(f"GATE PASS — qualified payload {digest[:12]} (v{version})")
            print(f"  run: {run.get('html_url')}")
            print(f"  beta: {chain['beta']['payload']['commit']}")
            if args.export_to:
                changed = _export(clone, args.export_to.resolve(), config)
                print(f"  exported: {changed} paths staged in {args.export_to}")
                print(
                    "next: inspect `git status`, run the tests, commit with the\n"
                    f"run URL in the message, push the branch (re-enable the push\n"
                    "URL first — see .ring/RUNBOOK.md), and follow RELEASING.md."
                )
    except (GateError, attestation.AttestationError,
            promote_ring.PromotionError, OSError, ValueError) as error:
        print(f"gate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
