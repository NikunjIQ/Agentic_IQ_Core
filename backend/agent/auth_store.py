"""Accounts: sign-up and password checks, on the SQLite store (agent.db).

main.py issues a session cookie on top of this module; a successful login() is
what earns one, and every data endpoint checks it. What this still is not:
there is no password reset, lockout, rate limiting or email verification, and
sign-up is open to anyone who can load the page. Replace before exposing this to
anything untrusted.

Passwords are salted PBKDF2 hashes, never text. The format is unchanged from the
JSON-file era, so accounts imported from authentication.json keep working with
the passwords their owners already use.

When LDAP_URL is set, a username with no local password is checked against the
company directory instead (bind as a read-only service account, find the user,
bind as the user). The first directory sign-in creates a local row so credits
and sessions work as before; it never holds a password. Local accounts are
always checked first, so a local admin stays usable when the directory is down.
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import ssl

import ldap3
from ldap3.core.exceptions import LDAPBindError, LDAPException
from ldap3.utils.conv import escape_filter_chars

from agent import db

log = logging.getLogger(__name__)

# password_hash of an account that signs in through LDAP only. Never equal to a
# 64-hex PBKDF2 digest, so no local password can ever match it.
DIRECTORY_ACCOUNT = "ldap"

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
    # With the directory on, accounts come from it. Open sign-up would let anyone
    # claim a directory user's name locally and lock that person out. A fresh
    # install still gets its first (admin) account, the break-glass login.
    if ldap_enabled() and db.count_users() > 0:
        return {"ok": False, "error": "Sign in with your company directory username and "
                                      "password; new accounts are not created here."}
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
    # A local account with its own password never falls through to LDAP: a
    # directory user of the same name must not be able to take it over.
    if ldap_enabled() and (not user or user["password_hash"] == DIRECTORY_ACCOUNT):
        return _directory_login((username or "").strip(), password or "")
    # One message for both cases: saying which half was wrong tells an attacker
    # which usernames exist.
    return {"ok": False, "error": "Username or password is incorrect."}


def _directory_login(username: str, password: str) -> dict:
    try:
        found = ldap_check(username, password)
    except LDAPException as exc:
        log.warning("LDAP sign-in for %r failed: %s", username, exc)
        return {"ok": False, "error": "The company directory could not be reached. "
                                      "Try again shortly, or use a local account."}
    if not found.get("accepted"):
        return {"ok": False, "error": "Username or password is incorrect."}

    user = db.find_user(username)
    if not user:
        email = found["email"] if found["email"] and not db.email_taken(found["email"]) else None
        try:
            db.create_user(username, email, secrets.token_hex(16), DIRECTORY_ACCOUNT)
        except sqlite3.IntegrityError:
            pass  # a parallel first sign-in created it; use that row
        user = db.find_user(username)
    # The directory owns the role, so removing someone from the group demotes
    # them at their next sign-in. Also undoes create_user's first-user-is-admin.
    role = "admin" if found["admin"] else "user"
    if user["role"] != role:
        db.set_role(user["id"], role)
    return {"ok": True, "user_id": user["id"], "username": user["username"], "role": role}


def _on(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def ldap_enabled(cfg=os.environ) -> bool:
    """A server URL is set and the switch is not off. Unset switch means on, so
    a .env with only LDAP_URL keeps working."""
    return bool(str(cfg.get("LDAP_URL") or "").strip()) and _on(cfg.get("LDAP_ENABLED") or "true")


def _bind(server, dn, password, start_tls):
    """A bound connection, or None when the directory refuses the credentials.

    bind()'s own answer, not auto_bind: ldap3's mock reports a refused auto_bind
    as a success, and the password check must not depend on such details."""
    conn = ldap3.Connection(server, dn, password, receive_timeout=10)
    if start_tls:
        conn.open()
        if not conn.start_tls():
            conn.unbind()
            raise LDAPException("StartTLS was refused")
    if conn.bind():
        return conn
    conn.unbind()
    return None


def ldap_check(username: str, password: str, cfg=os.environ) -> dict:
    """Ask the directory about username/password, with the settings in cfg.

    {"dn": None}                   no single entry has that username
    {"dn": ..., "accepted": False} found, password wrong or not given
    {"dn": ..., "accepted": True, "email": ..., "admin": bool}

    Raises LDAPException when the directory is unreachable or refuses the
    service account, so that is never mistaken for a wrong password.
    """
    start_tls = _on(cfg.get("LDAP_START_TLS"))
    # Certificates are verified: ldap3's own default is to accept any.
    tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=cfg.get("LDAP_CA_FILE") or None)
    server = ldap3.Server(str(cfg.get("LDAP_URL") or "").strip(), tls=tls, connect_timeout=5)

    svc = _bind(server, cfg.get("LDAP_BIND_DN") or None, cfg.get("LDAP_BIND_PASSWORD") or None,
                start_tls)
    if not svc:
        raise LDAPBindError("the directory refused the bind DN or bind password")
    try:
        if not username:
            return {"dn": None}
        attr = cfg.get("LDAP_USER_ATTR") or "uid"
        svc.search(cfg.get("LDAP_USER_BASE") or "ou=People,dc=example,dc=local",
                   f"({attr}={escape_filter_chars(username)})", attributes=["mail"])
        hits = [r for r in svc.response if r["type"] == "searchResEntry"]
        if len(hits) != 1:  # none, or ambiguous: refuse rather than guess who it is
            return {"dn": None}
        dn = hits[0]["dn"]

        # An empty password is an LDAP "unauthenticated bind", which servers
        # accept for any DN: it must never count as a correct password.
        user = _bind(server, dn, password, start_tls) if password else None
        if not user:
            return {"dn": dn, "accepted": False}
        user.unbind()

        # Covers groupOfNames, groupOfUniqueNames and posixGroup without having
        # to know which one the directory uses.
        group = str(cfg.get("LDAP_ADMIN_GROUP") or "").strip()
        admin = bool(group) and svc.search(
            group, f"(|(member={escape_filter_chars(dn)})(uniqueMember={escape_filter_chars(dn)})"
                   f"(memberUid={escape_filter_chars(username)}))",
            search_scope=ldap3.BASE, attributes=[])
        mail = hits[0]["attributes"].get("mail") or None
        if isinstance(mail, list):
            mail = mail[0] if mail else None
        return {"dn": dn, "accepted": True, "email": mail, "admin": bool(admin)}
    finally:
        svc.unbind()
