"""Test bootstrap: put `backend` on the path and load real credentials.

Imported by run_evals.py directly, and picked up automatically by pytest if it
is ever installed.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

try:
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
except Exception:
    pass

# Tracing adds latency and noise to every test run.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
