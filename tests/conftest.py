"""Pytest configuration: put the controller package (under crates/) on sys.path."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRATES = REPO_ROOT / "crates"
if str(CRATES) not in sys.path:
    sys.path.insert(0, str(CRATES))
