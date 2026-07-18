#!/usr/bin/env python3
"""Run disposable feature and failure scenarios through every pre-Grail ring."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import promote_ring
import render_ring
import ring_attestation


RINGS = ("canary", "nightly", "alpha", "beta")
REPOSITORIES = {ring: f"kody-w/rapp-{ring}" for ring in RINGS}
OVERLAY_BRANCH = os.getenv("RAPP_RING_REF", "setup/ring-overlay")


class ScenarioError(RuntimeError):
    pass


def _run(args, *, cwd=None, env=None, quiet=False, retries=0):
    result = None
    for attempt in range(retries + 1):
        result = subprocess.run(
            [str(item) for item in args],
            cwd=cwd,
            env=env,
            capture_output=quiet,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result
        if attempt < retries:
            time.sleep(2 ** attempt)
    detail = ""
    if quiet:
        detail = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    raise ScenarioError(f"command failed: {' '.join(map(str, args))}{detail}")


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", "-C", repo, *args], quiet=True)
    return result.stdout.strip()


def _clone_url(url: str, path: Path, *clone_args: str) -> None:
    last_error = None
    for attempt in range(3):
        if path.exists():
            shutil.rmtree(path)
        try:
            _run([
                "git",
                "-c",
                "core.autocrlf=false",
                "clone",
                "--quiet",
                *clone_args,
                url,
                path,
            ])
            return
        except ScenarioError as error:
            last_error = error
            time.sleep(2 ** attempt)
    raise last_error


def _clone_ring(root: Path, ring: str) -> Path:
    path = root / ring
    _clone_url(
        f"https://github.com/{REPOSITORIES[ring]}.git",
        path,
        "--branch",
        OVERLAY_BRANCH,
    )
    _git(path, "config", "core.autocrlf", "false")
    _git(path, "config", "user.name", "Autonomous Ring Test")
    _git(path, "config", "user.email", "ring-test@example.invalid")
    return path


def _commit(repo: Path, message: str, *, stage: bool = True) -> str:
    if stage:
        _git(repo, "add", "-f", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD^{commit}")


def _backend_feature(repo: Path) -> None:
    path = repo / "rapp_brainstem" / "brainstem.py"
    text = path.read_text(encoding="utf-8")
    marker = '@app.route("/version", methods=["GET"])'
    addition = (
        '\n@app.route("/pipeline-probe", methods=["GET"])\n'
        "def pipeline_probe():\n"
        '    return jsonify({"status": "ok", "feature": "backend-probe"})\n\n'
    )
    if marker not in text:
        raise ScenarioError("backend route insertion point not found")
    path.write_text(
        text.replace(marker, addition + marker, 1),
        encoding="utf-8",
        newline="\n",
    )


def _ui_feature(repo: Path) -> None:
    path = repo / "rapp_brainstem" / "index.html"
    text = path.read_text(encoding="utf-8")
    marker = "</head>"
    addition = '<meta name="rapp-pipeline-feature" content="ui-probe">\n'
    if marker not in text:
        raise ScenarioError("UI insertion point not found")
    path.write_text(
        text.replace(marker, addition + marker, 1),
        encoding="utf-8",
        newline="\n",
    )


def _agent_feature(repo: Path) -> None:
    path = (
        repo
        / "rapp_brainstem"
        / "agents"
        / "pipeline_probe_agent.py"
    )
    path.write_text(
        "from agents.basic_agent import BasicAgent\n\n"
        "class PipelineProbeAgent(BasicAgent):\n"
        "    def __init__(self):\n"
        '        self.name = "PipelineProbe"\n'
        "        self.metadata = {\n"
        '            "name": self.name,\n'
        '            "description": "Return a deterministic pipeline probe.",\n'
        '            "parameters": {"type": "object", "properties": {}, "required": []},\n'
        "        }\n"
        "        super().__init__(name=self.name, metadata=self.metadata)\n\n"
        "    def perform(self, **kwargs):\n"
        '        return "pipeline-probe-ok"\n',
        encoding="utf-8",
        newline="\n",
    )


def _installer_feature(repo: Path) -> None:
    for relative, comment in (
        ("install.sh", "# pipeline-feature: installer-probe\n"),
        ("docs/install.sh", "# pipeline-feature: installer-probe\n"),
        ("install.ps1", "# pipeline-feature: installer-probe\n"),
        ("docs/install.ps1", "# pipeline-feature: installer-probe\n"),
    ):
        path = repo / relative
        path.write_text(
            comment + path.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )


def _shape_feature(repo: Path) -> None:
    path = repo / "rapp_brainstem" / "pipeline_probe" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema":"pipeline-probe/1","enabled":true}\n',
        encoding="utf-8",
        newline="\n",
    )


def _config_feature(repo: Path) -> None:
    path = repo / "rapp_brainstem" / ".env.example"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n# Pipeline feature probe\nPIPELINE_PROBE=false\n",
        encoding="utf-8",
        newline="\n",
    )


def _storage_feature(repo: Path) -> None:
    path = repo / "rapp_brainstem" / "local_storage.py"
    text = path.read_text(encoding="utf-8")
    marker = "    def file_exists(self, file_path):"
    addition = (
        '    def pipeline_probe(self):\n'
        '        return "storage-probe-ok"\n\n'
    )
    if marker not in text:
        raise ScenarioError("storage insertion point not found")
    path.write_text(
        text.replace(marker, addition + marker, 1),
        encoding="utf-8",
        newline="\n",
    )


def _binary_feature(repo: Path) -> None:
    path = repo / "rapp_brainstem" / "assets" / "pipeline-probe.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00RAPP-PIPELINE\xff\x10")


def _deletion_feature(repo: Path) -> None:
    path = repo / "rapp_brainstem" / ".vscode" / "settings.json"
    if not path.is_file():
        raise ScenarioError("deletion fixture is missing")
    path.unlink()


SCENARIOS = {
    "backend-route": _backend_feature,
    "ui-meta": _ui_feature,
    "agent-addition": _agent_feature,
    "installer-parity": _installer_feature,
    "tree-shape": _shape_feature,
    "config-default": _config_feature,
    "storage-api": _storage_feature,
    "binary-asset": _binary_feature,
    "file-deletion": _deletion_feature,
}


def _sandbox_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    temporary = home / "tmp"
    temporary.mkdir(exist_ok=True)
    allowed = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PYTHONUTF8",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "TMP": str(temporary),
        "TEMP": str(temporary),
    })
    return env


def _assert_feature_static(name: str, build: Path) -> None:
    if name == "backend-route":
        text = (build / "rapp_brainstem" / "brainstem.py").read_text(
            encoding="utf-8"
        )
        if '/pipeline-probe"' not in text or '"backend-probe"' not in text:
            raise ScenarioError("rendered backend feature is missing")
    elif name == "ui-meta":
        if 'name="rapp-pipeline-feature"' not in (
            build / "rapp_brainstem" / "index.html"
        ).read_text(encoding="utf-8"):
            raise ScenarioError("rendered UI feature is missing")
    elif name == "agent-addition":
        text = (
            build
            / "rapp_brainstem"
            / "agents"
            / "pipeline_probe_agent.py"
        ).read_text(encoding="utf-8")
        if "PipelineProbeAgent" not in text or "pipeline-probe-ok" not in text:
            raise ScenarioError("rendered agent feature is missing")
    elif name == "installer-parity":
        for relative in (
            "install.sh",
            "docs/install.sh",
            "install.ps1",
            "docs/install.ps1",
        ):
            if "pipeline-feature: installer-probe" not in (
                build / relative
            ).read_text(encoding="utf-8"):
                raise ScenarioError(f"installer marker missing from {relative}")
    elif name == "tree-shape":
        if not (
            build / "rapp_brainstem" / "pipeline_probe" / "config.json"
        ).is_file():
            raise ScenarioError("new nested shape did not promote")
    elif name == "config-default":
        if "PIPELINE_PROBE=false" not in (
            build / "rapp_brainstem" / ".env.example"
        ).read_text(encoding="utf-8"):
            raise ScenarioError("configuration feature is missing")
    elif name == "storage-api":
        text = (build / "rapp_brainstem" / "local_storage.py").read_text(
            encoding="utf-8"
        )
        if "def pipeline_probe" not in text or "storage-probe-ok" not in text:
            raise ScenarioError("storage API feature is missing")
    elif name == "binary-asset":
        if (
            build / "rapp_brainstem" / "assets" / "pipeline-probe.bin"
        ).read_bytes() != b"\x00RAPP-PIPELINE\xff\x10":
            raise ScenarioError("binary asset changed during promotion")
    elif name == "file-deletion":
        if (
            build
            / "rapp_brainstem"
            / ".vscode"
            / "settings.json"
        ).exists():
            raise ScenarioError("deleted file reappeared")


def _assert_feature_runtime(
    name: str,
    build: Path,
    python: str,
    env: dict[str, str],
) -> None:
    _assert_feature_static(name, build)
    if name == "backend-route":
        _run([
            python,
            "-c",
            (
                "import os,sys; os.chdir(sys.argv[1]); sys.path.insert(0,'.'); "
                "import brainstem; r=brainstem.app.test_client().get('/pipeline-probe'); "
                "assert r.status_code==200 and r.get_json()['feature']=='backend-probe'"
            ),
            build / "rapp_brainstem",
        ], env=env)
    elif name == "agent-addition":
        _run([
            python,
            "-c",
            (
                "import importlib.util,sys; p=sys.argv[1]; "
                "sys.path.insert(0,sys.argv[2]); "
                "s=importlib.util.spec_from_file_location('probe',p); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "assert m.PipelineProbeAgent().perform()=='pipeline-probe-ok'"
            ),
            build / "rapp_brainstem" / "agents" / "pipeline_probe_agent.py",
            build / "rapp_brainstem",
        ], env=env)
    elif name == "storage-api":
        _run([
            python,
            "-c",
            (
                "import sys; sys.path.insert(0,sys.argv[1]); "
                "import local_storage; "
                "assert local_storage.AzureFileStorageManager().pipeline_probe()"
                "=='storage-probe-ok'"
            ),
            build / "rapp_brainstem",
        ], env=env)


def _test_source(
    repo: Path,
    python: str,
    bash: str,
    env: dict[str, str],
) -> None:
    _run(
        [python, "-m", "pytest", "tests", "-q"],
        cwd=repo / "rapp_brainstem",
        env=env,
        quiet=True,
    )
    _run(
        [bash, "tests/test_installer.sh"],
        cwd=repo,
        env=env,
        quiet=True,
    )


def _test_rendered(
    build: Path,
    python: str,
    bash: str,
    name: str,
    env: dict[str, str],
) -> None:
    _run(
        [python, "-m", "py_compile", "brainstem.py"],
        cwd=build / "rapp_brainstem",
        env=env,
    )
    _run(
        [
            python,
            "-c",
            (
                "import brainstem; "
                "r=brainstem.app.test_client().get('/health'); "
                "assert r.status_code==200"
            ),
        ],
        cwd=build / "rapp_brainstem",
        env=env,
    )
    _run([bash, "-n", "install.sh"], cwd=build, env=env)
    if (
        (build / "install.sh").read_bytes()
        != (build / "docs" / "install.sh").read_bytes()
    ):
        raise ScenarioError("rendered installer mirror drifted")
    _assert_feature_runtime(name, build, python, env)


def _promote_scenario(
    root: Path,
    name: str,
    mutation,
    config: Path,
    python: str,
    bash: str,
    run_tests: bool,
) -> dict:
    repos = {ring: _clone_ring(root, ring) for ring in RINGS}
    mutation(repos["canary"])
    commits = {
        "canary": _commit(repos["canary"], f"test: {name} in Canary")
    }
    attestations = {}
    builds = {}
    parent_path = None
    for index, ring in enumerate(RINGS):
        if index:
            parent = RINGS[index - 1]
            base_commit = _git(repos[ring], "rev-parse", "HEAD^{commit}")
            promote_ring.promote(
                repos[parent],
                repos[ring],
                parent,
                ring,
                commits[parent],
                base_commit,
                config,
            )
            commits[ring] = _commit(
                repos[ring],
                f"test: promote {name} into {ring}",
                stage=False,
            )
        output = root / f"{ring}.json"
        ring_attestation.create_attestation(
            ring,
            repos[ring],
            REPOSITORIES[ring],
            commits[ring],
            config,
            output,
            parent_path,
        )
        attestations[ring] = output
        parent_path = output
        build = root / f"{ring}-build"
        render_ring.render(
            repos[ring],
            repos[ring] / ".ring" / "ring.json",
            build,
        )
        if run_tests:
            sandbox_env = _sandbox_env(root / f"{ring}-sandbox-home")
            _test_source(
                repos[ring],
                python,
                bash,
                sandbox_env,
            )
            _test_rendered(
                build,
                python,
                bash,
                name,
                sandbox_env,
            )
        else:
            _assert_feature_static(name, build)
        builds[ring] = build

    digests = {
        json.loads(path.read_text(encoding="utf-8"))["payload"]["shared_sha256"]
        for path in attestations.values()
    }
    if len(digests) != 1:
        raise ScenarioError(f"{name}: shared payload diverged")
    return {
        "name": name,
        "status": "passed",
        "shared_sha256": next(iter(digests)),
        "commits": commits,
    }


def _failure_scenarios(root: Path, config: Path) -> list[dict]:
    root.mkdir(parents=True, exist_ok=True)
    results = []

    rewrite_root = root / "rewrite-drift"
    rewrite_root.mkdir()
    canary = _clone_ring(rewrite_root, "canary")
    readme = canary / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "kody-w/rapp-installer",
            "kody-w/other-installer",
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    _commit(canary, "test: rewrite count drift")
    try:
        render_ring.render(
            canary,
            canary / ".ring" / "ring.json",
            root / "rewrite-drift-build",
        )
        raise ScenarioError("rewrite drift unexpectedly rendered")
    except render_ring.RenderError as error:
        if "rewrite count drift" not in str(error):
            raise
        results.append({"name": "rewrite-count-drift", "status": "blocked"})

    divergence_root = root / "shared-divergence"
    divergence_root.mkdir()
    canary = _clone_ring(divergence_root, "canary")
    nightly = _clone_ring(divergence_root, "nightly")
    canary_commit = _git(canary, "rev-parse", "HEAD^{commit}")
    canary_json = divergence_root / "canary.json"
    ring_attestation.create_attestation(
        "canary",
        canary,
        REPOSITORIES["canary"],
        canary_commit,
        config,
        canary_json,
        None,
    )
    readme = nightly / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nNightly-only shared drift.\n",
        encoding="utf-8",
        newline="\n",
    )
    nightly_commit = _commit(nightly, "test: divergent Nightly shared payload")
    try:
        ring_attestation.create_attestation(
            "nightly",
            nightly,
            REPOSITORIES["nightly"],
            nightly_commit,
            config,
            divergence_root / "nightly.json",
            canary_json,
        )
        raise ScenarioError("shared divergence unexpectedly attested")
    except ring_attestation.AttestationError as error:
        if "payload changed between rings" not in str(error):
            raise
        results.append({"name": "shared-payload-divergence", "status": "blocked"})

    deletion_root = root / "required-deletion"
    deletion_root.mkdir()
    canary = _clone_ring(deletion_root, "canary")
    nightly = _clone_ring(deletion_root, "nightly")
    required = (
        canary
        / "rapp_brainstem"
        / "agents"
        / "experimental"
        / "copilot_research_agent.py"
    )
    required.unlink()
    canary_commit = _commit(canary, "test: delete required shared agent")
    try:
        promote_ring.promote(
            canary,
            nightly,
            "canary",
            "nightly",
            canary_commit,
            _git(nightly, "rev-parse", "HEAD^{commit}"),
            config,
        )
        raise ScenarioError("required shared deletion unexpectedly promoted")
    except promote_ring.PromotionError as error:
        if "required shared paths are missing" not in str(error):
            raise
        results.append({"name": "required-file-deletion", "status": "blocked"})

    grail_root = root / "grail-guard"
    grail_root.mkdir()
    beta = _clone_ring(grail_root, "beta")
    grail = grail_root / "grail"
    _clone_url(
        "https://github.com/kody-w/rapp-installer.git",
        grail,
    )
    try:
        promote_ring.promote(
            beta,
            grail,
            "beta",
            "grail",
            _git(beta, "rev-parse", "HEAD^{commit}"),
            _git(grail, "rev-parse", "HEAD^{commit}"),
            config,
        )
        raise ScenarioError("automated Grail promotion unexpectedly succeeded")
    except promote_ring.PromotionError as error:
        if "human-controlled promotion" not in str(error):
            raise
        results.append({"name": "human-grail-guard", "status": "blocked"})

    return results


def _remote_main(repository: str) -> str:
    result = _run(
        [
            "git",
            "ls-remote",
            f"https://github.com/{repository}.git",
            "refs/heads/main",
        ],
        quiet=True,
        retries=3,
    )
    return result.stdout.split()[0]


def _markdown(report: dict) -> str:
    lines = [
        "# Autonomous pre-Grail test report",
        "",
        f"- Baseline Grail: `{report['grail_commit']}`",
        f"- Evidence mode: **{report['execution']}**",
        "- Qualification requires the separate candidate test job to pass.",
        f"- Successful feature scenarios: **{len(report['features'])}**",
        f"- Expected failure scenarios blocked: **{len(report['failures'])}**",
        "",
        "## Features",
        "",
        "| Scenario | Result | Shared digest |",
        "|---|---|---|",
    ]
    for item in report["features"]:
        lines.append(
            f"| {item['name']} | {item['status']} | "
            f"`{item['shared_sha256'][:16]}` |"
        )
    lines.extend(["", "## Failure cases", ""])
    for item in report["failures"]:
        lines.append(f"- `{item['name']}`: **{item['status']}**")
    lines.extend([
        "",
        "## Rollback",
        "",
        "All four ring `main` branches remained unchanged during the run.",
        "Grail remained at its independently sampled baseline SHA.",
        "Candidate processes used an isolated HOME/config with no explicit GitHub tokens.",
        "The hosted workflow repeats this on fresh read-only runners.",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--bash", required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument(
        "--evidence-only",
        action="store_true",
        help="Rebuild attestations and static evidence without executing candidate code.",
    )
    args = parser.parse_args()
    tools = Path(__file__).resolve().parent
    config = tools.parent / "train.json"
    grail_commit = _remote_main("kody-w/rapp-installer")
    initial = {
        ring: _remote_main(REPOSITORIES[ring]) for ring in RINGS
    }
    features = []
    failures = []
    caught = None
    try:
        with tempfile.TemporaryDirectory(prefix="rapp-autonomous-") as temp:
            root = Path(temp)
            for name, mutation in SCENARIOS.items():
                scenario_root = root / name
                scenario_root.mkdir(parents=True)
                features.append(
                    _promote_scenario(
                        scenario_root,
                        name,
                        mutation,
                        config,
                        args.python,
                        args.bash,
                        not args.evidence_only,
                    )
                )
            failures = _failure_scenarios(root / "failures", config)
    except Exception as error:
        caught = error
    finally:
        final = {
            ring: _remote_main(REPOSITORIES[ring]) for ring in RINGS
        }
        grail_final = _remote_main("kody-w/rapp-installer")
        if (
            initial != final
            or grail_final != grail_commit
        ):
            raise ScenarioError(
                "rollback invariant failed: a protected main moved"
            ) from caught
    if caught is not None:
        raise caught
    report = {
        "schema": "rapp-autonomous-test/1",
        "execution": "evidence-only" if args.evidence_only else "isolated-tests",
        "grail_commit": grail_commit,
        "features": features,
        "failures": failures,
        "ring_mains": final,
        "ring_mains_initial": initial,
        "grail_final": grail_final,
    }
    args.json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    args.markdown_output.write_text(
        _markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Passed {len(features)} feature scenarios and "
        f"{len(failures)} failure scenarios; ring mains and Grail were unchanged."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
