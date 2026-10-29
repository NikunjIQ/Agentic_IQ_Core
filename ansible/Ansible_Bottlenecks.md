# Ansible bottlenecks

Everything that can stall the Ansible work, worked through one at a time. Setup steps are in
[Ansible_Setup.md](Ansible_Setup.md). Update **Status** as each one is settled.

Phases referred to below:
0 Ansible server · 1 reach the VMs · 2 playbooks · 3 IQ Core client · 4 dry run on real
tickets · 5 go live · 6 harness permissions.

---

## B1. Zabbix host names don't match the inventory
- **Problem:** tickets name hosts like `Zabbix server`, `Nikunj VM`, `192.168.1.209 OS`, `hp`.
  The inventory can't use those names, and a wrong name means the playbook runs on nothing.
- **Blocks:** phase 3.
- **Plan:** key the inventory by IP. IQ Core looks up the host's IP from Zabbix
  (`host.get` with interfaces) instead of trusting the name in the ticket text.
- **Status:** Partly handled. `tools/ansible_tool.py` accepts only a host that is one plain name and appears exactly in the template's inventory, so a ticket whose host is a Zabbix display name (`Nikunj VM`) is skipped and logged. Looking the IP up in Zabbix is still to do.

## B2. The tickets we get are mostly not CPU
- **Problem:** in the 20-ticket review sample, 1 was high load. Most were Zabbix's own internal
  processes being busy, and disk read/write response times.
- **Blocks:** phase 2 (which playbooks to write first).
- **Plan:** count problem types across a month of tickets, then write playbooks for the top
  three. Likely: disk I/O, Zabbix server health, top processes.
- **Status:** Open. `diag_top_processes.yml` exists; the others are not written.

## B3. SSH access to every VM
- **Problem:** each target VM needs the `iqdiag` user and our public key, and VM 152 must be
  able to reach it on port 22 through any firewalls. That's manual and depends on each
  VM's owner.
- **Blocks:** phase 1.
- **Plan:** start with 2 or 3 VMs that raise the most tickets. Once Semaphore works, a
  bootstrap playbook (run once with an admin account) can create `iqdiag` on the rest.
- **Status:** Open.

## B4. Mixed operating systems
- **Problem:** Windows hosts use WinRM, not SSH, and need different playbooks. Cisco devices
  need network modules and have no "top processes" view.
- **Blocks:** phase 2 for those hosts.
- **Plan:** Linux only at first. IQ Core skips non-Linux hosts and writes the AI suggestion
  as it does today.
- **Status:** Open.

## B5. "Server down" tickets
- **Problem:** if the VM is down, SSH fails and there is nothing to diagnose.
- **Blocks:** nothing; it changes what the note says.
- **Plan:** when Semaphore reports the host unreachable, the note says so plainly. That is
  useful on its own.
- **Status:** Open.

## B6. Alert storms
- **Problem:** we have seen 67 tickets in a day. Each would start an SSH run.
- **Blocks:** phase 5.
- **Plan:** skip a run when the same host and problem ran in the last hour, and add a daily
  cap, as ticket assist already does.
- **Status:** Partly handled. Runs share ticket assist's daily cap (`TICKET_ASSIST_DAILY_LIMIT`); no per-host repeat window yet.

## B7. Token permissions
- **Problem:** Semaphore roles cover a whole project. A token that can run one template
  can run every template in that project.
- **Blocks:** phase 3.
- **Plan:** a separate project `IQ Core diagnostics` with read-only playbooks only. IQ Core's
  user has the Task Runner role there and no other project.
- **Status:** Done 2026-10-29: user `iqcore`, Task Runner in `IQ Core diagnostics` only.

## B8. Who approves playbooks
- **Problem:** the whole team writes playbooks. Without review, a restart or delete playbook
  could land in IQ Core's project.
- **Blocks:** phase 5.
- **Plan:** playbooks live in their own repo, `NikunjIQ/iq-playbooks`, with teammates as
  collaborators and rules in its README. IQ Core's project only lists templates someone
  has approved.
- **Gap:** a new playbook needs a template before IQ Core can run it, but an edit to an
  *existing* playbook is live on its next run. A collaborator can commit straight to `main`
  unless branch protection requires a reviewed pull request, and GitHub only offers branch
  protection on private repos with a paid plan (Pro, Team). Options: pay for Pro or Team,
  make the repo public (playbooks hold no secrets; inventory and keys stay in Semaphore),
  or accept the gap and review the commit history.
- **Status:** Open. The repo is public (checked 2026-10-29), so branch protection is free:
  GitHub → `iq-playbooks` → Settings → Branches → add a rule for `main` requiring a pull
  request with 1 approval. Still needs a named reviewer. The gap is already live: on
  2026-10-29 `diag_router_issue.yml` went straight to `main` with no pull request. Being public also means playbooks
  must never contain IPs, host names or credentials (README rule 4).

## B9. SSH host key checking
- **Problem:** the first run against a new VM fails with `Host key verification failed`
  because Semaphore has never seen that VM's key. Turning the check off fixes it but lets
  an impersonating machine receive our SSH connection.
- **Blocks:** phase 1.
- **Plan:** acceptable on our LAN for now (`ANSIBLE_HOST_KEY_CHECKING=False` in the
  Semaphore environment). Later, collect the VMs' host keys into a `known_hosts` file.
- **Status:** Open.

## B10. Secrets in output
- **Problem:** command output is pasted into Zammad, where many people read it. Full
  process command lines can contain passwords and tokens.
- **Blocks:** phase 5.
- **Plan:** playbooks print process names only (`comm`, not `args`), and every new playbook
  is reviewed for what it prints.
- **Status:** Handled in `diag_top_processes.yml`; applies to every new playbook.

## B11. The SSH key is a master key
- **Problem:** the `iqdiag` key reaches every target VM. Whoever controls Semaphore controls it.
  Semaphore runs on VM 152, which also hosts IQ Core, Zabbix and Zammad, so a break-in or
  outage there hits monitoring, tickets and SSH access at once.
- **Blocks:** nothing; it's a standing risk, accepted on 2026-10-29 to keep it to one VM.
- **Plan:** `iqdiag` has no sudo, port 3000 is open to the LAN only, the admin password is
  changed after first login, and `.env` stays readable by root only.
- **Status:** Partly handled by setup steps 3, 5 and 7.

## B12. Docker network packet size (MTU)
- **Problem:** VM 152's network (`ens3`) uses MTU 1450, Docker's networks 1500. HTTPS from the
  Semaphore container hung, and the first run failed with `Connection reset by peer` on the
  `git clone` from GitHub.
- **Blocks:** phase 2 (Semaphore could not fetch playbooks).
- **Plan:** set the Semaphore network's MTU to 1450 in `docker-compose.yml` (setup step 4).
- **Status:** Solved 2026-10-29. The other Docker networks on VM 152 (Zammad, Zabbix) are
  still on 1500 and may hit the same problem on large outgoing HTTPS; not checked.
