"""A makeshift login gate, backed by authentication.json.

Deliberately NOT real authentication, and it should be replaced before this is
exposed to anything untrusted. What it is: a username/password list that the UI
checks before showing the app.

What it is NOT, and what has to change when the real thing goes in:

  - There is no session. A successful login sets a flag in the browser; the API
    itself is still open, so anything that can reach the port can call /chat
    without logging in at all.
  - There is no rate limiting, lockout, password policy, or reset.
  - Signup is open. Anyone who can load the page can create an account.

Passwords are still hashed rather than stored as text. That is not because this
is secure — it is not — but because the file would otherwise be a list of
plaintext passwords that people reuse elsewhere, and hashing costs three lines
of standard library.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

AUTH_PATH = Path(os.getenv("IQCORE_AUTH_PATH",
                           Path(__file__).resolve().parent.parent / "authentication.json"))

_lock = threading.Lock()

# PBKDF2 is in the standard library and needs no dependency. The cost is chosen
# to be unnoticeable on a login (~50ms) while still being real work to grind.
_ITERATIONS = 200_000


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS).hex()


def load() -> dict:
    """Whatever is on disk. An empty user list if the file has never been written."""
    try:
        with _lock:
            if AUTH_PATH.exists():
                data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("users"), list):
                    return data
    except Exception:
        pass
    return {"users": []}


def _write(data: dict) -> None:
    with _lock:
        AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTH_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(AUTH_PATH, 0o600)   # best effort; a no-op on some Windows setups
        except Exception:
            pass


def _find(users: list, username: str):
    # Usernames match case-insensitively. "Nikunj" failing to log in an account
    # created as "nikunj" is a support call, not a security feature.
    key = username.strip().lower()
    return next((u for u in users if u.get("username", "").lower() == key), None)


def user_count() -> int:
    return len(load()["users"])


def signup(username: str, password: str) -> dict:
    """Create an account. Returns {"ok": bool, "error": str}."""
    username = (username or "").strip()
    if len(username) < 3:
        return {"ok": False, "error": "Username must be at least 3 characters."}
    if len(password or "") < 6:
        return {"ok": False, "error": "Password must be at least 6 characters."}

    data = load()
    if _find(data["users"], username):
        return {"ok": False, "error": "That username is already taken."}

    salt = secrets.token_hex(16)
    data["users"].append({
        "username": username,
        "salt": salt,
        "hash": _hash(password, salt),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _write(data)
    return {"ok": True, "username": username}


def login(username: str, password: str) -> dict:
    """Check a username and password. Returns {"ok": bool, "error": str}."""
    user = _find(load()["users"], (username or "").strip())
    if user:
        # compare_digest, not ==, so the comparison does not leak how much of the
        # hash matched through how long it took.
        if hmac.compare_digest(user["hash"], _hash(password or "", user["salt"])):
            return {"ok": True, "username": user["username"]}
    # One message for both cases: saying which half was wrong tells an attacker
    # which usernames exist.
    return {"ok": False, "error": "Username or password is incorrect."}
