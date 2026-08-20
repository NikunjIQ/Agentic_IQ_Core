"""Connection checks for the credential form.

Saving credentials that do not work produces an app that looks fine and answers
every question with "could not connect". Each system is probed with the
cheapest call that proves both reachability and authentication.

Values are passed in explicitly rather than read from the environment, so the
UI can test before committing anything.
"""

import requests
from requests.auth import HTTPBasicAuth

TIMEOUT = 10


def _ok(message, **extra):
    return {"ok": True, "message": message, **extra}


def _fail(message):
    return {"ok": False, "message": message}


def _describe(exc: Exception) -> str:
    """Say what the user should change, not what Python objected to."""
    name = type(exc).__name__
    if "Timeout" in name:
        return "timed out — host reachable but not responding"
    if "ConnectionError" in name:
        return "could not connect — check the URL, port and network"
    if "JSONDecode" in name or "JSONDecode" in str(exc):
        return "something answered, but not with JSON — is this the right URL and port?"
    if any(k in name for k in ("MissingSchema", "InvalidSchema", "InvalidURL")):
        return "the URL is missing http:// or https:// at the start"
    if "SSL" in name:
        return "TLS handshake failed — try http:// if the service is not using HTTPS"
    return f"{name}: {str(exc)[:120]}"


def test_zabbix(cfg: dict) -> dict:
    url = str(cfg.get("ZABBIX_URL", "")).strip().rstrip("/")
    if not url:
        return _fail("no URL set")
    if not url.endswith("api_jsonrpc.php"):
        url += "/api_jsonrpc.php"

    token = str(cfg.get("ZABBIX_API_TOKEN", "")).strip()
    headers = {"Content-Type": "application/json-rpc"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        # apiinfo.version separates "wrong URL" from "wrong credentials", which is
        # the distinction the user needs in order to fix it. It must be sent
        # WITHOUT the auth header — Zabbix 7.x rejects it outright otherwise.
        res = requests.post(url, headers={"Content-Type": "application/json-rpc"},
                            timeout=TIMEOUT, json={
                                "jsonrpc": "2.0", "method": "apiinfo.version",
                                "params": {}, "id": 1})
        version = res.json().get("result")
        if not version:
            return _fail("reachable, but did not look like a Zabbix API endpoint")

        if not token:
            user = str(cfg.get("ZABBIX_USER", "")).strip()
            password = str(cfg.get("ZABBIX_PASS", "")).strip()
            if not (user and password):
                return _fail(f"Zabbix {version} reachable, but no API token or username/password given")
            for key in ("username", "user"):
                probe = requests.post(url, headers=headers, timeout=TIMEOUT, json={
                    "jsonrpc": "2.0", "method": "user.login",
                    "params": {key: user, "password": password}, "id": 1}).json()
                if probe.get("result"):
                    return _ok(f"Zabbix {version} — signed in as {user}")
            return _fail(f"Zabbix {version} reachable, but the username/password was rejected")

        probe = requests.post(url, headers=headers, timeout=TIMEOUT, json={
            "jsonrpc": "2.0", "method": "host.get",
            "params": {"output": ["hostid"], "limit": 1}, "id": 2}).json()
        if "error" in probe:
            return _fail(f"Zabbix {version} reachable, but the API token was rejected")
        return _ok(f"Zabbix {version} — token accepted")
    except Exception as exc:
        return _fail(_describe(exc))


def test_zammad(cfg: dict) -> dict:
    url = str(cfg.get("ZAMMAD_URL", "")).strip().rstrip("/")
    if not url:
        return _fail("no URL set")

    headers, auth = {}, None
    token = str(cfg.get("ZAMMAD_API_TOKEN", "")).strip()
    if token:
        headers["Authorization"] = f"Token token={token}"
    elif cfg.get("ZAMMAD_USER"):
        auth = HTTPBasicAuth(str(cfg["ZAMMAD_USER"]).strip(),
                             str(cfg.get("ZAMMAD_PASS", "")).strip())
    else:
        return _fail("no API token or username/password given")

    try:
        res = requests.get(f"{url}/api/v1/ticket_states", headers=headers, auth=auth,
                           timeout=TIMEOUT)
        if res.status_code in (401, 403):
            return _fail("reachable, but the credentials were rejected")
        res.raise_for_status()
        states = res.json()
        return _ok(f"Zammad — {len(states)} ticket states visible")
    except Exception as exc:
        return _fail(_describe(exc))


def test_prometheus(cfg: dict) -> dict:
    url = str(cfg.get("PROMETHEUS_URL", "")).strip().rstrip("/")
    if not url:
        return _fail("no URL set")
    user = str(cfg.get("PROMETHEUS_USER", "")).strip()
    auth = HTTPBasicAuth(user, str(cfg.get("PROMETHEUS_PASS", "")).strip()) if user else None

    try:
        res = requests.get(f"{url}/api/v1/query", params={"query": "up"},
                           auth=auth, timeout=TIMEOUT)
        if res.status_code in (401, 403):
            return _fail("reachable, but the credentials were rejected")
        res.raise_for_status()
        body = res.json()
        if body.get("status") != "success":
            return _fail("reachable, but the query was refused")
        return _ok(f"Prometheus — {len(body.get('data', {}).get('result', []))} scrape targets")
    except Exception as exc:
        return _fail(_describe(exc))


def test_opensearch(cfg: dict) -> dict:
    url = str(cfg.get("OPENSEARCH_URL", "")).strip().rstrip("/")
    if not url:
        return _fail("no URL set")
    index = str(cfg.get("OPENSEARCH_INDEX", "flog-*")).strip() or "flog-*"
    user = str(cfg.get("OPENSEARCH_USER", "")).strip()
    auth = HTTPBasicAuth(user, str(cfg.get("OPENSEARCH_PASS", "")).strip()) if user else None

    try:
        res = requests.get(url, auth=auth, timeout=TIMEOUT)
        if res.status_code in (401, 403):
            return _fail("reachable, but the credentials were rejected")
        res.raise_for_status()
        info = res.json()
        name = info.get("version", {}).get("distribution", "elasticsearch")

        # Reaching the cluster is not enough — the index pattern has to match
        # something, or every log question comes back empty.
        count = requests.post(f"{url}/{index}/_count", auth=auth, timeout=TIMEOUT,
                              json={"query": {"match_all": {}}})
        if count.status_code == 404:
            return _fail(f"{name} reachable, but no index matches '{index}'")
        total = count.json().get("count", 0)
        if not total:
            return _fail(f"{name} reachable, but '{index}' contains no documents")
        return _ok(f"{name} {info.get('version', {}).get('number', '')} — "
                   f"{total:,} documents in '{index}'")
    except Exception as exc:
        return _fail(_describe(exc))


def test_ceph(cfg: dict) -> dict:
    """Log in and read cluster health.

    Reaching the dashboard is not enough: the API refuses an unversioned Accept
    header with 415, and every endpoint has its own version, so a check that only
    confirmed the port was open would pass against a cluster nothing can query.
    """
    url = str(cfg.get("CEPH_URL", "")).strip().rstrip("/")
    if not url:
        return _fail("no URL set")
    # The dashboard's browser address carries a frontend fragment the API never
    # sees; strip it so a pasted URL still works.
    if "/#" in url:
        url = url.split("/#")[0].rstrip("/")

    user = str(cfg.get("CEPH_USER", "")).strip()
    password = str(cfg.get("CEPH_PASS", "")).strip()
    if not (user and password):
        return _fail("no username/password given — Ceph has no long-lived API key")

    verify = str(cfg.get("CEPH_VERIFY_TLS", "false")).strip().lower() in ("1", "true", "yes")
    if not verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    accept = {"Accept": "application/vnd.ceph.api.v1.0+json",
              "Content-Type": "application/json"}
    try:
        auth = requests.post(f"{url}/api/auth", verify=verify, timeout=TIMEOUT,
                             headers=accept,
                             json={"username": user, "password": password})
        if auth.status_code in (400, 401, 403):
            return _fail("reachable, but the username/password was rejected")
        if auth.status_code == 415:
            return _fail("reachable, but refused the API version — is this a Ceph dashboard?")
        auth.raise_for_status()
        token = (auth.json() or {}).get("token")
        if not token:
            return _fail("signed in, but no token was returned")

        health = requests.get(f"{url}/api/health/minimal", verify=verify, timeout=TIMEOUT,
                              headers={"Accept": "application/vnd.ceph.api.v1.0+json",
                                       "Authorization": f"Bearer {token}"})
        if health.status_code == 401:
            return _fail("signed in, but the token was not accepted")
        health.raise_for_status()
        body = health.json() or {}
        status = (body.get("health") or {}).get("status", "unknown")
        osds = len((body.get("osd_map") or {}).get("osds") or [])
        return _ok(f"Ceph — signed in as {user}, cluster {status}, {osds} OSD(s)")
    except Exception as exc:
        return _fail(_describe(exc))


def test_groq(cfg: dict) -> dict:
    key = str(cfg.get("GROQ_API_KEY", "")).strip()
    if not key:
        return _fail("no API key set")
    try:
        res = requests.get("https://api.groq.com/openai/v1/models",
                           headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
        if res.status_code in (401, 403):
            return _fail("the API key was rejected")
        res.raise_for_status()
        models = [m.get("id") for m in res.json().get("data", [])]
        # Checked against the ids the app will actually call, so a retired model
        # is reported here rather than surfacing as a 404 mid-answer.
        from agent.models import REQUIRED_MODELS
        needed = [m for m in REQUIRED_MODELS if m not in models]
        if needed:
            return _fail(f"key works, but these models are unavailable: {', '.join(needed)}")
        return _ok(f"Groq — key valid, {len(models)} models available")
    except Exception as exc:
        return _fail(_describe(exc))


TESTS = {
    "Zabbix": test_zabbix,
    "Zammad": test_zammad,
    "Prometheus": test_prometheus,
    "Ceph": test_ceph,
    "OpenSearch": test_opensearch,
    "Groq": test_groq,
}


def test_all(cfg: dict, systems=None) -> dict:
    """Check each system. Listing a model call is avoided — this costs no tokens."""
    results = {}
    for name, check in TESTS.items():
        if systems and name not in systems:
            continue
        try:
            results[name] = check(cfg)
        except Exception as exc:
            results[name] = _fail(_describe(exc))
    return results
