"""SQLite storage: accounts, credit balances, usage per answer, per-user service
credentials, and login sessions.

One file, standard library only. Chosen over a database server because this runs
on one VM for a handful of users; a second app server would need a real one.

Three things that bite if missed, and why every connection comes from connect():

  - PRAGMA foreign_keys is OFF by default, and per connection. Without it
    ON DELETE CASCADE and every REFERENCES clause are decoration.
  - WAL journal mode, so a reader never waits on the single writer.
  - A fresh connection per operation. sqlite3 connections must not be shared
    across the threads this app answers requests on.

Money is integer paise throughout. Floating point cannot represent 0.10, and a
balance that drifts by a paisa per answer is a support call.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# What a token is sold for, in paise. Lives here, not in the browser: the page
# can no longer be the source of what someone spent.
PAISE_PER_TOKEN = int(os.getenv("IQCORE_PAISE_PER_TOKEN", "2"))
DEFAULT_ALLOCATION_PAISE = 1_000_000          # Rs 10,000
SESSION_HOURS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email         TEXT UNIQUE COLLATE NOCASE,
  password_salt TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS credit_accounts (
  user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  allocated_paise INTEGER NOT NULL DEFAULT 1000000 CHECK (allocated_paise >= 0),
  updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS usage (
  id              INTEGER PRIMARY KEY,
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id      TEXT NOT NULL,
  question        TEXT,
  tokens          INTEGER NOT NULL CHECK (tokens >= 0),
  paise           INTEGER NOT NULL CHECK (paise >= 0),
  paise_per_token INTEGER NOT NULL,
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS usage_by_user ON usage(user_id, created_at);

CREATE TABLE IF NOT EXISTS service_credentials (
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  service    TEXT NOT NULL,
  settings   TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, service)
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS credit_balance AS
SELECT u.id                                            AS user_id,
       u.username,
       u.email,
       u.role,
       a.allocated_paise,
       COALESCE(SUM(g.paise), 0)                       AS spent_paise,
       a.allocated_paise - COALESCE(SUM(g.paise), 0)   AS remaining_paise,
       COUNT(g.id)                                     AS answers
FROM users u
JOIN credit_accounts a ON a.user_id = u.id
LEFT JOIN usage g      ON g.user_id = u.id
GROUP BY u.id;

CREATE TABLE IF NOT EXISTS turns (
  id              INTEGER PRIMARY KEY,
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL,
  question        TEXT NOT NULL,
  answer          TEXT NOT NULL,
  payload         TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS turns_by_conversation ON turns(user_id, conversation_id, id);

CREATE TABLE IF NOT EXISTS favourites (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  question   TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (user_id, question)
);

CREATE TABLE IF NOT EXISTS feedback (
  turn_id    INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rating     INTEGER NOT NULL CHECK (rating IN (-1, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Each Zammad that sends tickets to ticket assist. Token and secret are encrypted.
CREATE TABLE IF NOT EXISTS zammad_connections (
  name               TEXT PRIMARY KEY,
  url                TEXT NOT NULL,
  write_token_enc    TEXT NOT NULL,
  webhook_secret_enc TEXT NOT NULL,
  assist_mode        TEXT NOT NULL DEFAULT 'dry_run'
                     CHECK (assist_mode IN ('off', 'dry_run', 'write')),
  daily_limit        INTEGER NOT NULL DEFAULT 40 CHECK (daily_limit >= 0),
  ansible_enabled    INTEGER NOT NULL DEFAULT 0 CHECK (ansible_enabled IN (0, 1)),
  updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _db_path() -> Path:
    # Read per call, not at import, so a test can point it at a temp file by
    # setting the variable — no reload dance.
    return Path(os.getenv("IQCORE_DB_PATH", BACKEND / "iqcore.db"))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def tx():
    """One connection, one transaction: committed on success, rolled back on error."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


_ready = set()


def init() -> None:
    """Create the schema if it is missing, and import the JSON files once.

    Idempotent: safe to call at every start, and from every test.
    """
    path = str(_db_path())
    if path in _ready:
        return
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")   # persists in the file
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    try:
        os.chmod(_db_path(), 0o600)                 # best effort; a no-op on some Windows setups
    except Exception:
        pass
    _import_legacy()
    _ready.add(path)


# ------------------------------------------------------------------- secrets
# Service passwords and tokens cannot be hashed — the app has to send them to
# Zabbix and the rest — so they are encrypted instead. The key comes from
# IQCORE_SECRET_KEY, or is generated once into secret.key beside the database
# (covered by the *.key rule in .gitignore). Keeping the key out of the database
# is the point: a copied iqcore.db on its own reveals no service credential.

