"""Authentication and JSON-RPC transport for the Zabbix API."""

from tools import clients


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

    A token is preferred; user/password falls back to user.login. Neither being
    present is not an error here — Zabbix answers some methods unauthenticated,
    and a real failure surfaces as an API error with a readable message.
    """
    clients._ensure_config("Zabbix")
    zabbix_url = clients.cfg("ZABBIX_URL", "http://192.168.1.205:8080")
    if not zabbix_url.endswith("api_jsonrpc.php"):
        zabbix_url = zabbix_url.rstrip("/") + "/api_jsonrpc.php"

    zabbix_token = clients.cfg("ZABBIX_API_TOKEN", "")
    zabbix_user = clients.cfg("ZABBIX_USER", "")
    zabbix_pass = clients.cfg("ZABBIX_PASS", "")

    headers = {"Content-Type": "application/json-rpc"}

    if zabbix_token:
        headers["Authorization"] = f"Bearer {zabbix_token}"
        auth_param = None
    elif zabbix_user and zabbix_pass:
        session, legacy = _login(zabbix_url, headers, zabbix_user, zabbix_pass)
        # 6.4+ carries the session in the header and rejects a payload "auth"
        # outright; only a pre-6.4 server still wants it in the request body.
        if legacy:
            auth_param = session
        else:
            headers["Authorization"] = f"Bearer {session}"
            auth_param = None
    else:
        auth_param = None

    def rpc(method, params):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 2
        }
        if auth_param:
            payload["auth"] = auth_param
        r = clients.request("POST", zabbix_url, system="Zabbix", headers=headers,
                              json=payload, timeout=30)
        data = r.json()
        if "error" in data:
            raise Exception(data["error"])
        return data.get("result", [])

    return rpc
