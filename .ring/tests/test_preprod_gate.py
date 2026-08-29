import gzip
import importlib.util
import io
import json
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / ".ring" / "tools" / "preprod_gate.py"
POLICY = ROOT / ".ring" / "preprod-policy.json"
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
            "print('rollback')\n", encoding="utf-8"
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
        self.rollback_frame = self.root / "rollback-brainstem.json"
        GATE.brainstem_history.create_frame(
            self.source,
            "brainstem-v1.2.2",
            self.rollback_frame,
        )

        (self.source / "rapp_brainstem" / "VERSION").write_text(
            "1.2.3\n", encoding="utf-8"
        )
        (self.source / "rapp_brainstem" / "brainstem.py").write_text(
            "print('candidate')\n", encoding="utf-8"
        )
        GATE._git(self.source, "add", ".")
        GATE._git(self.source, "commit", "-qm", "candidate")
        (self.source / "obsolete.txt").unlink()
        GATE._git(self.source, "add", "-u")
        (self.source / "runtime.tmp").write_text("not payload\n", encoding="utf-8")
        self.artifact = self.root / "rapp-preprod.tar.gz"
        self.manifest = self.root / "readiness.json"
        self.materials = {}
        for platform_name in ("linux", "macos", "windows"):
            material = self.root / f"material-{platform_name}"
            (material / "wheelhouse").mkdir(parents=True)
            wheel = material / "wheelhouse" / f"example-1.0-{platform_name}.whl"
            wheel.write_bytes(f"wheel-{platform_name}".encode("utf-8"))
            requirements = ["example==1.0"]
            (material / "requirements.lock").write_text(
                "\n".join(requirements) + "\n",
                encoding="utf-8",
            )
            (material / "sbom.json").write_text(
                json.dumps({
                    "schema": "rapp-dependency-materials/1",
                    "platform": platform_name,
                    "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
                    "architecture": platform.machine().lower(),
                    "requirements": requirements,
                    "files": {
                        wheel.name: GATE._sha256(wheel),
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
                    "licenses": {wheel.name: "MIT"},
                    "blocked": [],
                }),
                encoding="utf-8",
            )
            path = self.root / f"dependency-material-{platform_name}.tar.gz"
            GATE.build_artifact(material, path)
            self.materials[f"dependency-material-{platform_name}"] = path
        self.issued = datetime(2026, 8, 29, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def package(self):
        return GATE.package_candidate(
            self.source,
            self.artifact,
            self.manifest,
            POLICY,
            "a" * 40,
            "123",
            "https://github.com/kody-w/rapp-canary/actions/runs/123",
            "456",
            "https://github.com/kody-w/rapp-beta/actions/runs/456",
            "https://github.com/kody-w/rapp-canary/issues/99",
            "release-engineering",
            "f" * 40,
            "gpt-4o",
            "brainstem-v1.2.2",
            self.rollback_frame,
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
            ".ring/tools/archive_preprod.sh",
            ".ring/tools/brainstem_history.py",
            ".ring/tools/preprod_gate.py",
            ".github/workflows/stage-preprod.yml",
        ):
            self.assertTrue(
                RING._is_ring_owned(path, prefixes),
                f"{path} could leak into the Grail payload",
            )

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
            GATE.verify_staged_tree(sealed_path, target),
            json.loads(sealed_path.read_text(encoding="utf-8"))["subject"]["expected_grail_tree"],
        )
        (target / "install.sh").write_text("changed after Preprod\n", encoding="utf-8")
        with self.assertRaisesRegex(GATE.PreprodError, "unstaged"):
            GATE.verify_staged_tree(sealed_path, target)

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
        )
        self.assertTrue((result["source"] / "rapp_brainstem" / "brainstem.py").is_file())
        self.assertTrue(
            (destination / "dependencies" / "requirements.lock").is_file()
        )
        deployment = json.loads(result["deployment"].read_text(encoding="utf-8"))
        self.assertEqual(deployment["material"], "dependency-material-linux")
        self.assertEqual(deployment["state_dir"], str(state_dir.resolve()))
        self.assertIn(
            "GITHUB_MODEL=gpt-4o",
            (destination / "runtime.env").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
