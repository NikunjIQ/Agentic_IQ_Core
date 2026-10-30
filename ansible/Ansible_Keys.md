# Ansible keys

Every key and secret in the IQ Core → Semaphore → VM chain: what it opens, where it lives,
and how to revoke it. **Never write a key's value here or anywhere else in git.**
Setup steps are in [DEPLOYMENT.md](../DEPLOYMENT.md).

One run, in order:

```
Zammad --webhook secret--> IQ Core --API token--> Semaphore --(public, no key)--> GitHub (playbook)
                                                      |
                                                      +--SSH key--> target VM (as iqdiag)
IQ Core --Zammad token--> Zammad (writes the output into the ticket)
```

## Semaphore's own secrets
All three are in `/opt/semaphore/.env` on VM 152, readable by root only. Created in DEPLOYMENT.md 2.2.

| Key | Connects | What it does | If it leaks or is lost | Status |
|---|---|---|---|---|
| `SEMAPHORE_ACCESS_KEY_ENCRYPTION` | Semaphore → its database | Encrypts the SSH keys stored in Semaphore | Leaked with a copy of the database: the stored SSH keys can be decrypted. Lost: every stored key must be re-entered. **Back it up** | In use |
| `POSTGRES_PASSWORD` | Semaphore → its PostgreSQL | Semaphore's login to its own database | Low risk: the database has no published port | In use |
| `SEMAPHORE_ADMIN_PASSWORD` | You → Semaphore web UI | The first admin password | Out of date: the password was changed in the UI on 2026-10-29 | Superseded |

## SSH key pairs
Created on VM 152. The private half goes into Semaphore's key store, then the file is shredded.

| Key | Connects | Private half | Public half | Status |
|---|---|---|---|---|
| `semaphore_ed25519` (DEPLOYMENT.md 2.5) | Semaphore → target VMs, as user `iqdiag` (no sudo) | `~/semaphore_ed25519` on 152 until stored in Semaphore as `iqdiag-ssh` | `/home/iqdiag/.ssh/authorized_keys` on each target VM. So far: `192.168.1.163` | In use: stored in Semaphore as `iqdiag-ssh`, first run on 163 succeeded 2026-10-29. Shred the file on 152 |
| `github_deploy` | Semaphore → GitHub `NikunjIQ/iq-playbooks`, read-only | — | — | Not needed: the repo is public. Needed only if it is made private |

**Revoke:**
- `semaphore_ed25519` on one VM: delete its line from that VM's `/home/iqdiag/.ssh/authorized_keys`. Everywhere: make a new pair, put the new public key on every VM, and replace `iqdiag-ssh` in Semaphore.
- `github_deploy`: delete it under GitHub → Deploy keys.

## IQ Core's tokens
In IQ Core's `backend/.env` on VM 152.

| Key | Connects | What it does | Status |
|---|---|---|---|
| `ANSIBLE_TOKEN` (DEPLOYMENT.md 2.7) | IQ Core → Semaphore API, as user `iqcore` | Starts the approved templates in the `IQ Core diagnostics` project and reads their output. Task Runner role, that project only | In use since 2026-10-29: created through the API straight into `backend/.env` on VM 152 (Semaphore's UI has no token button); a dry-run check against the real Semaphore passed |
| `ZAMMAD_WRITE_TOKEN` | IQ Core → Zammad 205, as user "IQ Core Assist" | Reads tickets and adds the internal note | In use (ticket assist) |
| `ZAMMAD_WEBHOOK_SECRET` | Zammad 205 → IQ Core port 8000 | Proves a "new ticket" call really came from Zammad 205 | In use (ticket assist) |

## The second Zammad's keys (VM 183)
In `/home/ubuntu/iqcore-colleague/env` on VM 152 (outside the repo, readable by `ubuntu`
only), used by the service `iqcore-colleague` on port 8001. Created 2026-10-30
(DEPLOYMENT.md 4b).

| Key | Connects | What it does | Status |
|---|---|---|---|
| `ZAMMAD_WRITE_TOKEN` | IQ Core → Zammad 183, as its own "IQ Core Assist" (group Users: READ + CHANGE) | Reads tickets and adds the internal note | In use: live tickets 165 and 166 written 2026-10-30 |
| `ZAMMAD_WEBHOOK_SECRET` | Zammad 183 → IQ Core port 8001 | Proves the call came from Zammad 183 | In use |
| `GROQ_API_KEY` (a copy) | IQ Core → Groq | The model key, copied from `backend/credentials.json`. **Rotating the key on the Connections page does not update this copy**: redo the copy command in DEPLOYMENT.md 4b | In use |

## Passwords kept in a password manager
- Semaphore `admin` and `iqcore` users (DEPLOYMENT.md 2.4, 2.7).
- "IQ Core Assist" in each Zammad (DEPLOYMENT.md 4.1).

**Revoke:**
- `ANSIBLE_TOKEN`: delete it in Semaphore (`DELETE /api/user/tokens/{id}`) or remove `iqcore` from the project.
- `ZAMMAD_WRITE_TOKEN`: delete it from the "IQ Core Assist" user's tokens in Zammad.

## Not keys we created
- **Host keys** (`known_hosts`): each VM's own identity, saved on first connection so an
  impersonator is noticed. VM 163's was saved for the `ubuntu` user on 152 on 2026-10-29.
  Semaphore's container keeps its own list; see bottleneck B9.
