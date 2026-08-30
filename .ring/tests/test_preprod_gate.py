import gzip
import importlib.util
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / ".ring" / "tools" / "preprod_gate.py"
POLICY_TEMPLATE = ROOT / ".ring" / "preprod-policy.json"
POLICY = POLICY_TEMPLATE
SPEC = importlib.util.spec_from_file_location("preprod_gate", MODULE)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
import ring_attestation as RING  # noqa: E402


class PreprodGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "candidate"
        (self.source / "rapp_brainstem").mkdir(parents=True)
        GATE._git(self.source, "init", "-q")
        GATE._git(self.source, "branch", "-M", "main")
        GATE._git(self.source, "config", "user.name", "Preprod Test")
        GATE._git(self.source, "config", "user.email", "preprod@example.invalid")
        GATE._git(
            self.source,
            "remote",
            "add",
            "origin",
            "https://github.com/kody-w/rapp-installer.git",
        )
        (self.source / "rapp_brainstem" / "VERSION").write_text(
            "1.2.2\n", encoding="utf-8"
        )
        (self.source / "rapp_brainstem" / "brainstem.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "marker = os.environ.get('RAPP_TEST_LAUNCH_MARKER')\n"
            "if marker:\n"
            "    Path(marker).write_text('launched', encoding='utf-8')\n",
            encoding="utf-8",
        )
        (self.source / "install.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )
        (self.source / "obsolete.txt").write_text(
            "remove in candidate\n", encoding="utf-8"
        )
        GATE._git(self.source, "add", ".")
        GATE._git(self.source, "commit", "-qm", "rollback")
        GATE._git(self.source, "tag", "brainstem-v1.2.2")
        self.history = self.root / "brainstem-history"
        self.history.mkdir()
        self.rollback_frame = self.history / "brainstem-v1.2.2.json"
        self.rollback_frame_data = GATE.brainstem_history.create_frame(
            self.source,
            "brainstem-v1.2.2",
            self.rollback_frame,
        )

        (self.source / "rapp_brainstem" / "VERSION").write_text(
            "1.2.3\n", encoding="utf-8"
        )
        GATE._git(self.source, "add", ".")
        GATE._git(self.source, "commit", "-qm", "candidate")
        (self.source / "obsolete.txt").unlink()
        GATE._git(self.source, "add", "-u")
        (self.source / "runtime.tmp").write_text("not payload\n", encoding="utf-8")
        self.artifact = self.root / "rapp-preprod.tar.gz"
        self.manifest = self.root / "readiness.json"
        self.policy = self.root / "preprod-policy.json"
        policy = json.loads(POLICY_TEMPLATE.read_text(encoding="utf-8"))
        policy["grail_kernel"] = {
            "repository": "kody-w/rapp-installer",
            "release_scope": "https://github.com/kody-w/rapp-canary",
            "grail_id": GATE._grail_id(
                (self.source / "rapp_brainstem" / "brainstem.py").read_bytes()
            ),
            "immutable_ref": "refs/tags/brainstem-v1.2.2",
            "object_format": "sha1",
            "commit": self.rollback_frame_data["commit"],
            "path": "rapp_brainstem/brainstem.py",
            "mode": "100644",
            "blob": self.rollback_frame_data["brainstem"]["blob"],
            "sha256": self.rollback_frame_data["brainstem"]["sha256"],
            "size_bytes": self.rollback_frame_data["brainstem"]["size_bytes"],
            "policy": "immutable-forever",
        }
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        self.original_grail_kernel_pin = GATE.GRAIL_KERNEL_PIN
        GATE.GRAIL_KERNEL_PIN = policy["grail_kernel"]
        global POLICY
        POLICY = self.policy
        self.issued = datetime(2026, 8, 29, tzinfo=timezone.utc)
        self.soak_evidence = self.root / "soak.json"
        soak_started = self.issued - timedelta(minutes=30)
        soak_completed = self.issued - timedelta(minutes=5)
        soak_probes = [
            {
                "at": (
                    soak_started + timedelta(minutes=index)
                ).isoformat().replace("+00:00", "Z"),
                "status": "ok",
                "model_id": "gpt-4o",
            }
            for index in range(26)
        ]
        self.soak_evidence.write_text(
            json.dumps({
                "schema": "rapp/1:soak",
                "result": "passed",
                "canary_commit": "c" * 40,
                "beta_commit": "a" * 40,
                "qualification_run_id": "123",
                "model_id": "gpt-4o",
                "started_at": soak_started.isoformat().replace("+00:00", "Z"),
                "completed_at": soak_completed.isoformat().replace("+00:00", "Z"),
                "probe_interval_seconds": 60,
                "health_probe_count": len(soak_probes),
                "authenticated_chat_count": 2,
                "authenticated_chat_times": [
                    soak_started.isoformat().replace("+00:00", "Z"),
                    soak_completed.isoformat().replace("+00:00", "Z"),
                ],
                "probes": soak_probes,
                "checks": {
                    "authenticated_chat": True,
                    "state_isolated": True,
                    "health_stable": True,
                    "no_critical_events": True,
                },
            }),
            encoding="utf-8",
        )
        self.materials = {}
        for platform_name in ("linux", "macos", "windows"):
            material = self.root / f"material-{platform_name}"
            (material / "wheelhouse").mkdir(parents=True)
            (material / "test-wheelhouse").mkdir(parents=True)
            wheel = material / "wheelhouse" / f"example-1.0-{platform_name}.whl"
            wheel.write_bytes(f"wheel-{platform_name}".encode("utf-8"))
            test_wheel = (
                material
                / "test-wheelhouse"
                / f"pytest-9.1.1-{platform_name}.whl"
            )
            test_wheel.write_bytes(f"test-wheel-{platform_name}".encode("utf-8"))
            requirements = ["example==1.0"]
            test_requirements = ["pytest==9.1.1"]
            (material / "requirements.lock").write_text(
                "\n".join(requirements) + "\n",
                encoding="utf-8",
            )
            (material / "test-requirements.lock").write_text(
                "\n".join(test_requirements) + "\n",
                encoding="utf-8",
            )
            (material / "sbom.json").write_text(
                json.dumps({
                    "schema": "rapp-dependency-materials/1",
                    "platform": platform_name,
                    "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
                    "architecture": platform.machine().lower(),
                    "runtime_requirements": requirements,
                    "test_requirements": test_requirements,
                    "files": {
                        f"wheelhouse/{wheel.name}": GATE._sha256(wheel),
                        f"test-wheelhouse/{test_wheel.name}": GATE._sha256(test_wheel),
                    },
                }),
                encoding="utf-8",
            )
            (material / "vulnerability-report.json").write_text(
                json.dumps({"dependencies": [], "fixes": []}),
                encoding="utf-8",
            )
            (material / "licenses.json").write_text(
                json.dumps({
                    "schema": "rapp-license-report/1",
                    "platform": platform_name,
                    "licenses": {
                        f"wheelhouse/{wheel.name}": "MIT",
                        f"test-wheelhouse/{test_wheel.name}": "MIT",
                    },
                    "blocked": [],
                }),
                encoding="utf-8",
            )
            path = self.root / f"dependency-material-{platform_name}.tar.gz"
            GATE.build_artifact(material, path)
            self.materials[f"dependency-material-{platform_name}"] = path

    def tearDown(self):
        GATE.GRAIL_KERNEL_PIN = self.original_grail_kernel_pin
        self.temp.cleanup()

    def package(self, expires_hours=None):
        return GATE.package_candidate(
            self.source,
            self.artifact,
            self.manifest,
            POLICY,
            "a" * 40,
            "123",
            "https://github.com/kody-w/rapp-canary/actions/runs/123",
            "c" * 40,
            "456",
            "https://github.com/kody-w/rapp-beta/actions/runs/456",
            self.soak_evidence,
            "https://raw.githubusercontent.com/kody-w/rapp-canary/"
            + "f" * 40
            + "/.ring/soak/beta-a.json",
            GATE._sha256(self.soak_evidence),
            "release-engineering",
            "f" * 40,
            "gpt-4o",
            "brainstem-v1.2.2",
            self.rollback_frame,
            expires_hours=expires_hours,
            issued_at=self.issued,
        )

    def test_package_is_deterministic_and_ignores_untracked_runtime_files(self):
        first = self.package()
        first_bytes = self.artifact.read_bytes()
        second = self.package()
        self.assertEqual(first["subject"]["artifact_sha256"], second["subject"]["artifact_sha256"])
        self.assertEqual(first_bytes, self.artifact.read_bytes())
        with tarfile.open(self.artifact, "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("install.sh", names)
        self.assertNotIn("runtime.tmp", names)
        self.assertNotIn("obsolete.txt", names)

    def test_package_rejects_immutable_grail_kernel_drift(self):
        (self.source / "rapp_brainstem" / "brainstem.py").write_text(
            "print('changed kernel')\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(GATE.PreprodError, "kernel-drift"):
            self.package()

    def test_package_rejects_immutable_grail_mode_drift(self):
        GATE._git(
            self.source,
            "update-index",
            "--chmod=+x",
            "rapp_brainstem/brainstem.py",
        )
        with self.assertRaisesRegex(GATE.PreprodError, "mode or blob"):
            self.package()

    def test_launch_failure_terminates_the_runtime(self):
        self.package()
        destination = self.root / "failed-launch-runtime"
        GATE.prepare_runtime(
            self.artifact,
            self.manifest,
            destination,
            self.root / "failed-launch-state",
            POLICY,
            {"dependency-material-linux": self.materials["dependency-material-linux"]},
            platform_name="linux",
            verify_provenance=False,
            install_dependencies=False,
            allow_candidate=True,
            now=self.issued + timedelta(hours=1),
        )
        python = destination / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        os.symlink(sys.executable, python)
        unwritable = self.root / "evidence-directory"
        unwritable.mkdir()
        prepared = {
            "source": destination / "src",
            "venv": destination / "venv",
            "deployment": destination / "deployment.json",
            "manifest": json.loads(self.manifest.read_text(encoding="utf-8")),
            "material_name": "dependency-material-linux",
            "material_sha256": GATE._sha256(
                self.materials["dependency-material-linux"]
            ),
        }
        with (
            mock.patch.object(GATE, "prepare_runtime", return_value=prepared),
            mock.patch.object(GATE, "verify_grail_kernel_bytes"),
            mock.patch.object(GATE.subprocess, "Popen") as popen,
        ):
            process = popen.return_value
            process.stdin = mock.Mock()
            process.pid = 123
            process.poll.return_value = None
            with self.assertRaises(OSError):
                GATE.launch_runtime(
                    self.artifact,
                    self.manifest,
                    self.root / "failed-launch-state",
                    self.policy,
                    {"dependency-material-linux": self.materials["dependency-material-linux"]},
                    unwritable,
                    platform_name="linux",
                    allow_candidate=True,
                    verify_provenance=False,
                    now=self.issued + timedelta(hours=1),
                )
            process.terminate.assert_called_once()
            process.wait.assert_called_once()

    def test_policy_cannot_redefine_the_immutable_grail(self):
        policy = json.loads(self.policy.read_text(encoding="utf-8"))
        policy["grail_kernel"]["sha256"] = "0" * 64
        self.policy.write_text(json.dumps(policy), encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "immutable Grail kernel"):
            GATE._validate_policy(policy)

    def test_policy_cannot_redefine_the_rapp1_authority(self):
        policy = json.loads(self.policy.read_text(encoding="utf-8"))
        policy["rapp1_authority"]["revision"] = "rev-999"
        with self.assertRaisesRegex(GATE.PreprodError, "RAPP/1"):
            GATE._validate_policy(policy)

    def test_policy_cannot_weaken_release_controls(self):
        for key, value in (
            ("minimum_soak_minutes", 1),
            ("max_candidate_age_hours", 720),
        ):
            with self.subTest(key=key):
                policy = json.loads(self.policy.read_text(encoding="utf-8"))
                policy[key] = value
                with self.assertRaises(GATE.PreprodError):
                    GATE._validate_policy(policy)
        policy = json.loads(self.policy.read_text(encoding="utf-8"))
        policy["required_checks"] = ["immutable-grail-kernel"]
        with self.assertRaisesRegex(GATE.PreprodError, "required_checks"):
            GATE._validate_policy(policy)

    def test_preprod_control_plane_cannot_enter_the_shared_grail_payload(self):
        config = RING._read_json(ROOT / ".ring" / "train.json")
        prefixes = RING._ring_owned_prefixes(config)
        for path in (
            ".ring/PREPROD.md",
            ".ring/SEAWORTHINESS-CONSTITUTION.md",
            ".ring/brainstem-frame.schema.json",
            ".ring/brainstem-history/brainstem-v0.6.15.json",
            ".ring/preprod-policy.json",
            ".ring/readiness.schema.json",
            ".ring/soak.schema.json",
            ".ring/soak/README.md",
            ".ring/tooling/pip-audit.lock",
            ".ring/tools/archive_preprod.sh",
            ".ring/tools/brainstem_history.py",
            ".ring/tools/build_dependency_material.py",
            ".ring/tools/preprod_gate.py",
            ".github/workflows/stage-preprod.yml",
            ".github/workflows/test-pre-grail-rings.yml",
        ):
            self.assertTrue(
                RING._is_ring_owned(path, prefixes),
                f"{path} could leak into the Grail payload",
            )

    def test_workflow_isolates_control_python_and_uses_safe_extraction(self):
        workflow = (
            ROOT / ".github" / "workflows" / "stage-preprod.yml"
        ).read_text(encoding="utf-8").replace("\r\n", "\n")
        for line in workflow.splitlines():
            command = line.strip()
            if command.startswith(("python ", "python3 ")):
                self.assertRegex(command, r"^python3? -I(?: |$)", command)
        self.assertNotIn("tar -x", workflow)
        self.assertIn("prepare-candidate-runtime", workflow)
        self.assertIn(".ring/tooling/pip-audit.lock", workflow)
        self.assertNotIn(
            "python -I -m pip install --quiet pip-audit",
            workflow,
        )
        builder = (
            ROOT / ".ring" / "tools" / "build_dependency_material.py"
        ).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn('"--force-reinstall"', builder)
        self.assertIn('"--require-hashes"', builder)
        self.assertIn("brainstem-history", workflow)
        self.assertIn('export BRAINSTEM_VERSION="$ROLLBACK_COMMIT"', workflow)
        qualification = (
            ROOT / ".github" / "workflows" / "test-pre-grail-rings.yml"
        ).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("verify-kernel --repo", qualification)
        autonomous = (
            ROOT / ".github" / "workflows" / "autonomous-pre-grail.yml"
        ).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("Enforce immutable Grail kernel", autonomous)

    def test_soak_tool_records_live_candidate_bound_evidence(self):
        source = (
            ROOT / ".ring" / "tools" / "soak.sh"
        ).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("soak.sh evidence", source)
        self.assertIn('SOAK_REF="${SOAK_REF:-main}"', source)
        self.assertIn('rm -rf "$SOAK_HOME/venv"', source)
        self.assertIn('curl -fsS -X POST "http://localhost:$SOAK_PORT/chat"', source)
        self.assertIn(
            "real-auth evidence cannot be created from a --no-auth soak",
            source,
        )
        self.assertIn("soak probe history contains an unhealthy interval", source)
        self.assertIn("belongs to another process; refusing to kill it", source)
        self.assertIn("stop_owned_process", source)
        self.assertIn('"qualification_run_id": sys.argv[9]', source)
        self.assertNotIn(".copilot_token\"", source.split('value = {', 1)[-1])

    def test_archive_requires_canonical_run_and_provenance(self):
        source = (
            ROOT / ".ring" / "tools" / "archive_preprod.sh"
        ).read_text(encoding="utf-8").replace("\r\n", "\n")
        for required in (
            '".github/workflows/stage-preprod.yml"',
            '"workflow_dispatch"',
            "head_branch",
            '"evidence"]["preprod"]["run_id"]',
            '"evidence"]["control_plane"]["commit"]',
            'job.get("name") == "seal"',
            "--verify-provenance",
        ):
            self.assertIn(required, source)

    def test_verify_accepts_the_exact_unexpired_artifact(self):
        expected = self.package()
        actual = GATE.verify_candidate(
            self.artifact,
            self.manifest,
            POLICY,
            now=self.issued + timedelta(hours=1),
            expected_beta_commit="a" * 40,
            expected_qualification_run="123",
        )
        self.assertEqual(actual, expected)

    def test_verify_rejects_artifact_tampering(self):
        self.package()
        with self.artifact.open("ab") as handle:
            handle.write(b"tampered")
        with self.assertRaisesRegex(GATE.PreprodError, "digest"):
            GATE.verify_candidate(self.artifact, self.manifest, POLICY, now=self.issued)

    def test_verify_rejects_expired_readiness(self):
        self.package()
        with self.assertRaisesRegex(GATE.PreprodError, "expired"):
            GATE.verify_candidate(
                self.artifact,
                self.manifest,
                POLICY,
                now=self.issued + timedelta(days=8),
            )
        archived = GATE.verify_candidate(
            self.artifact,
            self.manifest,
            POLICY,
            now=self.issued + timedelta(days=8),
            allow_expired=True,
        )
        self.assertEqual(archived["status"], "preprod-candidate")

    def test_zero_hour_readiness_is_rejected(self):
        with self.assertRaisesRegex(GATE.PreprodError, "lifetime"):
            self.package(expires_hours=0)

    def test_verify_rejects_unsafe_archive_members(self):
        self.package()
        raw = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                info = tarfile.TarInfo("../escape")
                payload = b"bad"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        self.artifact.write_bytes(raw.getvalue())
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["subject"]["artifact_sha256"] = GATE._sha256(self.artifact)
        value["subject"]["size_bytes"] = self.artifact.stat().st_size
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "unsafe artifact"):
            GATE.verify_candidate(self.artifact, self.manifest, POLICY, now=self.issued)

    def test_verify_rejects_cross_platform_unsafe_archive_names(self):
        for names in (
            [r"..\escape"],
            ["NUL.txt"],
            ["trailing. "],
            ["Case.txt", "case.txt"],
            ["caf\u00e9.txt", "cafe\u0301.txt"],
        ):
            with self.subTest(names=names):
                raw = io.BytesIO()
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, mtime=0
                ) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w") as archive:
                        for name in names:
                            payload = b"bad"
                            info = tarfile.TarInfo(name)
                            info.size = len(payload)
                            archive.addfile(info, io.BytesIO(payload))
                unsafe = self.root / "unsafe.tar.gz"
                unsafe.write_bytes(raw.getvalue())
                with self.assertRaisesRegex(
                    GATE.PreprodError,
                    "unsafe artifact|cross-platform duplicate",
                ):
                    GATE._validate_archive(unsafe)

    def test_requirements_must_be_registry_only(self):
        requirements = self.root / "requirements.txt"
        requirements.write_text(
            "flask>=2.0.0\nrequests>=2.28.0,<3\n",
            encoding="utf-8",
        )
        self.assertEqual(
            GATE._requirement_lines(requirements),
            ["flask>=2.0.0", "requests>=2.28.0,<3"],
        )
        for unsafe in (
            "./local-package",
            "../parent",
            "file:///tmp/package",
            "name @ https://example.invalid/name.whl",
            "-r other.txt",
            "${PACKAGE_NAME}==1.0",
        ):
            with self.subTest(unsafe=unsafe):
                requirements.write_text(unsafe + "\n", encoding="utf-8")
                with self.assertRaisesRegex(GATE.PreprodError, "registry package"):
                    GATE._requirement_lines(requirements)

    def test_soak_evidence_is_bound_to_candidate_model_and_time(self):
        value = json.loads(self.soak_evidence.read_text(encoding="utf-8"))
        value["model_id"] = "another-model"
        self.soak_evidence.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "candidate and model"):
            self.package()

    def test_soak_evidence_requires_probe_coverage(self):
        value = json.loads(self.soak_evidence.read_text(encoding="utf-8"))
        value["health_probe_count"] = 1
        self.soak_evidence.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "authenticated interval"):
            self.package()

    def test_provenance_uses_exact_certificate_identity_and_source_digest(self):
        manifest = self.package()
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(GATE.subprocess, "run", return_value=completed) as run:
            GATE._verify_github_provenance(manifest, (self.artifact,))
        command = run.call_args.args[0]
        self.assertIn("--cert-identity", command)
        self.assertEqual(
            command[command.index("--cert-identity") + 1],
            GATE.PREPROD_CERT_IDENTITY,
        )
        self.assertEqual(
            command[command.index("--source-digest") + 1],
            "f" * 40,
        )

    def test_verify_rejects_git_metadata_members(self):
        self.package()
        raw = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                info = tarfile.TarInfo(".GiT/hooks/post-index-change")
                payload = b"#!/bin/sh\nexit 99\n"
                info.mode = 0o755
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        self.artifact.write_bytes(raw.getvalue())
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["subject"]["artifact_sha256"] = GATE._sha256(self.artifact)
        value["subject"]["size_bytes"] = self.artifact.stat().st_size
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "unsafe artifact"):
            GATE.verify_candidate(self.artifact, self.manifest, POLICY, now=self.issued)

    def test_human_approval_seals_the_same_artifact(self):
        candidate = self.package()
        self.assertTrue(
            any(
                result["status"] == "pending"
                for result in candidate["evidence"]["controls"].values()
            )
        )
        sealed_path = self.root / "seaworthy.json"
        sealed = GATE.seal_candidate(
            self.artifact,
            self.manifest,
            sealed_path,
            POLICY,
            "789",
            "https://github.com/kody-w/rapp-canary/actions/runs/789",
            "github-environment:preprod",
            self.materials,
            sealed_at=self.issued + timedelta(hours=2),
        )
        self.assertEqual(sealed["status"], "seaworthy")
        self.assertEqual(
            sealed["subject"]["artifact_sha256"],
            candidate["subject"]["artifact_sha256"],
        )
        verified = GATE.verify_candidate(
            self.artifact,
            sealed_path,
            POLICY,
            now=self.issued + timedelta(hours=3),
            materials=self.materials,
        )
        self.assertEqual(
            verified["evidence"]["preprod"]["approval_authority"],
            "github-environment:preprod",
        )
        self.assertTrue(
            all(
                result["status"] == "passed"
                for result in verified["evidence"]["controls"].values()
            )
        )

    def test_unknown_control_blocks_readiness(self):
        self.package()
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        control = next(iter(value["evidence"]["controls"].values()))
        control["status"] = "unknown"
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "blocking control is unknown"):
            GATE.verify_candidate(
                self.artifact,
                self.manifest,
                POLICY,
                now=self.issued,
            )

    def test_sealed_dependency_material_tampering_is_rejected(self):
        self.package()
        sealed_path = self.root / "seaworthy.json"
        GATE.seal_candidate(
            self.artifact,
            self.manifest,
            sealed_path,
            POLICY,
            "789",
            "https://github.com/kody-w/rapp-canary/actions/runs/789",
            "github-environment:preprod",
            self.materials,
            sealed_at=self.issued + timedelta(hours=2),
        )
        self.materials["dependency-material-linux"].write_bytes(b"changed")
        with self.assertRaises(GATE.PreprodError):
            GATE.verify_candidate(
                self.artifact,
                sealed_path,
                POLICY,
                now=self.issued + timedelta(hours=3),
                materials=self.materials,
            )

    def test_only_a_seaworthy_artifact_exports_to_a_grail_release_branch(self):
        self.package()
        sealed_path = self.root / "seaworthy.json"
        GATE.seal_candidate(
            self.artifact,
            self.manifest,
            sealed_path,
            POLICY,
            "789",
            "https://github.com/kody-w/rapp-canary/actions/runs/789",
            "github-environment:preprod",
            self.materials,
            sealed_at=self.issued + timedelta(hours=2),
        )
        target = self.root / "grail"
        result = subprocess.run(
            ["git", "clone", "-q", str(self.source), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        GATE._git(target, "config", "user.name", "Preprod Test")
        GATE._git(target, "config", "user.email", "preprod@example.invalid")
        GATE._git(
            target,
            "remote",
            "set-url",
            "origin",
            "https://github.com/kody-w/rapp-installer.git",
        )
        GATE._git(target, "checkout", "-qb", "release/v1.2.3")

        changed = GATE.export_candidate(
            self.artifact,
            sealed_path,
            self.rollback_frame,
            target,
            POLICY,
            now=self.issued + timedelta(hours=3),
            verify_provenance=False,
            materials=self.materials,
        )
        self.assertGreater(changed, 0)
        self.assertFalse((target / "obsolete.txt").exists())
        self.assertEqual(
            (target / "rapp_brainstem" / "VERSION").read_text(encoding="utf-8"),
            "1.2.3\n",
        )
        self.assertTrue(GATE._git(target, "status", "--porcelain").strip())
        self.assertEqual(
            GATE.verify_staged_tree(
                self.artifact,
                sealed_path,
                target,
                POLICY,
                self.materials,
                now=self.issued + timedelta(hours=3),
                verify_provenance=False,
            ),
            json.loads(sealed_path.read_text(encoding="utf-8"))["subject"]["expected_grail_tree"],
        )
        (target / "install.sh").write_text("changed after Preprod\n", encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "unstaged"):
            GATE.verify_staged_tree(
                self.artifact,
                sealed_path,
                target,
                POLICY,
                self.materials,
                now=self.issued + timedelta(hours=3),
                verify_provenance=False,
            )
        (target / "install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        release_tree = GATE._git(target, "write-tree").strip()
        GATE._git(target, "commit", "-qm", "release: v1.2.3")
        release_commit = GATE.verify_release_commit(
            self.artifact,
            sealed_path,
            target,
            POLICY,
            self.materials,
            now=self.issued + timedelta(hours=3),
            verify_provenance=False,
        )
        self.assertEqual(GATE._git(target, "rev-parse", "HEAD^{tree}").strip(), release_tree)
        base = json.loads(sealed_path.read_text(encoding="utf-8"))["subject"]["grail_base_commit"]
        GATE._git(target, "checkout", "-q", "main")
        self.assertEqual(GATE._git(target, "rev-parse", "HEAD").strip(), base)
        GATE._git(
            target,
            "merge",
            "--no-ff",
            "release/v1.2.3",
            "-m",
            "release: v1.2.3",
        )
        merge_commit = GATE.verify_final_merge(
            self.artifact,
            sealed_path,
            target,
            release_commit,
            POLICY,
            self.materials,
            now=self.issued + timedelta(hours=3),
            verify_provenance=False,
        )
        self.assertEqual(merge_commit, GATE._git(target, "rev-parse", "HEAD").strip())

    def test_export_rejects_a_moved_grail_base(self):
        self.package()
        sealed_path = self.root / "seaworthy.json"
        GATE.seal_candidate(
            self.artifact,
            self.manifest,
            sealed_path,
            POLICY,
            "789",
            "https://github.com/kody-w/rapp-canary/actions/runs/789",
            "github-environment:preprod",
            self.materials,
            sealed_at=self.issued + timedelta(hours=2),
        )
        target = self.root / "moved-grail"
        result = subprocess.run(
            ["git", "clone", "-q", str(self.source), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        GATE._git(target, "config", "user.name", "Preprod Test")
        GATE._git(target, "config", "user.email", "preprod@example.invalid")
        GATE._git(
            target,
            "remote",
            "set-url",
            "origin",
            "https://github.com/kody-w/rapp-installer.git",
        )
        (target / "newer.txt").write_text("newer Grail\n", encoding="utf-8")
        GATE._git(target, "add", ".")
        GATE._git(target, "commit", "-qm", "Grail moved")
        GATE._git(target, "checkout", "-qb", "release/v1.2.3")
        with self.assertRaisesRegex(GATE.PreprodError, "Grail base moved"):
            GATE.export_candidate(
                self.artifact,
                sealed_path,
                self.rollback_frame,
                target,
                POLICY,
                now=self.issued + timedelta(hours=3),
                verify_provenance=False,
                materials=self.materials,
            )

    def test_prepare_runtime_uses_the_sealed_platform_material(self):
        self.package()
        sealed_path = self.root / "seaworthy.json"
        GATE.seal_candidate(
            self.artifact,
            self.manifest,
            sealed_path,
            POLICY,
            "789",
            "https://github.com/kody-w/rapp-canary/actions/runs/789",
            "github-environment:preprod",
            self.materials,
            sealed_at=self.issued + timedelta(hours=2),
        )
        destination = self.root / "runtime"
        state_dir = self.root / "state"
        result = GATE.prepare_runtime(
            self.artifact,
            sealed_path,
            destination,
            state_dir,
            POLICY,
            self.materials,
            platform_name="linux",
            verify_provenance=False,
            install_dependencies=False,
            now=self.issued + timedelta(hours=3),
        )
        self.assertTrue((result["source"] / "rapp_brainstem" / "brainstem.py").is_file())
        self.assertTrue(
            (destination / "dependencies" / "requirements.lock").is_file()
        )
        self.assertTrue(
            (destination / "dependencies" / "test-requirements.lock").is_file()
        )
        deployment = json.loads(result["deployment"].read_text(encoding="utf-8"))
        self.assertEqual(deployment["material"], "dependency-material-linux")
        self.assertEqual(deployment["state_dir"], str(state_dir.resolve()))
        self.assertEqual(
            deployment["resolved_kernel_path"],
            str((destination / "src" / "rapp_brainstem" / "brainstem.py").resolve()),
        )
        self.assertIn(
            "GITHUB_MODEL=gpt-4o",
            (destination / "runtime.env").read_text(encoding="utf-8"),
        )

    def test_prepare_candidate_runtime_exercises_the_unsealed_platform_material(self):
        self.package()
        destination = self.root / "candidate-runtime"
        result = GATE.prepare_runtime(
            self.artifact,
            self.manifest,
            destination,
            self.root / "candidate-state",
            POLICY,
            {"dependency-material-linux": self.materials["dependency-material-linux"]},
            platform_name="linux",
            verify_provenance=False,
            install_dependencies=False,
            allow_candidate=True,
            now=self.issued + timedelta(hours=1),
        )
        deployment = json.loads(result["deployment"].read_text(encoding="utf-8"))
        self.assertEqual(deployment["schema"], "rapp-preprod-deployment/1")
        self.assertEqual(
            deployment["grail_id"],
            json.loads(self.policy.read_text(encoding="utf-8"))["grail_kernel"]["grail_id"],
        )
        self.assertEqual(
            deployment["material_sha256"],
            GATE._sha256(self.materials["dependency-material-linux"]),
        )
        python = destination / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        os.symlink(sys.executable, python)
        marker = self.root / "kernel-launched"
        with mock.patch.dict(
            os.environ,
            {"RAPP_TEST_LAUNCH_MARKER": str(marker)},
        ):
            launch_evidence = self.root / "kernel-launch.json"
            with mock.patch.object(
                GATE,
                "prepare_runtime",
                return_value=result,
            ):
                self.assertEqual(
                    GATE.launch_runtime(
                        self.artifact,
                        self.manifest,
                        self.root / "candidate-state",
                        self.policy,
                        {"dependency-material-linux": self.materials["dependency-material-linux"]},
                        launch_evidence,
                        platform_name="linux",
                        allow_candidate=True,
                        verify_provenance=False,
                        now=self.issued + timedelta(hours=1),
                    ),
                    0,
                )
        self.assertEqual(marker.read_text(encoding="utf-8"), "launched")
        evidence = json.loads(launch_evidence.read_text(encoding="utf-8"))
        self.assertEqual(evidence["execution_mode"], "verified-memory-snapshot")
        self.assertEqual(
            evidence["grail_id"],
            json.loads(self.policy.read_text(encoding="utf-8"))["grail_kernel"]["grail_id"],
        )

    def test_launch_rejects_persisted_model_drift(self):
        self.package()
        state_dir = self.root / "model-drift-state"
        state_dir.mkdir()
        (state_dir / ".brainstem_model").write_text(
            '{"model":"different-model"}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(GATE.PreprodError, "persisted model"):
            GATE.launch_runtime(
                self.artifact,
                self.manifest,
                state_dir,
                self.policy,
                {"dependency-material-linux": self.materials["dependency-material-linux"]},
                self.root / "model-drift-launch.json",
                allow_candidate=True,
                verify_provenance=False,
                now=self.issued + timedelta(hours=1),
            )


if __name__ == "__main__":
    unittest.main()
