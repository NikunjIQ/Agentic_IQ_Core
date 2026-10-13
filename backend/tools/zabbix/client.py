"""Authentication and JSON-RPC transport for the Zabbix API.

Username and password only. API tokens were supported and were the preferred
path, until one expired: the token still won over a valid username and
password, and the settings form treats a blank field as "unchanged", so there
was no way to clear it and Zabbix stayed broken until someone edited the
database. A password does not expire underneath the app like that.
"""

from tools import clients

# Signing in creates a session row in Zabbix, and every tool call and dashboard
# refresh builds a new transport, so signing in each time would add thousands a
# day. Kept per signed-in user, shorter than Zabbix's own idle logout.
_SESSION = clients.Cached("zabbix session", 300)


def _login(url, headers, username, password):
    """Exchange username and password for a session id.

    Returns (session_id, legacy). Zabbix renamed user.login's "user" parameter
    to "username" in 6.0 and removed the old spelling in 6.4, so a 7.x server
    rejects "user" outright. The modern name is tried first and the old one only
    as a fallback, which keeps this working against an older server without
    silently failing on a current one.

    Which spelling succeeded also tells the caller how to present the session:
    6.4+ dropped the "auth" request field and expects the session id in the
    Authorization header instead, exactly like an API token.
    """
    for field, legacy in (("username", False), ("user", True)):
        response = clients.request("POST", url, system="Zabbix", headers=headers, timeout=10, json={
            "jsonrpc": "2.0",
            "method": "user.login",
            "params": {field: username, "password": password},
            "id": 1,
        })
        response.raise_for_status()
        payload = response.json()
        if payload.get("result"):
            return payload["result"], legacy
        error = payload.get("error") or {}
        # Only an argument-shape complaint is worth retrying under the other
        # spelling. Wrong credentials must surface, not be tried twice.
        if "parameter" not in str(error.get("data", "")).lower():
            raise Exception(error or "user.login returned no result")
    raise Exception("user.login failed under both 'username' and 'user'")


def build_rpc():
    """Return a callable issuing authenticated JSON-RPC calls.

    Credentials are read at call time rather than at import. They can be changed
    through the UI while the process runs, and every tool reads the signed-in
    user's own settings at the moment it executes so a saved config takes effect without a restart.

    No username and password is not an error here — Zabbix answers some methods
    unauthenticated, and a real failure surfaces as an API error with a readable
    message.
    """
    clients._ensure_config("Zabbix")
    zabbix_url = clients.cfg("ZABBIX_URL", "http://192.168.1.205:8080")
    if not zabbix_url.endswith("api_jsonrpc.php"):
        zabbix_url = zabbix_url.rstrip("/") + "/api_jsonrpc.php"

    zabbix_user = clients.cfg("ZABBIX_USER", "")
    zabbix_pass = clients.cfg("ZABBIX_PASS", "")
    base_headers = {"Content-Type": "application/json-rpc"}

    def session(fresh=False):
        if not (zabbix_user and zabbix_pass):
            return None
        held = None if fresh else _SESSION.get()
        return held or _SESSION.put(_login(zabbix_url, base_headers, zabbix_user, zabbix_pass))

    def send(method, params, held):
        headers, payload = dict(base_headers), {
            "jsonrpc": "2.0", "method": method, "params": params, "id": 2}
        if held:
            token, legacy = held
            # 6.4+ carries the session in the header and rejects a payload "auth"
            # outright; only a pre-6.4 server still wants it in the request body.
            if legacy:
                payload["auth"] = token
            else:
                headers["Authorization"] = f"Bearer {token}"
        return clients.request("POST", zabbix_url, system="Zabbix", headers=headers,
                               json=payload, timeout=30).json()

    def rpc(method, params):
        held = session()
        data = send(method, params, held)
        # A held session can be ended on the Zabbix side at any time. Sign in
        # again once, rather than failing every call until the cache expires.
        if held and "error" in data and any(
                word in str(data["error"]).lower()
                for word in ("session", "re-login", "not authori")):
            data = send(method, params, session(fresh=True))
        if "error" in data:
            error = data["error"]
            raise RuntimeError((error.get("data") if isinstance(error, dict) else None) or error)
        return data.get("result", [])

    return rpc
