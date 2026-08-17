"""Authentication and JSON-RPC transport for the Zabbix API."""

import os

import requests


def build_rpc():
    """Return a callable issuing authenticated JSON-RPC calls.

    Credentials are read at call time rather than at import. They can be changed
    through the UI while the process runs, and every tool reads os.getenv at the
    moment it executes so a saved config takes effect without a restart.

    A token is preferred; user/password falls back to user.login. Neither being
    present is not an error here — Zabbix answers some methods unauthenticated,
    and a real failure surfaces as an API error with a readable message.
    """
    zabbix_url = os.getenv("ZABBIX_URL", "http://192.168.1.205:8080")
    if not zabbix_url.endswith("api_jsonrpc.php"):
        zabbix_url = zabbix_url.rstrip("/") + "/api_jsonrpc.php"

    zabbix_token = os.getenv("ZABBIX_API_TOKEN", "")
    zabbix_user = os.getenv("ZABBIX_USER", "")
    zabbix_pass = os.getenv("ZABBIX_PASS", "")

    headers = {"Content-Type": "application/json-rpc"}

    if zabbix_token:
        headers["Authorization"] = f"Bearer {zabbix_token}"
        auth_param = None
    elif zabbix_user and zabbix_pass:
        login_payload = {
            "jsonrpc": "2.0",
            "method": "user.login",
            "params": {"user": zabbix_user, "password": zabbix_pass},
            "id": 1
        }
        res = requests.post(zabbix_url, headers=headers, json=login_payload, timeout=10)
        res.raise_for_status()
        auth_param = res.json().get("result")
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
        r = requests.post(zabbix_url, headers=headers, json=payload, timeout=30)
        data = r.json()
        if "error" in data:
            raise Exception(data["error"])
        return data.get("result", [])

    return rpc
