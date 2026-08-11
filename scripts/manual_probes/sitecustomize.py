"""Keep moved manual probes importable when run as scripts.

Python imports this file automatically when a script in this directory is run.
It adds the project root to sys.path so imports like `from adapters...` keep
working after the probes moved out of the repository root.
"""
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
