# Ansible keys

Every key and secret in the IQ Core → Semaphore → VM chain: what it opens, where it lives,
and how to revoke it. **Never write a key's value here or anywhere else in git.**
Setup steps are in [Ansible_Setup.md](Ansible_Setup.md).

One run, in order:

```
Zammad --webhook secret--> IQ Core --API token--> Semaphore --(public, no key)--> GitHub (playbook)
                                                      |
                                                      +--SSH key--> target VM (as iqdiag)
IQ Core --Zammad token--> Zammad (writes the output into the ticket)
```

## Semaphore's own secrets
All three are in `/opt/semaphore/.env` on VM 152, readable by root only. Created in setup step 3.

| Key | Connects | What it does | If it leaks or is lost | Status |
|---|---|---|---|---|
| `SEMAPHORE_ACCESS_KEY_ENCRYPTION` | Semaphore → its database | Encrypts the SSH keys stored in Semaphore | Leaked with a copy of the database: the stored SSH keys can be decrypted. Lost: every stored key must be re-entered. **Back it up** | In use |
| `POSTGRES_PASSWORD` | Semaphore → its PostgreSQL | Semaphore's login to its own database | Low risk: the database has no published port | In use |
| `SEMAPHORE_ADMIN_PASSWORD` | You → Semaphore web UI | The first admin password | Out of date: the password was changed in the UI on 2026-10-29 | Superseded |

## SSH key pairs
Created on VM 152. The private half goes into Semaphore's key store, then the file is shredded.

| Key | Connects | Private half | Public half | Status |
|---|---|---|---|---|
| `semaphore_ed25519` (setup step 6) | Semaphore → target VMs, as user `iqdiag` (no sudo) | `~/semaphore_ed25519` on 152 until stored in Semaphore as `iqdiag-ssh` | `/home/iqdiag/.ssh/authorized_keys` on each target VM. So far: `192.168.1.163` | In use: stored in Semaphore as `iqdiag-ssh`, first run on 163 succeeded 2026-10-29. Shred the file on 152 |
| `github_deploy` | Semaphore → GitHub `NikunjIQ/iq-playbooks`, read-only | — | — | Not needed: the repo is public. Needed only if it is made private |

**Revoke:**
- `semaphore_ed25519` on one VM: delete its line from that VM's `/home/iqdiag/.ssh/authorized_keys`. Everywhere: make a new pair, put the new public key on every VM, and replace `iqdiag-ssh` in Semaphore.
- `github_deploy`: delete it under GitHub → Deploy keys.

## IQ Core's tokens
In IQ Core's `backend/.env` on VM 152.

| Key | Connects | What it does | Status |
|---|---|---|---|
| `ANSIBLE_TOKEN` (setup step 9) | IQ Core → Semaphore API, as user `iqcore` | Starts the approved templates in the `IQ Core diagnostics` project and reads their output. Task Runner role, that project only | Created 2026-10-29, held in a password manager; not yet in `backend/.env` |
| `ZAMMAD_WRITE_TOKEN` | IQ Core → Zammad, as user "IQ Core Assist" | Reads tickets and adds the internal note | In use (ticket assist) |
| `ZAMMAD_WEBHOOK_SECRET` | Zammad → IQ Core | Proves a "new ticket" call really came from Zammad | In use (ticket assist) |

**Revoke:**
- `ANSIBLE_TOKEN`: delete it in Semaphore (`DELETE /api/user/tokens/{id}`) or remove `iqcore` from the project.
- `ZAMMAD_WRITE_TOKEN`: delete it from the "IQ Core Assist" user's tokens in Zammad.

## Not keys we created
- **Host keys** (`known_hosts`): each VM's own identity, saved on first connection so an
  impersonator is noticed. VM 163's was saved for the `ubuntu` user on 152 on 2026-10-29.
  Semaphore's container keeps its own list; see bottleneck B9.
