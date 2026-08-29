import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / ".ring" / "tools" / "autonomous_test.py"
sys.path.insert(0, str(MODULE.parent))
SPEC = importlib.util.spec_from_file_location("autonomous_test", MODULE)
AUTONOMOUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTONOMOUS)


class AutonomousMutationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
