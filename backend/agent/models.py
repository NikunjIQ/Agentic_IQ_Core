"""Which Groq models this app uses, in one place.

Providers retire models. `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`
were both decommissioned while this app was running against them, and the ids
were hardcoded across four modules — so a provider-side change became a
code change in four files plus a stale connection test that still checked for
models nobody could call.

Both ids can be overridden by environment variable, so the next retirement can
be handled by configuration rather than a deployment.

Roles, not sizes: REASONING writes prose and drives the orchestrator loop where
answer quality shows; FAST classifies and fills in a schema that is already
forced by tool_choice, where it does not.
"""

import os

# Large: answer synthesis and the multi-step orchestrator.
REASONING_MODEL = os.getenv("IQCORE_REASONING_MODEL", "openai/gpt-oss-120b")

# Small: complexity classification and argument extraction.
FAST_MODEL = os.getenv("IQCORE_FAST_MODEL", "openai/gpt-oss-20b")

# Every model the app will try to call. The connection test checks these are
# reachable on the configured key, so a retirement is reported as a
# configuration problem rather than surfacing mid-answer as a 404.
REQUIRED_MODELS = (REASONING_MODEL, FAST_MODEL)
