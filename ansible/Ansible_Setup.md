# Ansible setup

How we run Ansible for IQ Core: what gets installed, where, and the exact commands.
Open problems are tracked separately in [Ansible_Bottlenecks.md](Ansible_Bottlenecks.md),
and every key and secret in [Ansible_Keys.md](Ansible_Keys.md).

## What we install and why

**Semaphore UI** (open source, MIT) on VM 152. It is a web UI and REST API around Ansible:
the team writes playbooks in git, Semaphore runs them over SSH, and IQ Core starts runs and
reads their output through the API.

We chose it over AWX because AWX has had no official release since July 2024 and needs
Kubernetes to install. Semaphore is two Docker containers: Semaphore itself (Ansible is
bundled inside its image) and PostgreSQL for its data.

```
Zabbix -> Zammad ticket -> IQ Core --API--> Semaphore (VM 152) --SSH--> target VM
                              ^                                             |
                              +------------- output pasted into ticket <----+
```

## What we need

| Need | Detail |
|---|---|
| Server | VM 152 (`192.168.1.152`, `user-test`), Ubuntu 24.04. It also runs IQ Core, Zabbix and Zammad; Semaphore adds about 1 GB RAM and a few GB disk |
| Inbound | Port 3000 on VM 152, reachable from the team. IQ Core is on the same VM and uses `localhost` |
| Outbound | SSH (port 22) from VM 152 to every target VM |
| Git | A playbook-only repo, `NikunjIQ/iq-playbooks`, separate from IQ Core's code. It is public, so Semaphore needs no key to read it |
| Targets | A user `iqdiag` with no sudo and our SSH public key, on each Linux VM to be diagnosed |

Versions pinned below: Semaphore `v2.19.17` (latest stable, 2026-10-20), PostgreSQL `16`.

## 1. Check the OS (on VM 152)

```bash
cat /etc/os-release
```

The commands below are for Ubuntu. On RHEL, Rocky or Debian, stop and ask: the Docker install step differs.

## 2. Install Docker

```bash
sudo apt-get update
```
```bash
sudo apt-get install -y docker.io docker-compose-v2
```
```bash
sudo systemctl enable --now docker
```
```bash
sudo docker compose version
```

## 3. Create the Semaphore folder and its secrets

The secrets are generated on the server and never typed or pasted anywhere.

```bash
sudo mkdir -p /opt/semaphore && cd /opt/semaphore
```
```bash
sudo sh -c 'umask 077; cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
SEMAPHORE_ADMIN_PASSWORD=$(openssl rand -hex 16)
SEMAPHORE_ACCESS_KEY_ENCRYPTION=$(head -c32 /dev/urandom | base64)
EOF'
```

`SEMAPHORE_ACCESS_KEY_ENCRYPTION` encrypts the SSH keys Semaphore stores. Back up `.env`:
lose it and every stored key has to be re-entered.

## 4. Write the compose file

```bash
sudo tee /opt/semaphore/docker-compose.yml > /dev/null <<'EOF'
services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: semaphore
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: semaphore
    volumes:
      - semaphore-postgres:/var/lib/postgresql/data

  semaphore:
    image: semaphoreui/semaphore:v2.19.17
    restart: unless-stopped
    depends_on:
      - postgres
    ports:
      - "3000:3000"
    environment:
      SEMAPHORE_DB_DIALECT: postgres
      SEMAPHORE_DB_HOST: postgres
      SEMAPHORE_DB_PORT: 5432
      SEMAPHORE_DB_USER: semaphore
      SEMAPHORE_DB_PASS: ${POSTGRES_PASSWORD}
      SEMAPHORE_DB: semaphore
      SEMAPHORE_PLAYBOOK_PATH: /tmp/semaphore/
      SEMAPHORE_ADMIN: admin
      SEMAPHORE_ADMIN_NAME: admin
      SEMAPHORE_ADMIN_EMAIL: admin@localhost
      SEMAPHORE_ADMIN_PASSWORD: ${SEMAPHORE_ADMIN_PASSWORD}
      SEMAPHORE_ACCESS_KEY_ENCRYPTION: ${SEMAPHORE_ACCESS_KEY_ENCRYPTION}
      TZ: UTC
    volumes:
      - semaphore-data:/var/lib/semaphore
      - semaphore-config:/etc/semaphore
      - semaphore-tmp:/tmp/semaphore

volumes:
  semaphore-postgres:
  semaphore-data:
  semaphore-config:
  semaphore-tmp:

networks:
  default:
    driver_opts:
      com.docker.network.driver.mtu: 1450
EOF
```

PostgreSQL has no published port: only Semaphore can reach it.

The `mtu: 1450` matches VM 152's `ens3`. OpenStack VMs use a smaller packet size than
Docker's default 1500; without this, HTTPS from the container (the `git clone` from GitHub)
hangs and fails with `Connection reset by peer`. Check the VM's value with
`ip -o link | awk '{print $2, $5}'`.

## 5. Start it and log in

```bash
cd /opt/semaphore && sudo docker compose up -d
```
```bash
sudo docker compose ps
```
```bash
sudo docker compose logs --tail 50 semaphore
```
```bash
sudo grep SEMAPHORE_ADMIN_PASSWORD /opt/semaphore/.env
```

Open `http://192.168.1.152:3000` and log in as `admin` with that password. If the page
does not load from another machine, check the firewall:

```bash
sudo ufw status
```

