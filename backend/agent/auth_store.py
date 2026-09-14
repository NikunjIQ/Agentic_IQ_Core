"""Accounts: sign-up and password checks, on the SQLite store (agent.db).

main.py issues a session cookie on top of this module; a successful login() is
what earns one, and every data endpoint checks it. What this still is not:
there is no password reset, lockout, rate limiting or email verification, and
sign-up is open to anyone who can load the page. Replace before exposing this to
anything untrusted.

Passwords are salted PBKDF2 hashes, never text. The format is unchanged from the
JSON-file era, so accounts imported from authentication.json keep working with
the passwords their owners already use.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3

from agent import db

# PBKDF2 is in the standard library and needs no dependency. The cost is chosen
# to be unnoticeable on a login (~50ms) while still being real work to grind.
_ITERATIONS = 200_000

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS).hex()


def user_count() -> int:
    return db.count_users()


def signup(username: str, password: str, email: str) -> dict:
    """Create an account. The first one ever created is the admin."""
    username = (username or "").strip()
    email = (email or "").strip()
    if len(username) < 3:
        return {"ok": False, "error": "Username must be at least 3 characters."}
    if len(password or "") < 6:
        return {"ok": False, "error": "Password must be at least 6 characters."}
    if not _EMAIL.match(email):
        return {"ok": False, "error": "That email address doesn't look right."}
    if db.find_user(username):
        return {"ok": False, "error": "That username is already taken."}
    if db.email_taken(email):
        return {"ok": False, "error": "That email address is already registered."}

    salt = secrets.token_hex(16)
    try:
        user_id = db.create_user(username, email, salt, _hash(password, salt))
    except sqlite3.IntegrityError:
        # Two sign-ups racing for the same name: the UNIQUE constraint is the
        # real guard, the checks above are only there for a readable message.
        return {"ok": False, "error": "That username or email is already taken."}
    return {"ok": True, "user_id": user_id, "username": username}


def login(username: str, password: str) -> dict:
    """Check a username and password."""
    user = db.find_user((username or "").strip())
    # Hash even when the user does not exist, so how long the answer takes does
    # not reveal which usernames are real.
    candidate = _hash(password or "", user["password_salt"] if user else "00" * 16)
    # compare_digest, not ==, so the comparison does not leak how much matched.
    if user and hmac.compare_digest(user["password_hash"], candidate):
        return {"ok": True, "user_id": user["id"], "username": user["username"],
                "role": user["role"]}
    # One message for both cases: saying which half was wrong tells an attacker
    # which usernames exist.
    return {"ok": False, "error": "Username or password is incorrect."}
