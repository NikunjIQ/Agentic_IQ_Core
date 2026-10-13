"""Runtime settings, entered through the UI instead of a .env file.

Two kinds of setting live here, and the difference is the point of the module:

  - Per-user service credentials: Zabbix, Zammad, Prometheus, OpenSearch, Ceph.
    Stored per account in the database (agent.db), encrypted where secret. For a
    request made by a signed-in user, get() answers from THAT user's settings
    and nothing else. A service they have not connected raises NotConnected; it
    never quietly borrows whatever the process environment happens to hold.

  - System settings: the Groq key, and anything the Connections form does not
    expose (the second OpenSearch cluster, PROMETHEUS_TOKEN). These stay in
    credentials.json and the process environment, exactly as before. The model
    belongs to the deployment, not the user — it is what the credits pay for.

With no signed-in user in context — the eval harness, a one-off script — get()
reads the environment, so everything that ran before still runs unchanged.
"""

import contextvars
import json
import os
import threading
from pathlib import Path

CONFIG_PATH = Path(os.getenv("IQCORE_CONFIG_PATH",
                             Path(__file__).resolve().parent.parent / "credentials.json"))

_lock = threading.Lock()

# The form the UI renders. Each field: (env key, label, is_secret, placeholder).
FIELD_GROUPS = [
    ("Zabbix", "Network devices, Windows hosts", [
        ("ZABBIX_URL", "URL", False, "http://192.168.1.205:8080"),
        ("ZABBIX_USER", "Username", False, ""),
        ("ZABBIX_PASS", "Password", True, ""),
    ]),
    ("Zammad", "Helpdesk tickets", [
        ("ZAMMAD_URL", "URL", False, "http://192.168.1.205:8082"),
        ("ZAMMAD_API_TOKEN", "API token", True, ""),
        ("ZAMMAD_USER", "Username (only if no token)", False, ""),
        ("ZAMMAD_PASS", "Password (only if no token)", True, ""),
    ]),
    ("Prometheus", "Linux servers, OpenStack, VMs", [
        ("PROMETHEUS_URL", "URL", False, "http://192.168.1.29:9190"),
        ("PROMETHEUS_USER", "Username", False, ""),
        ("PROMETHEUS_PASS", "Password", True, ""),
        ("PROMETHEUS_TOKEN", "API token (optional)", True, ""),
    ]),
    ("OpenSearch", "OpenStack application logs", [
        ("OPENSEARCH_URL", "URL", False, "http://192.168.1.32:9200"),
        ("OPENSEARCH_INDEX", "Index pattern", False, "flog-*"),
        ("OPENSEARCH_USER", "Username", False, ""),
        ("OPENSEARCH_PASS", "Password", True, ""),
    ]),
    ("Ceph", "Storage under OpenStack: pools, OSDs, RBD images", [
        ("CEPH_URL", "URL", False, "https://192.168.1.30:8443"),
        ("CEPH_USER", "Username", False, "admin"),
        ("CEPH_PASS", "Password", True, ""),
        # Most dashboards run a self-signed certificate. Left as a field rather
        # than assumed, so a cluster with a real certificate can verify it.
        ("CEPH_VERIFY_TLS", "Verify TLS certificate (true/false)", False, "false"),
    ]),
    # A separate OpenSearch cluster holding what Ceph's containers print. Its
    # own section, not part of Ceph: different host and credentials, and until
    # it was listed here every user read it through the deployment's .env login.
    ("Ceph container logs", "Ceph daemon stdout (second OpenSearch, HTTPS)", [
        ("OPENSEARCH2_URL", "URL", False, "https://192.168.1.152:9200"),
        ("OPENSEARCH2_CEPH_INDEX", "Index pattern", False, "podman-20*"),
        ("OPENSEARCH2_USER", "Username", False, ""),
        ("OPENSEARCH2_PASS", "Password", True, ""),
        ("OPENSEARCH2_VERIFY_TLS", "Verify TLS certificate (true/false)", False, "false"),
    ]),
    ("Groq", "The AI model provider", [
        ("GROQ_API_KEY", "API key", True, ""),
    ]),
]

ALL_KEYS = [key for _, _, fields in FIELD_GROUPS for key, _, _, _ in fields]
SECRET_KEYS = {key for _, _, fields in FIELD_GROUPS for key, _, secret, _ in fields if secret}

# Services whose settings belong to the deployment rather than to one account.
SYSTEM_SERVICES = {"Groq"}
SYSTEM_KEYS = {key for service, _, fields in FIELD_GROUPS if service in SYSTEM_SERVICES
               for key, *_ in fields}
USER_KEYS = set(ALL_KEYS) - SYSTEM_KEYS
SERVICE_OF = {key: service for service, _, fields in FIELD_GROUPS for key, *_ in fields}


class NotConnected(RuntimeError):
    """The signed-in user has not connected the service a tool needs."""


# ------------------------------------------------------------- per request
# Set once per request, in the thread that serves it — the same discipline as
# the streaming emitter and the telemetry recorder, because a context variable
# does not follow into a new thread on its own.
_user = contextvars.ContextVar("iqcore_user", default=None)


def use_user(user_id) -> None:
    """Make this user's settings the ones get() answers from, in this context."""
    if user_id is None:
        _user.set(None)
        return
    from agent import db
    _user.set({"id": user_id, "services": db.load_settings(user_id)})


def current_user_id():
    ctx = _user.get()
    return ctx["id"] if ctx else None


def get(key: str, default: str = "") -> str:
    """A setting for the current request.

    A per-user service field comes from the signed-in user's own settings; every
    other key, and every key when nobody is signed in, from the environment.
    """
    ctx = _user.get()
    if ctx is None or key not in USER_KEYS:
        return os.getenv(key, default)
    return ctx["services"].get(SERVICE_OF[key], {}).get(key) or default