def _key_path() -> Path:
    return Path(os.getenv("IQCORE_SECRET_KEY_PATH", BACKEND / "secret.key"))


@lru_cache(maxsize=4)
def _fernet_for(key: bytes):
    from cryptography.fernet import Fernet
    return Fernet(key)


def _fernet():
    key = os.getenv("IQCORE_SECRET_KEY", "").strip()
    if not key:
        path = _key_path()
        if not path.exists():
            from cryptography.fernet import Fernet
            path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        key = path.read_text(encoding="utf-8").strip()
    return _fernet_for(key.encode())


def _secret_keys() -> set:
    from agent.config_store import SECRET_KEYS
    return SECRET_KEYS


def _seal(values: dict) -> str:
    secret = _secret_keys()
    return json.dumps({k: ("enc:" + _fernet().encrypt(v.encode()).decode()) if k in secret else v
                       for k, v in values.items()})


def _open(settings: str) -> dict:
    out = {}
    for k, v in json.loads(settings).items():
        out[k] = _fernet().decrypt(v[4:].encode()).decode() if str(v).startswith("enc:") else v
    return out


# ---------------------------------------------------------------------- users

def count_users() -> int:
    with tx() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, email, salt: str, password_hash: str,
                created_at: str = None) -> int:
    """Insert a user and their credit account together.

    The first account ever created is the admin. Decided inside the INSERT, so
    two people signing up at once cannot both become admin.
    """
    with tx() as c:
        cur = c.execute(
            """INSERT INTO users (username, email, password_salt, password_hash, role, created_at)
               VALUES (?, ?, ?, ?,
                       CASE WHEN (SELECT COUNT(*) FROM users) = 0 THEN 'admin' ELSE 'user' END,
                       COALESCE(?, CURRENT_TIMESTAMP))""",
            (username, email or None, salt, password_hash, created_at))
        user_id = cur.lastrowid
        c.execute("INSERT INTO credit_accounts (user_id, allocated_paise) VALUES (?, ?)",
                  (user_id, DEFAULT_ALLOCATION_PAISE))
        return user_id


def find_user(username: str):
    with tx() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?",
                        ((username or "").strip(),)).fetchone()
        return dict(row) if row else None


def set_role(user_id: int, role: str) -> None:
    with tx() as c:
        c.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def email_taken(email: str) -> bool:
    with tx() as c:
        return c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


# ------------------------------------------------------------------- sessions
# The cookie carries a random token; only its SHA-256 is stored, so a leaked
# database hands nobody a live session.

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
    with tx() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?",
                  (datetime.now(timezone.utc).isoformat(),))
        c.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                  (_digest(token), user_id, expires.isoformat()))
    return token


