"""Unit tests for grail_gate's pure validation logic (no network, no git)."""

import copy
import sys
import unittest
from pathlib import Path

RING_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RING_DIR / "tools"))

import grail_gate  # noqa: E402
import ring_attestation as attestation  # noqa: E402


def _fixture_chain():
    """A minimal, internally consistent canary->beta attestation chain."""
    rings = attestation._ring_map(attestation._read_json(RING_DIR / "train.json"))
    digest = "d" * 64
    chain = {}
    parent_ref = None
    for name in grail_gate.RING_ORDER:
        value = {
            "schema": "rapp-ring-attestation/1",
            "ring": name,
            "payload": {
                "repository": rings[name]["repository"],
                "commit": "a" * 40,
                "tree": "b" * 40,
                "shared_sha256": digest,
            },
            "parent": parent_ref,
            "result": "passed",
        }
        chain[name] = value
        parent_ref = {
            "ring": name,
            "sha256": attestation._attestation_sha256(value),
        }
    return chain, rings


class TestValidateRunRecord(unittest.TestCase):
    def _run(self, **overrides):
        run = {
            "name": grail_gate.WORKFLOW_NAME,
            "conclusion": "success",
            "repository": {"full_name": grail_gate.HUB_REPOSITORY},
        }
        run.update(overrides)
        return run

    def test_green_hub_run_passes(self):
        grail_gate._validate_run_record(self._run())

    def test_wrong_workflow_rejected(self):
        with self.assertRaises(grail_gate.GateError):
            grail_gate._validate_run_record(self._run(name="preflight"))

    def test_failed_run_rejected(self):
        with self.assertRaises(grail_gate.GateError):
            grail_gate._validate_run_record(self._run(conclusion="failure"))

    def test_foreign_repository_rejected(self):
        with self.assertRaises(grail_gate.GateError):
            grail_gate._validate_run_record(
                self._run(repository={"full_name": "someone/else"})
            )


class TestValidateChain(unittest.TestCase):
    def test_consistent_chain_passes(self):
        chain, rings = _fixture_chain()
        digest = grail_gate._validate_chain(chain, rings)
        self.assertEqual(digest, "d" * 64)

    def test_missing_ring_rejected(self):
        chain, rings = _fixture_chain()
        del chain["alpha"]
        with self.assertRaises(grail_gate.GateError):
            grail_gate._validate_chain(chain, rings)

    def test_divergent_payload_rejected(self):
        chain, rings = _fixture_chain()
        chain["beta"] = copy.deepcopy(chain["beta"])
        chain["beta"]["payload"]["shared_sha256"] = "e" * 64
        with self.assertRaises((grail_gate.GateError, attestation.AttestationError)):
            grail_gate._validate_chain(chain, rings)

    def test_tampered_parent_link_rejected(self):
        chain, rings = _fixture_chain()
        chain["nightly"] = copy.deepcopy(chain["nightly"])
        chain["nightly"]["parent"]["sha256"] = "f" * 64
        with self.assertRaises(grail_gate.GateError):
            grail_gate._validate_chain(chain, rings)

    def test_swapped_ring_identity_rejected(self):
        chain, rings = _fixture_chain()
        chain["alpha"], chain["nightly"] = chain["nightly"], chain["alpha"]
        with self.assertRaises((grail_gate.GateError, attestation.AttestationError)):
            grail_gate._validate_chain(chain, rings)


if __name__ == "__main__":
    unittest.main()
