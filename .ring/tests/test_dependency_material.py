import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / ".ring" / "tools" / "build_dependency_material.py"
SPEC = importlib.util.spec_from_file_location("build_dependency_material", MODULE)
MATERIAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATERIAL)


class DependencyMaterialTests(unittest.TestCase):
    def test_runtime_and_test_pins_merge_deterministically(self):
        self.assertEqual(
            MATERIAL._combined_pins(
                ["Requests==2.34.2", "Flask==3.1.3"],
                ["pytest==9.1.1", "requests==2.34.2"],
            ),
            ["flask==3.1.3", "pytest==9.1.1", "requests==2.34.2"],
        )

    def test_conflicting_runtime_and_test_pins_fail_closed(self):
        with self.assertRaisesRegex(MATERIAL.MaterialError, "conflicting"):
            MATERIAL._combined_pins(
                ["packaging==25.0"],
                ["Packaging==26.3"],
            )


if __name__ == "__main__":
    unittest.main()
