import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / ".ring" / "tools" / "autonomous_test.py"
sys.path.insert(0, str(MODULE.parent))
SPEC = importlib.util.spec_from_file_location("autonomous_test", MODULE)
AUTONOMOUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTONOMOUS)


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _repository(root, ring, count=1):
    repo = root / ring
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.name", "Autonomous Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text(
        "kody-w/rapp-installer\n" * count, encoding="utf-8", newline="\n"
    )
    (repo / ".ring").mkdir()
    (repo / ".ring" / "ring.json").write_text(
        json.dumps({
            "schema": "rapp-ring/1",
            "name": ring,
            "repository": f"kody-w/rapp-{ring}",
            "pages_url": f"https://kody-w.github.io/rapp-{ring}",
            "parent": None if ring == "canary" else "canary",
            "rewrites": [{
                "from": "kody-w/rapp-installer",
                "to": f"kody-w/rapp-{ring}",
                "expected_count": count,
            }],
            "protected_paths": [".ring/", ".github/workflows/"],
            "rewrite_excluded_prefixes": ["tests/"],
        }),
        encoding="utf-8",
        newline="\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixture")
    return repo


class AutonomousInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repos = {
            ring: _repository(self.root / "origins", ring)
            for ring in AUTONOMOUS.RINGS
        }
        self.urls = {
            f"https://github.com/{AUTONOMOUS.REPOSITORIES[ring]}.git":
                repo.as_uri()
            for ring, repo in self.repos.items()
        }
        run = AUTONOMOUS._run

        def local_run(args, **kwargs):
            return run([self.urls.get(str(arg), arg) for arg in args], **kwargs)

        self.enterContext(mock.patch.object(AUTONOMOUS, "_run", side_effect=local_run))
        self.enterContext(mock.patch.object(AUTONOMOUS.time, "sleep"))
        self.enterContext(mock.patch.dict(os.environ, {
            key: value for key, value in os.environ.items()
            if not key.startswith("RAPP_")
        }, clear=True))

    def _feature(self):
        repo = self.repos["canary"]
        _git(repo, "checkout", "-qb", "docs/onboarding")
        _git(repo, "commit", "--allow-empty", "-qm", "feature")
        os.environ["RAPP_CANARY_REF"] = "docs/onboarding"
        return _git(repo, "rev-parse", "HEAD")

    def _clone_all(self, inputs):
        root = self.root / "clones"
        root.mkdir()
        return {
            ring: AUTONOMOUS._clone_ring(root, ring, inputs)
            for ring in AUTONOMOUS.RINGS
        }

    def test_canary_only_feature_uses_downstream_mains(self):
        feature = self._feature()
        inputs = AUTONOMOUS._select_inputs()
        clones = self._clone_all(inputs)
        self.assertEqual(inputs["canary"]["ref"], "refs/heads/docs/onboarding")
        self.assertEqual(inputs["canary"]["commit"], feature)
        for ring in AUTONOMOUS.RINGS:
            if ring != "canary":
                self.assertEqual(inputs[ring]["ref"], "refs/heads/main")
                self.assertEqual(
                    _git(self.repos[ring], "branch", "--list", "docs/onboarding"), ""
                )
            self.assertEqual(_git(clones[ring], "rev-parse", "HEAD"), inputs[ring]["commit"])
            self.assertEqual(_git(clones[ring], "status", "--porcelain"), "")

    def test_event_pin_survives_branch_advancement(self):
        feature = self._feature()
        os.environ["RAPP_CANARY_COMMIT"] = feature
        _git(self.repos["canary"], "commit", "--allow-empty", "-qm", "newer push")
        inputs = AUTONOMOUS._select_inputs()
        clones = self._clone_all(inputs)
        self.assertEqual(_git(clones["canary"], "rev-parse", "HEAD"), feature)
        self.assertNotEqual(_git(self.repos["canary"], "rev-parse", "HEAD"), feature)

    def test_selected_downstream_pin_survives_branch_advancement(self):
        inputs = AUTONOMOUS._select_inputs()
        nightly = inputs["nightly"]["commit"]
        _git(self.repos["nightly"], "commit", "--allow-empty", "-qm", "later main")
        clones = self._clone_all(inputs)
        self.assertEqual(_git(clones["nightly"], "rev-parse", "HEAD"), nightly)
        for ring in AUTONOMOUS.RINGS:
            os.environ[f"RAPP_{ring.upper()}_REF"] = inputs[ring]["ref"]
            os.environ[f"RAPP_{ring.upper()}_COMMIT"] = inputs[ring]["commit"]
        self.assertEqual(AUTONOMOUS._select_inputs(), inputs)

    def test_missing_explicit_ref_never_falls_back_even_with_pin(self):
        os.environ["RAPP_CANARY_REF"] = "missing-branch"
        os.environ["RAPP_CANARY_COMMIT"] = _git(self.repos["canary"], "rev-parse", "HEAD")
        with self.assertRaises(AUTONOMOUS.ScenarioError):
            AUTONOMOUS._select_inputs()

    def test_missing_pinned_commit_never_falls_back(self):
        os.environ["RAPP_CANARY_COMMIT"] = "f" * 40
        inputs = AUTONOMOUS._select_inputs()
        root = self.root / "missing-pin"
        root.mkdir()
        with self.assertRaises(AUTONOMOUS.ScenarioError):
            AUTONOMOUS._clone_ring(root, "canary", inputs)

    def test_invalid_or_empty_explicit_pin_is_rejected(self):
        for value in ("", "1234567", "main"):
            with self.subTest(value=value):
                os.environ["RAPP_CANARY_COMMIT"] = value
                with self.assertRaisesRegex(AUTONOMOUS.ScenarioError, "full commit SHA"):
                    AUTONOMOUS._select_inputs()

    def test_legacy_global_ref_is_rejected(self):
        os.environ["RAPP_RING_REF"] = "docs/onboarding"
        with self.assertRaisesRegex(AUTONOMOUS.ScenarioError, "ambiguous"):
            AUTONOMOUS._select_inputs()

    def test_annotated_tag_resolves_to_commit(self):
        canary = self.repos["canary"]
        _git(canary, "tag", "-a", "candidate", "-m", "candidate")
        os.environ["RAPP_CANARY_REF"] = "refs/tags/candidate"
        inputs = AUTONOMOUS._select_inputs()
        self.assertEqual(inputs["canary"]["commit"], _git(canary, "rev-parse", "HEAD"))
        self.assertNotEqual(inputs["canary"]["commit"], _git(canary, "rev-parse", "candidate"))


class AutonomousRewriteMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = _repository(self.root, "canary", count=2)
        self.target = _repository(self.root, "nightly", count=1)

    def _config(self, repo):
        return json.loads((repo / ".ring" / "ring.json").read_text(encoding="utf-8"))

    def _save_config(self, repo, config):
        (repo / ".ring" / "ring.json").write_text(
            json.dumps(config), encoding="utf-8", newline="\n"
        )
        _git(repo, "add", ".ring/ring.json")
        _git(repo, "commit", "-qm", "adjust fixture config")

    def test_stages_only_declared_counts_and_preserves_target_identity(self):
        before = self._config(self.target)
        source_head = _git(self.source, "rev-parse", "HEAD")
        target_head = _git(self.target, "rev-parse", "HEAD")
        payload = _git(self.target, "rev-parse", "HEAD:README.md")
        commit = AUTONOMOUS._stage_rewrite_counts(self.source, self.target)
        expected = before
        expected["rewrites"][0]["expected_count"] = 2
        self.assertEqual(self._config(self.target), expected)
        self.assertEqual(commit, _git(self.target, "rev-parse", "HEAD"))
        self.assertNotEqual(commit, target_head)
        self.assertEqual(_git(self.source, "rev-parse", "HEAD"), source_head)
        self.assertEqual(_git(self.target, "rev-parse", "HEAD:README.md"), payload)
        self.assertEqual(
            _git(self.target, "diff", "--name-only", target_head, commit),
            ".ring/ring.json",
        )
        self.assertEqual(_git(self.target, "status", "--porcelain"), "")
        self.assertIsNone(AUTONOMOUS._stage_rewrite_counts(self.source, self.target))
        self.assertEqual(commit, _git(self.target, "rev-parse", "HEAD"))

    def test_mismatched_or_duplicate_rules_and_scopes_are_rejected(self):
        original = self._config(self.target)
        for mismatch in ("unknown", "duplicate", "protected", "excluded"):
            with self.subTest(mismatch=mismatch):
                config = json.loads(json.dumps(original))
                if mismatch == "unknown":
                    config["rewrites"][0]["from"] = "unapproved/source"
                elif mismatch == "duplicate":
                    config["rewrites"].append(dict(config["rewrites"][0]))
                elif mismatch == "protected":
                    config["protected_paths"].append("README.md")
                else:
                    config["rewrite_excluded_prefixes"].append("README.md")
                self._save_config(self.target, config)
                head = _git(self.target, "rev-parse", "HEAD")
                with self.assertRaisesRegex(AUTONOMOUS.ScenarioError, "do not match"):
                    AUTONOMOUS._stage_rewrite_counts(self.source, self.target)
                self.assertEqual(_git(self.target, "rev-parse", "HEAD"), head)
                self.assertEqual(_git(self.target, "status", "--porcelain"), "")

    def test_dirty_target_is_not_committed(self):
        path = self.target / "README.md"
        path.write_text("uncommitted\n", encoding="utf-8")
        head = _git(self.target, "rev-parse", "HEAD")
        with self.assertRaisesRegex(AUTONOMOUS.ScenarioError, "clean repositories"):
            AUTONOMOUS._stage_rewrite_counts(self.source, self.target)
        self.assertEqual(_git(self.target, "rev-parse", "HEAD"), head)
        self.assertEqual(path.read_text(encoding="utf-8"), "uncommitted\n")

    def test_staged_counts_still_reject_drift_and_render_only_matching_payload(self):
        AUTONOMOUS._stage_rewrite_counts(self.source, self.target)
        config = self.target / ".ring" / "ring.json"
        with self.assertRaisesRegex(AUTONOMOUS.render_ring.RenderError, "rewrite count drift"):
            AUTONOMOUS.render_ring.render(self.target, config, self.root / "old-payload")
        (self.target / "README.md").write_bytes((self.source / "README.md").read_bytes())
        AUTONOMOUS._commit(self.target, "test: incoming payload")
        build = self.root / "matching-payload"
        AUTONOMOUS.render_ring.render(self.target, config, build)
        self.assertEqual(
            (build / "README.md").read_text(encoding="utf-8"),
            "kody-w/rapp-nightly\n" * 2,
        )
        (self.target / "README.md").write_text(
            "kody-w/rapp-installer\n" * 3, encoding="utf-8", newline="\n"
        )
        AUTONOMOUS._commit(self.target, "test: unexpected payload drift")
        with self.assertRaisesRegex(AUTONOMOUS.render_ring.RenderError, "rewrite count drift"):
            AUTONOMOUS.render_ring.render(self.target, config, self.root / "drifted-payload")