def user_for_session(token: str):
    """The signed-in user for a cookie, or None when it is missing, unknown or expired."""
    if not token:
        return None
    with tx() as c:
        row = c.execute(
            """SELECT u.id, u.username, u.email, u.role, s.token_hash
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (_digest(token), datetime.now(timezone.utc).isoformat())).fetchone()
        return dict(row) if row else None


def end_session(token: str) -> None:
    if token:
        with tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


# -------------------------------------------------------- service credentials

def load_settings(user_id: int) -> dict:
    """{service: {field: plain value}} for every service this user has connected."""
    with tx() as c:
        rows = c.execute("SELECT service, settings FROM service_credentials WHERE user_id = ?",
                         (user_id,)).fetchall()
    return {r["service"]: _open(r["settings"]) for r in rows}


def save_settings(user_id: int, service: str, values: dict) -> None:
    """Replace one service's fields for one user. Secrets are sealed on the way in."""
    with tx() as c:
        c.execute(
            """INSERT INTO service_credentials (user_id, service, settings, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT (user_id, service)
               DO UPDATE SET settings = excluded.settings, updated_at = CURRENT_TIMESTAMP""",
            (user_id, service, _seal(values)))


# ------------------------------------------------------- zammad connections

def _zammad_row(row, with_secrets: bool) -> dict:
    out = {"name": row["name"], "url": row["url"], "assist_mode": row["assist_mode"],
           "daily_limit": row["daily_limit"], "ansible_enabled": bool(row["ansible_enabled"]),
           "updated_at": row["updated_at"]}
    if with_secrets:
        out["write_token"] = _fernet().decrypt(row["write_token_enc"].encode()).decode()
        out["webhook_secret"] = _fernet().decrypt(row["webhook_secret_enc"].encode()).decode()
    return out


def list_zammads() -> list:
    """Every connection, without its token or secret: safe to send to a browser."""
    with tx() as c:
        rows = c.execute("SELECT * FROM zammad_connections ORDER BY name").fetchall()
    return [_zammad_row(r, with_secrets=False) for r in rows]


def get_zammad(name: str):
    """One connection with its token and secret decrypted, or None. Server-side only."""
    with tx() as c:
        row = c.execute("SELECT * FROM zammad_connections WHERE name = ?", (name,)).fetchone()
    return _zammad_row(row, with_secrets=True) if row else None


def save_zammad(name: str, url: str, write_token: str, webhook_secret: str,
                assist_mode: str, daily_limit: int, ansible_enabled: bool) -> None:
    """Insert or replace one connection; the token and secret are encrypted here."""
    seal = lambda value: _fernet().encrypt(value.encode()).decode()
    with tx() as c:
        c.execute(
            """INSERT INTO zammad_connections (name, url, write_token_enc, webhook_secret_enc,
                                               assist_mode, daily_limit, ansible_enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (name) DO UPDATE SET
                 url = excluded.url, write_token_enc = excluded.write_token_enc,
                 webhook_secret_enc = excluded.webhook_secret_enc,
                 assist_mode = excluded.assist_mode, daily_limit = excluded.daily_limit,
                 ansible_enabled = excluded.ansible_enabled, updated_at = CURRENT_TIMESTAMP""",
            (name, url, seal(write_token), seal(webhook_secret), assist_mode,
             int(daily_limit), int(bool(ansible_enabled))))


def delete_zammad(name: str) -> bool:
    with tx() as c:
        return c.execute("DELETE FROM zammad_connections WHERE name = ?", (name,)).rowcount > 0


# --------------------------------------------------------------------- credit

def record_usage(user_id: int, session_id: str, question: str, tokens: int) -> None:
    """One row per answer. The rate is stored with it, so repricing later never
    rewrites what somebody was already charged."""
    tokens = max(int(tokens or 0), 0)
    with tx() as c:
        c.execute(
            """INSERT INTO usage (user_id, session_id, question, tokens, paise, paise_per_token)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, session_id, (question or "")[:500], tokens,
             tokens * PAISE_PER_TOKEN, PAISE_PER_TOKEN))


def balance(user_id: int) -> dict:
    with tx() as c:
        row = c.execute("SELECT * FROM credit_balance WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else {}


def all_balances() -> list:
    with tx() as c:
        return [dict(r) for r in
                c.execute("SELECT * FROM credit_balance ORDER BY user_id").fetchall()]


def set_allocation(user_id: int, paise: int) -> bool:
    with tx() as c:
        cur = c.execute(
            "UPDATE credit_accounts SET allocated_paise = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ?", (int(paise), user_id))
        return cur.rowcount == 1


# ------------------------------------------------------------ one-time import
# The JSON files are read once, into an empty database, and then left exactly as
# they were — so rolling back is deleting iqcore.db.

def _import_legacy() -> None:
    if count_users():
        return

    auth_path = Path(os.getenv("IQCORE_AUTH_PATH", BACKEND / "authentication.json"))
    try:
        users = json.loads(auth_path.read_text(encoding="utf-8")).get("users", [])
    except Exception:
        users = []

    admin_id = None
    for u in users:
        if not (u.get("username") and u.get("salt") and u.get("hash")):
            continue
        uid = create_user(u["username"], None, u["salt"], u["hash"], u.get("created"))
        admin_id = admin_id or uid

    if admin_id is None:
        return

    # The single global credential set becomes the first user's — the person
    # who configured it. Groq is not copied: it stays a system setting.
    from agent import config_store
    stored = config_store.load()
    for service, _desc, fields in config_store.FIELD_GROUPS:
        if service in config_store.SYSTEM_SERVICES:
            continue
        values = {key: str(stored[key]).strip() for key, *_ in fields
                  if str(stored.get(key, "") or "").strip()}
        if values:
            save_settings(admin_id, service, values)


# ------------------------------------------------------------- conversations
# One row per answered question. A conversation is simply the turns that share
# an id, titled by its first question — a separate table of titles would hold
# nothing else.

STORED_ROWS = 500


def _trim(payload: dict) -> dict:
    """What a reopened conversation needs to redraw: the answer, its tables and
    charts, its cost. Not the raw tool output, which can hold a whole estate."""
    kept = {k: v for k, v in (payload or {}).items() if k != "tool_results_raw"}
    kept["dataframes"] = [frame[:STORED_ROWS] if isinstance(frame, list) else frame
                          for frame in kept.get("dataframes") or []]
    kept["trimmed"] = True
    return kept


def new_conversation_id() -> str:
    return secrets.token_hex(8)


def save_turn(user_id: int, conversation_id: str, question: str, answer: str,
              payload: dict) -> int:
    with tx() as c:
        return c.execute("INSERT INTO turns (user_id, conversation_id, question, answer, payload) "
                  "VALUES (?, ?, ?, ?, ?)",
                  (user_id, conversation_id, question, answer or "",
                   json.dumps(_trim(payload), default=str))).lastrowid


def list_conversations(user_id: int, limit: int = 50) -> list:
    """Newest first: id, title (the first question), turn count, last activity."""
    with tx() as c:
        rows = c.execute(
            """SELECT t.conversation_id AS id,
                      (SELECT f.question FROM turns f
                        WHERE f.user_id = t.user_id AND f.conversation_id = t.conversation_id
                        ORDER BY f.id LIMIT 1)                 AS title,
                      COUNT(*)                                 AS turns,
                      MAX(t.created_at)                        AS updated_at
               FROM turns t WHERE t.user_id = ?
               GROUP BY t.conversation_id ORDER BY MAX(t.id) DESC LIMIT ?""",
            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def load_conversation(user_id: int, conversation_id: str) -> list:
    """The turns, oldest first. Empty for an id that is not this user's — which
    is indistinguishable from one that does not exist, on purpose."""
    with tx() as c:
        rows = c.execute("SELECT t.id, t.question, t.answer, t.payload, t.created_at, "
                         "f.rating FROM turns t LEFT JOIN feedback f ON f.turn_id = t.id "
                         "WHERE t.user_id = ? AND t.conversation_id = ? ORDER BY t.id",
                         (user_id, conversation_id)).fetchall()
    return [{"id": r["id"], "question": r["question"], "answer": r["answer"],
             "payload": json.loads(r["payload"]), "created_at": r["created_at"],
             "rating": r["rating"]}
            for r in rows]


def delete_conversation(user_id: int, conversation_id: str) -> bool:
    with tx() as c:
        return c.execute("DELETE FROM turns WHERE user_id = ? AND conversation_id = ?",
                         (user_id, conversation_id)).rowcount > 0


# ---------------------------------------------------------------- favourites

def list_favourites(user_id: int) -> list:
    with tx() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, question, created_at FROM favourites WHERE user_id = ? "
            "ORDER BY id DESC", (user_id,)).fetchall()]


def add_favourite(user_id: int, question: str) -> dict:
    """Idempotent: pinning the same question twice keeps the one pin."""
    question = (question or "").strip()[:500]
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO favourites (user_id, question) VALUES (?, ?)",
                  (user_id, question))
        row = c.execute("SELECT id, question, created_at FROM favourites "
                        "WHERE user_id = ? AND question = ?", (user_id, question)).fetchone()
    return dict(row)


def delete_favourite(user_id: int, favourite_id: int) -> bool:
    with tx() as c:
        return c.execute("DELETE FROM favourites WHERE id = ? AND user_id = ?",
                         (favourite_id, user_id)).rowcount > 0


# ------------------------------------------------------------------ feedback
# One rating per answer, changeable. A thumbs-down is the cheapest signal there
# is that routing or an answer went wrong, and each one is a candidate case for
# tests/routing_cases.py.

def set_feedback(user_id: int, turn_id: int, rating: int) -> bool:
    """Rate one of this user's own answers. False for anyone else's turn."""
    with tx() as c:
        return c.execute(
            """INSERT INTO feedback (turn_id, user_id, rating)
               SELECT id, user_id, ? FROM turns WHERE id = ? AND user_id = ?
               ON CONFLICT (turn_id) DO UPDATE SET rating = excluded.rating,
                                                  created_at = CURRENT_TIMESTAMP""",
            (rating, turn_id, user_id)).rowcount > 0


def list_downvotes(limit: int = 200) -> list:
    """Every thumbs-down, newest first, with the tools that answered it."""
    with tx() as c:
        rows = c.execute(
            """SELECT f.created_at, u.username, t.question, t.answer, t.payload
               FROM feedback f JOIN turns t ON t.id = f.turn_id JOIN users u ON u.id = f.user_id
               WHERE f.rating = -1 ORDER BY f.created_at DESC LIMIT ?""", (limit,)).fetchall()
    return [{"created_at": r["created_at"], "username": r["username"],
             "question": r["question"], "answer": r["answer"],
             "tools_called": json.loads(r["payload"]).get("tools_called") or []}
            for r in rows]
