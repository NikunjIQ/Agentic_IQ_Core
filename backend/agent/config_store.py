"""Runtime credential storage, set through the UI instead of a .env file.

The app is configured by whoever runs it, not by editing files on the server.
Credentials entered in the sidebar are written here and applied to the process
environment, which is all the tools read.

Precedence: values saved here win over anything in .env, so a saved config is
the source of truth once it exists. .env still works as a fallback, which keeps
local development unchanged.
"""

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
        ("ZABBIX_API_TOKEN", "API token", True, ""),
        ("ZABBIX_USER", "Username (only if no token)", False, ""),
        ("ZABBIX_PASS", "Password (only if no token)", True, ""),
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
    ]),
    ("OpenSearch", "OpenStack application logs", [
        ("OPENSEARCH_URL", "URL", False, "http://192.168.1.32:9200"),
        ("OPENSEARCH_INDEX", "Index pattern", False, "flog-*"),
        ("OPENSEARCH_USER", "Username", False, ""),
        ("OPENSEARCH_PASS", "Password", True, ""),
    ]),
    ("Groq", "The AI model provider", [
        ("GROQ_API_KEY", "API key", True, ""),
    ]),
]

ALL_KEYS = [key for _, _, fields in FIELD_GROUPS for key, _, _, _ in fields]
SECRET_KEYS = {key for _, _, fields in FIELD_GROUPS for key, _, secret, _ in fields if secret}


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

    Every tool builds its client fresh per call and reads os.getenv at that
    moment, so updating the environment takes effect on the next question with
    no restart.
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
    """Apply saved credentials once per process, whatever the entry point.

    Credentials live in two places — .env and this file — and only main.py used
    to merge them. Anything else (the eval harness, a one-off script) silently
    ran unconfigured, which looks like a broken system rather than a missing
    call. Every client and the metadata fetch call this, so the merge happens
    wherever the process started.
    """
    global _applied_once
    if not _applied_once:
        _applied_once = True
        try:
            apply_to_environment()
        except Exception:
            pass


def reset_caches() -> list:
    """Drop every cached lookup after the credentials change.

    Without this the app keeps serving tickets, devices and schema fetched with
    the previous credentials, which looks like the new settings silently failing.
    """
    cleared = []

    try:
        from tools import clients
        clients._TICKET_CACHE["tickets"] = None
        clients._TICKET_CACHE["fetched_at"] = 0.0
        clients._PROM_NODE_CACHE["map"] = None
        clients._PROM_NODE_CACHE["fetched_at"] = 0.0
        cleared += ["zammad tickets", "prometheus nodes"]
    except Exception:
        pass

    try:
        from agent import metadata
        metadata._METADATA_CACHE["data"] = None
        metadata._METADATA_CACHE["last_fetched"] = 0
        cleared.append("live schema")
    except Exception:
        pass

    try:
        from tools import correlation
        correlation._DEVICE_CACHE["data"] = None
        correlation._DEVICE_CACHE["fetched_at"] = 0
        cleared.append("device index")
    except Exception:
        pass

    try:
        from agent import answer_cache
        answer_cache.clear()
        cleared.append("answers")
    except Exception:
        pass

    return cleared


def save(updates: dict) -> dict:
    """Merge, write, apply, and invalidate caches.

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


def masked() -> dict:
    """Current config with secrets obscured, for display in the UI."""
    config = load()
    out = {}
    for key in ALL_KEYS:
        value = str(config.get(key, "") or "")
        if not value:
            out[key] = ""
        elif key in SECRET_KEYS:
            out[key] = f"{'•' * 8}{value[-4:]}" if len(value) > 4 else "•" * 8
        else:
            out[key] = value
    return out


def is_configured() -> bool:
    config = load()
    return any(str(config.get(k, "")).strip() for k in ALL_KEYS)