If it says `active`, allow the LAN only:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 3000 proto tcp
```

VM 152 is an OpenStack VM (private `10.0.0.92`, floating `192.168.1.152`), so its security
group also needs an ingress rule for TCP 3000 from `192.168.1.0/24`. Add it in Horizon under
Network → Security Groups, in the same group that already allows 8000 and 8082.

## 6. Make the SSH key Semaphore will use

On VM 152:

```bash
ssh-keygen -t ed25519 -N "" -C "semaphore@152" -f ~/semaphore_ed25519
```
```bash
cat ~/semaphore_ed25519.pub
```

Keep the `.pub` line for step 7. The private key stays on disk until it is in Semaphore
(step 8), then gets deleted.

## 7. Prepare each target VM (Linux)

Run on every VM Semaphore should reach. Start with 2 or 3. Replace the quoted key with the
`.pub` line from step 6.

```bash
sudo useradd -m -s /bin/bash iqdiag
```
```bash
sudo install -d -m 700 -o iqdiag -g iqdiag /home/iqdiag/.ssh
```
```bash
echo 'ssh-ed25519 AAAA...REPLACE... semaphore@152' | sudo tee /home/iqdiag/.ssh/authorized_keys > /dev/null
```
```bash
sudo chown iqdiag:iqdiag /home/iqdiag/.ssh/authorized_keys && sudo chmod 600 /home/iqdiag/.ssh/authorized_keys
```

`iqdiag` gets no password and no sudo. Diagnostic playbooks need neither. Check from VM 152:

```bash
ssh -i ~/semaphore_ed25519 iqdiag@<target-ip> uptime
```

## 8. Configure Semaphore (in the web UI)

1. **Project**: create `IQ Core diagnostics`. Only read-only diagnostic playbooks go in this
   project, because a token that can run one template can run every template in its project.
2. **Key Store**:
   - `iqdiag-ssh`: type SSH Key, username `iqdiag`, paste the private key from step 6.
   - `none`: type None. Used for the public repo.

   Once `iqdiag-ssh` is saved, delete the private key from VM 152:
   ```bash
   shred -u ~/semaphore_ed25519
   ```
3. **Repository**: `https://github.com/NikunjIQ/iq-playbooks.git`, branch `main`, key `none`.
4. **Inventory**: type Static, key `iqdiag-ssh`. List hosts by IP, because Zabbix host names
   have spaces and do not match anything else:
   ```ini
   [linux]
   192.168.1.31
   192.168.1.60
   ```
5. **Environment**: create `default` with extra variables `{}`. If the first run fails with
   `Host key verification failed`, add environment variable `ANSIBLE_HOST_KEY_CHECKING` = `False`
   (see bottleneck B9).
6. **Task template**: name `Top processes`, playbook `diag_top_processes.yml`, the
   inventory, repository and environment above. Turn on the **Limit** prompt, or the host
   IQ Core passes through the API is ignored.
7. **Test by hand**: run the template with Limit set to one IP. The output should end with
   the `Report` task showing uptime, memory and the top processes.

## 9. Give IQ Core its own user and token

1. In the UI, create user `iqcore` (not admin) and add it to `IQ Core diagnostics` with role
   **Task Runner**. It can start templates in that project and nothing else.
2. On VM 152, get a token for it. The password prompt keeps it out of shell history:

```bash
IFS= read -rs -p "iqcore password: " P; echo
```
```bash
curl -s -c /tmp/sem.cookie -H 'Content-Type: application/json' -d "{\"auth\":\"iqcore\",\"password\":\"$P\"}" http://localhost:3000/api/auth/login; unset P
```
```bash
curl -s -b /tmp/sem.cookie -X POST http://localhost:3000/api/user/tokens; rm -f /tmp/sem.cookie
```

The `id` in the reply is the token. It goes into IQ Core's `backend/.env` as `ANSIBLE_TOKEN`
(with `ANSIBLE_URL=http://localhost:3000`), and nowhere else.

## Adding a playbook

Playbooks live in `NikunjIQ/iq-playbooks`, separate from IQ Core's code. Teammates are
collaborators there; the rules they follow are in that repo's README
(https://github.com/NikunjIQ/iq-playbooks).

1. A teammate writes or uploads the playbook on GitHub: **Add file** → **Create new file**
   or **Upload files** → commit.
2. Someone reviews it against the README's rules.
3. Someone creates a **template** for it in the `IQ Core diagnostics` project in Semaphore,
   with the **Limit** prompt on. Until a template exists, IQ Core cannot run it.

Semaphore pulls `main` on every run, so an edit to an existing playbook is live on its next
run, with no template change.

## Day-to-day

Upgrade: change the image tag in `docker-compose.yml`, then:

```bash
cd /opt/semaphore && sudo docker compose pull && sudo docker compose up -d
```

Back up the database:

```bash
cd /opt/semaphore && sudo docker compose exec -T postgres pg_dump -U semaphore semaphore | gzip > ~/semaphore-$(date +%F).sql.gz
```

## The API IQ Core uses

| Call | Purpose |
|---|---|
| `POST /api/project/{id}/tasks` with `template_id`, `limit` | start a run on one host |
| `GET /api/project/{id}/tasks/{task_id}` | status: waiting, running, success, error |
| `GET /api/project/{id}/tasks/{task_id}/raw_output` | the output to paste into the ticket |

Header: `Authorization: Bearer <token>`.
