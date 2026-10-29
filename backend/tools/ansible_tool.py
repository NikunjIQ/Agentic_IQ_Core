"""Run an approved diagnostic playbook on one host, through the team's Ansible.

The Ansible server is Semaphore (a web UI and API around Ansible) on the IQ Core
VM. IQ Core never sends commands: it starts a template that already exists in
its own Semaphore project, narrowed to one host from that template's inventory,
and reads back the playbook's Report. The playbooks live in the iq-playbooks
repo and are read-only by rule.

Not a chat tool yet. Every chat tool only reads; this one acts, so it joins the
chat tool list only once the harness has a permission check for act tools.

ANSIBLE_MODE=off (default) does nothing, dry_run runs every check but starts no
playbook, run starts it.
"""

import os
import re
import textwrap
import time

from tools.clients import request

# Zabbix problem name (lowercase substring) -> Semaphore template name.
PLAYBOOK_FOR_ALERT = {
    "high cpu utilization": "Top processes",
    "load average is too high": "Top processes",
    "high memory utilization": "Top processes",
    "lack of available memory": "Top processes",
}

# One plain host. Ansible's limit reads spaces, commas, colons, * and ! as
# patterns, and a host named "all" or "*" would widen the run to every server.
_ONE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
_FINISHED = {"success", "error", "stopped", "rejected"}
# Semaphore's API returns Ansible's colour codes ("\x1b[0;32m"); its web page hides them.
_COLOUR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RUN_SECONDS = 120
POLL_SECONDS = 2
REPORT_CHARS = 3000
FAILURE_LINES = 15


def mode() -> str:
    value = os.getenv("ANSIBLE_MODE", "off").strip().lower()
    return value if value in ("dry_run", "run") else "off"


def playbook_for(problem_name: str):
    """The template that diagnoses this alert, or None."""
    text = (problem_name or "").lower()
    return next((name for key, name in PLAYBOOK_FOR_ALERT.items() if key in text), None)


def inventory_hosts(text: str) -> set:
    """Host names from a static INI inventory: the first word of each host line.

    shortcut: INI inventories only, no YAML and no host ranges like web[1:3];
    add them if the Semaphore inventory ever uses either.
    """
    hosts = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";", "[")):
            hosts.add(line.split()[0])
    return hosts


class AnsibleClient:
    """Semaphore's API, as IQ Core's own user (Task Runner in one project)."""

    def __init__(self):
        url, token = os.getenv("ANSIBLE_URL", ""), os.getenv("ANSIBLE_TOKEN", "")
        if not url or not token:
            raise RuntimeError("ANSIBLE_URL and ANSIBLE_TOKEN must be set")
        project = os.getenv("ANSIBLE_PROJECT_ID", "1")
        self.base = f"{url.rstrip('/')}/api/project/{project}"
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, path: str, retry: bool = True, **kwargs):
        res = request(method, f"{self.base}{path}", system="Ansible", retry=retry,
                      headers=self.headers, timeout=20, **kwargs)
        res.raise_for_status()
        return res.json() if res.content else None

    def templates(self) -> list:
        return self._call("GET", "/templates") or []

    def inventory(self, inventory_id) -> str:
        return (self._call("GET", f"/inventory/{inventory_id}") or {}).get("inventory", "")

    def start(self, template_id, host: str) -> int:
        # No retry: a POST that timed out may already have queued the run.
        task = self._call("POST", "/tasks", retry=False,
                          json={"template_id": template_id, "limit": host})
        return task["id"]

    def status(self, task_id) -> str:
        return (self._call("GET", f"/tasks/{task_id}") or {}).get("status", "")

    def output(self, task_id) -> list:
        return [row.get("output", "") for row in self._call("GET", f"/tasks/{task_id}/output") or []]


def report_from(lines: list, succeeded: bool) -> str:
    """What goes into the ticket: the Report task's message on success, the end
    of the log on failure (where Ansible says why, e.g. UNREACHABLE), and on a
    success whose Report cannot be found, so the note is never silently empty."""
    lines = [_COLOUR.sub("", part).rstrip() for line in lines for part in str(line).split("\n")]
    tail = "\n".join(lines[-FAILURE_LINES:]).strip("\n")[:REPORT_CHARS]
    if not succeeded:
        return tail
    keep, inside = [], False
    for line in lines:
        if "TASK [Report]" in line:
            inside = True
        elif inside and line.startswith("PLAY RECAP"):
            break
        elif inside and line.strip() not in ("", "msg:") and not line.lstrip().startswith("ok: ["):
            keep.append(line)
    return textwrap.dedent("\n".join(keep))[:REPORT_CHARS] or tail


def run_ansible(playbook: str, host: str, client=None, dry_run: bool = False) -> dict:
    """Run one approved playbook on one host and return its report.

    Raises ValueError for anything not approved: a host that is not one plain
    name, a playbook with no template in IQ Core's project, or a host missing
    from that template's inventory. Nothing has started when it raises.
    """
    if not _ONE_HOST.match(host or "") or host.lower() == "all":
        raise ValueError(f"not a single host: {host!r}")
    client = client or AnsibleClient()
    template = next((t for t in client.templates() if t.get("name") == playbook), None)
    if not template:
        raise ValueError(f"no template named {playbook!r} in IQ Core's Ansible project")
    if host not in inventory_hosts(client.inventory(template["inventory_id"])):
        raise ValueError(f"{host} is not in the inventory of {playbook!r}")

    found = {"playbook": playbook, "host": host}
    if dry_run:
        return {**found, "status": "dry_run"}

    task_id = client.start(template["id"], host)
    deadline, status = time.monotonic() + RUN_SECONDS, ""
    while time.monotonic() < deadline:
        status = client.status(task_id)
        if status in _FINISHED:
            break
        time.sleep(POLL_SECONDS)
    else:
        status = "timed out"
    return {**found, "task_id": task_id, "status": status,
            "report": report_from(client.output(task_id), status == "success")}