class AutonomousMutationTests(unittest.TestCase):
    def test_brainstem_mutation_is_a_failure_case_not_a_feature(self):
        self.assertNotIn("backend-route", AUTONOMOUS.SCENARIOS)
        source = MODULE.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("immutable-grail-kernel-drift", source)

    def test_storage_probe_targets_the_method_name_not_an_old_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            path = repo / "rapp_brainstem" / "local_storage.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                "class Storage:\n"
                "    def file_exists(self, directory_name, file_name=None):\n"
                "        return False\n",
                encoding="utf-8",
            )
            AUTONOMOUS._storage_feature(repo)
            source = path.read_text(encoding="utf-8")
            self.assertIn("def pipeline_probe(self):", source)
            self.assertLess(
                source.index("def pipeline_probe"),
                source.index("def file_exists"),
            )

    def test_workflow_pins_event_and_reuses_tested_downstream_inputs(self):
        workflow = (ROOT / ".github" / "workflows" / "autonomous-pre-grail.yml")
        source = workflow.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("RAPP_RING_REF:", source)
        self.assertIn("RAPP_CANARY_REF: ${{ github.ref }}", source)
        self.assertIn("RAPP_CANARY_COMMIT: ${{ github.sha }}", source)
        self.assertIn("ring_inputs: ${{ steps.matrix.outputs.ring_inputs }}", source)
        for ring in ("nightly", "alpha", "beta"):
            for suffix, field in (("REF", "ref"), ("COMMIT", "commit")):
                self.assertIn(
                    f"RAPP_{ring.upper()}_{suffix}: "
                    "${{ fromJSON(needs.test.outputs.ring_inputs)."
                    f"{ring}.{field} }}}}",
                    source,
                )


if __name__ == "__main__":
    unittest.main()
