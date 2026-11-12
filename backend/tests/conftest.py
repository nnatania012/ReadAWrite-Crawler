import sys
from pathlib import Path

# Make "backend/" importable as the top-level package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
