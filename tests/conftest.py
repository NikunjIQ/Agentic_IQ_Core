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

# Hold the ticket estate for the whole run. A dozen checks touch Zammad, and at
# the app's 60s default the cache expires mid-suite and re-downloads all 3,788
# tickets several times — that alone was most of a 210s run.
#
# This is a test-only setting. It does NOT make the app fetch less: that is a
# real inefficiency (43s to answer "how many tickets are open") and it should be
# fixed in the tool, not hidden behind a longer cache here. Set before any
# backend import, since the TTL is read at module load.
os.environ.setdefault("ZAMMAD_CACHE_TTL", "900")

try:
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
except Exception:
    pass

# Credentials entered through the UI live in credentials.json, not .env, and
# main.py is what normally applies them. The tests never import main, so without
# this the whole suite runs against an unconfigured environment the moment
# someone configures the app through the sidebar.
try:
    from agent import config_store
    config_store.apply_to_environment()
except Exception:
    pass

# Tracing adds latency and noise to every test run.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