def connected(service: str) -> bool:
    ctx = _user.get()
    return ctx is None or service in ctx["services"]


def require(service: str) -> None:
    if not connected(service):
        raise NotConnected(f"{service} isn't connected for your account — "
                           f"add it on the Connections page.")


# ---------------------------------------------------- the system settings file

def load() -> dict:
    """Whatever was last saved. Empty dict if the app has never been configured."""
    try:
        with _lock:
            if CONFIG_PATH.exists():
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def apply_to_environment(config: dict = None) -> int:
    """Push saved values into os.environ.

    This is the no-user path: scripts, the eval harness, and the system Groq key
    that ChatGroq reads from the environment. A signed-in request never reads a
    per-user service key from here — see get().
    """
    config = load() if config is None else config
    applied = 0
    for key in ALL_KEYS:
        value = str(config.get(key, "") or "").strip()
        if value:
            os.environ[key] = value
            applied += 1
    return applied


_applied_once = False


def ensure_applied() -> None:
    """Apply saved system settings once per process, whatever the entry point."""
    global _applied_once
    if not _applied_once:
        _applied_once = True
        try:
            apply_to_environment()
        except Exception:
            pass


def reset_caches(user_id=None) -> list:
    """Drop cached lookups after settings change — one user's, or everyone's.

    Without this the app keeps serving tickets, devices and schema fetched with
    the previous credentials, which looks like the new settings silently failing.
    The TTL caches register themselves with tools.clients.Cached, so this cannot
    miss one the way naming each module's private dict could.
    """
    cleared = []

    try:
        from tools import clients, correlation, vm_storage_correlation  # noqa: F401
        from agent import metadata                                     # noqa: F401
        cleared += (clients.Cached.clear_all() if user_id is None
                    else clients.Cached.clear_owner_all(user_id))
    except Exception:
        pass

    try:
        from agent import answer_cache
        if user_id is None:
            answer_cache.clear()
        else:
            answer_cache.clear_user(user_id)
        cleared.append("answers")
    except Exception:
        pass

    return cleared


def save(updates: dict) -> dict:
    """Merge, write, apply, and invalidate caches — for the system settings file.

    Blank values are treated as "leave unchanged" so the UI can show masked
    secrets without the user having to retype them on every edit.
    """
    config = load()
    changed = []
    for key in ALL_KEYS:
        if key not in updates:
            continue
        value = str(updates.get(key) or "").strip()
        if not value:
            continue
        if config.get(key) != value:
            changed.append(key)
        config[key] = value

    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        try:
            os.chmod(CONFIG_PATH, 0o600)   # best effort; a no-op on some Windows setups
        except Exception:
            pass

    apply_to_environment(config)
    cleared = reset_caches()
    return {"saved_keys": sorted(config), "changed": changed, "caches_cleared": cleared,
            "path": str(CONFIG_PATH)}


def _mask(key: str, value: str) -> str:
    if not value:
        return ""
    if key in SECRET_KEYS:
        return f"{'•' * 8}{value[-4:]}" if len(value) > 4 else "•" * 8
    return value


def masked() -> dict:
    """The system file's config with secrets obscured."""
    config = load()
    return {key: _mask(key, str(config.get(key, "") or "")) for key in ALL_KEYS}


def is_configured() -> bool:
    config = load()
    return any(str(config.get(k, "")).strip() for k in ALL_KEYS)


# ------------------------------------------------------ one user's settings

def user_values(user_id) -> dict:
    """Every per-user field for this user, blank where they have not set it."""
    from agent import db
    services = db.load_settings(user_id)
    return {key: services.get(SERVICE_OF[key], {}).get(key, "") for key in USER_KEYS}


def user_masked(user_id, include_system: bool = False) -> dict:
    """This user's settings with secrets obscured. Admins also see the system key."""
    values = user_values(user_id)
    if include_system:
        values.update({k: os.getenv(k, "") or str(load().get(k, "") or "")
                       for k in SYSTEM_KEYS})
    return {key: _mask(key, str(value or "")) for key, value in values.items()}


def user_merged(user_id, updates: dict = None) -> dict:
    """What a connection test should check: the user's stored values, the system
    Groq key, and anything non-blank typed into the form on top."""
    merged = user_values(user_id)
    merged.update({k: os.getenv(k, "") for k in SYSTEM_KEYS})
    merged.update({k: v for k, v in (updates or {}).items() if str(v or "").strip()})
    return merged


def user_is_configured(user_id) -> bool:
    from agent import db
    return bool(db.load_settings(user_id))


def user_save(user_id, updates: dict, allow_system: bool = False) -> dict:
    """Save one user's settings, service by service. Blank means unchanged.

    A service is only written once it has at least one value, so saving a form
    with the Ceph section empty does not create a Ceph connection. System keys
    are written only when the caller is allowed to (the admin), and go to the
    system file and the environment exactly as before.
    """
    from agent import db
    services = db.load_settings(user_id)
    changed = []
    for service, _desc, fields in FIELD_GROUPS:
        if service in SYSTEM_SERVICES:
            continue
        existing = services.get(service, {})
        merged = dict(existing)
        for key, *_ in fields:
            value = str(updates.get(key) or "").strip()
            if value and existing.get(key) != value:
                merged[key] = value
                changed.append(key)
        if merged != existing:
            db.save_settings(user_id, service, merged)

    if allow_system:
        system = {k: str(v).strip() for k, v in updates.items()
                  if k in SYSTEM_KEYS and str(v or "").strip()}
        if system:
            before = load()
            changed += [k for k, v in system.items() if before.get(k) != v]
            save(system)

    return {"changed": changed, "caches_cleared": reset_caches(user_id)}
