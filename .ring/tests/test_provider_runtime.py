"""Kernel-preserving render and sealed-bootstrap contracts for provider profiles."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / ".ring" / "tools"
sys.path.insert(0, str(TOOLS))
import preprod_gate as GATE
import render_ring as RENDER


def _git(repo, *args):
    environment = {
        **os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=environment,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class ProviderRenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "--template=")
        _git(self.repo, "config", "user.name", "Provider Render Fixture")
        _git(self.repo, "config", "user.email", "fixture@example.invalid")
        _git(self.repo, "config", "core.autocrlf", "false")
        engine = self.repo / "rapp_brainstem"
        engine.mkdir()
        self.kernel = (ROOT / "rapp_brainstem" / "brainstem.py").read_bytes()
        (engine / "brainstem.py").write_bytes(self.kernel)
        (engine / "launch.py").write_text("# renderer fixture; never executed\n")
        self.profile = json.loads(
            (ROOT / "rapp_brainstem" / "runtime_profile.json").read_text(encoding="utf-8")
        )
        (engine / "runtime_profile.json").write_text(json.dumps(self.profile))
        ring_dir = self.repo / ".ring"
        ring_dir.mkdir()
        self.config = ring_dir / "ring.json"
        self.config.write_text(json.dumps({
            "schema": "rapp-ring/1", "name": "canary",
            "rewrites": [{
                "from": "kody-w/rapp-support", "to": "kody-w/rapp-canary", "expected_count": 1,
            }],
            "protected_paths": [".ring/"],
        }))
        self.commit()

    def commit(self):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--no-gpg-sign", "-qm", "fixture")

    def test_profile_changes_identity_without_rewriting_kernel(self):
        output = self.root / "render"
        report = RENDER.render(self.repo, self.config, output)
        self.assertEqual((output / "rapp_brainstem" / "brainstem.py").read_bytes(), self.kernel)
        self.assertEqual(report["kernel_sha256"], RENDER.KERNEL_SHA256)
        rendered_profile = json.loads((output / "rapp_brainstem" / "runtime_profile.json").read_text())
        self.assertEqual(rendered_profile["support_repository"], "kody-w/rapp-canary")
        self.assertEqual(rendered_profile["kernel_sha256"], RENDER.KERNEL_SHA256)

    def test_kernel_drift_is_not_repaired_or_hidden_by_rendering(self):
        kernel = self.repo / RENDER.KERNEL_PATH
        kernel.write_bytes(self.kernel + b"\n# forbidden mutation\n")
        self.commit()
        with self.assertRaisesRegex(RENDER.RenderError, "kernel-drift"):
            RENDER.render(self.repo, self.config, self.root / "drifted")
        self.assertNotEqual(kernel.read_bytes(), self.kernel)

    def test_kernel_exemption_is_exact_not_a_prefix(self):
        impostor = self.repo / "rapp_brainstem" / "brainstem.py.notes"
        impostor.write_text("kody-w/rapp-support\n")
        self.commit()
        with self.assertRaisesRegex(RENDER.RenderError, "rewrite count drift"):
            RENDER.render(self.repo, self.config, self.root / "prefix-drift")

    def test_incomplete_provider_profile_cannot_use_legacy_rendering(self):
        (self.repo / "rapp_brainstem" / "runtime_profile.json").unlink()
        self.commit()
        with self.assertRaisesRegex(RENDER.RenderError, "missing its explicit profile"):
            RENDER.render(self.repo, self.config, self.root / "missing-profile")

    def test_unknown_profile_version_is_rejected(self):
        self.profile["provider_api"] = 2
        (self.repo / "rapp_brainstem" / "runtime_profile.json").write_text(json.dumps(self.profile))
        self.commit()
        with self.assertRaisesRegex(RENDER.RenderError, "incompatible"):
            RENDER.render(self.repo, self.config, self.root / "future-profile")


class ProviderBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "brainstem.py"
        self.path.write_text("raise AssertionError('mutable kernel path was reopened')\n")
        self.source = b"print('verified-kernel-fixture')\n"

    def run_bootstrap(self):
        return subprocess.run(
            [sys.executable, "-I", "-c", GATE.KERNEL_BOOTSTRAP, str(self.path)],
            input=self.source, capture_output=True,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        )

    def test_legacy_bootstrap_executes_verified_bytes_not_mutable_path(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.strip(), b"verified-kernel-fixture")

    def test_provider_bootstrap_receives_the_same_verified_bytes(self):
        (self.root / "runtime_profile.json").write_text("{}")
        (self.root / "launch.py").write_text(
            "import hashlib\n"
            "def run_verified_kernel(source, path):\n"
            "    print('provider-v1:' + hashlib.sha256(source).hexdigest())\n"
            "    return 0\n"
        )
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(
            result.stdout.strip().decode(),
            "provider-v1:" + hashlib.sha256(self.source).hexdigest(),
        )

    def test_incomplete_provider_composition_is_not_a_legacy_fallback(self):
        (self.root / "launch.py").write_text("raise AssertionError('must not import without a profile')\n")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"verified-kernel-fixture", result.stdout)
        self.assertIn(b"Incomplete provider runtime", result.stderr)

    def test_sealed_launch_forces_the_artifact_provider_selection(self):
        source_dir = self.root / "runtime" / "src"
        engine = source_dir / "rapp_brainstem"
        engine.mkdir(parents=True)
        kernel_path = engine / "brainstem.py"
        kernel_path.write_bytes(self.source)
        kernel = {
            "path": "rapp_brainstem/brainstem.py",
            "sha256": hashlib.sha256(self.source).hexdigest(),
            "grail_id": GATE._grail_id(self.source),
            "size_bytes": len(self.source), "release_scope": "fixture",
        }
        profile = {
            "schema": "brainstem-runtime-profile/1", "entrypoint": "launch.py",
            "kernel_sha256": kernel["sha256"], "provider_api": 1, "providers": ["responses"],
        }
        (engine / "runtime_profile.json").write_text(json.dumps(profile))
        venv = self.root / "venv"
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.parent.mkdir(parents=True)
        python.write_text("fixture; never executed")
        manifest = {
            "runtime": {"model_id": "gpt-6-astra"},
            "subject": {"artifact_sha256": "a" * 64},
        }
        prepared = {
            "source": source_dir, "venv": venv, "manifest": manifest,
            "material_name": "fixture-material", "material_sha256": "b" * 64,
        }
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps({"grail_kernel": kernel}))
        process = mock.Mock()
        process.pid = 123456
        process.wait.return_value = 0
        evidence = self.root / "evidence.json"
        with mock.patch.object(GATE, "_validate_policy"), \
             mock.patch.object(GATE, "verify_candidate", return_value=manifest), \
             mock.patch.object(GATE, "prepare_runtime", return_value=prepared), \
             mock.patch.object(GATE.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.dict(os.environ, {
                 "PATH": os.environ.get("PATH", ""), "BRAINSTEM_PROVIDER_PLUGINS": "unqualified-extra",
             }, clear=True):
            result = GATE.launch_runtime(
                self.root / "artifact.tar.gz", self.root / "manifest.json",
                self.root / "state", policy_path, {}, evidence,
            )
        self.assertEqual(result, 0)
        self.assertEqual(popen.call_args.kwargs["env"]["BRAINSTEM_PROVIDER_PLUGINS"], "responses")
        process.stdin.write.assert_called_once_with(self.source)
        receipt = json.loads(evidence.read_text())
        self.assertEqual(receipt["provider_plugins"], ["responses"])
        self.assertEqual(receipt["runtime_profile"], "brainstem-runtime-profile/1")
        self.assertEqual(receipt["execution_mode"], "verified-memory-snapshot")


if __name__ == "__main__":
    unittest.main()
