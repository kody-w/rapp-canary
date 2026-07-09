# The brainstem is a single-file server in the parent directory — make it
# importable for tests living here (the root stays clean; see CLAUDE.md).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
