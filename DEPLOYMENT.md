# Deploying IQ Core

Step-by-step instructions for a deployment engineer: install IQ Core, connect it to one
or more Zammads so new Zabbix tickets get an AI suggestion, and add the Ansible layer so
those tickets also get live, read-only diagnostics from the affected server.

Work top to bottom. Every command goes in its own block; run them one at a time and
check the expected result before moving on. Parts 1 and 4 alone give you suggestions;
parts 2, 3 and 5 add Ansible diagnostics.

Related: every key and secret is listed in [ansible/Ansible_Keys.md](ansible/Ansible_Keys.md),
open problems in [ansible/Ansible_Bottlenecks.md](ansible/Ansible_Bottlenecks.md), and
how playbooks are written in the [iq-playbooks](https://github.com/NikunjIQ/iq-playbooks) README.

---

## The system at a glance

```
Zabbix alert ──► ticket in a Zammad ──► Zammad trigger ──► signed webhook ──► IQ Core
                                                                                │
         reads the ticket, picks a playbook for the alert, asks Semaphore ◄─────┤
                                                                                │
 Semaphore pulls the playbook from GitHub, Ansible SSHes to the host as iqdiag  │
                                                                                ▼
         IQ Core writes one internal note: AI suggestion + diagnostics ──► same Zammad
```

| Part | Where it runs (today) | Port | Service / path |
|---|---|---|---|
| IQ Core (API + web UI) | VM 152 (`user-test`) | 8000 | systemd `iqcore`, `/home/ubuntu/Agentic_IQ_Core` |
| IQ Core for a second Zammad (stopgap) | VM 152 | 8001 | systemd `iqcore-colleague`, settings in `/home/ubuntu/iqcore-colleague` |
| Semaphore (Ansible server + API) | VM 152 | 3000 | Docker Compose, `/opt/semaphore` |
| Playbooks | GitHub `NikunjIQ/iq-playbooks` (public) | – | – |
| Zammad (production) | VM 205 | 8082 | – |
| Zammad (colleague) | VM 183 | 8082 | – |
| Target servers | any Linux VM | 22 | user `iqdiag` |

**Rules this design keeps:** the AI never writes or picks commands; playbooks are
read-only and need no sudo; each run targets exactly one host from Semaphore's inventory;
every switch ships `off`, then `dry_run`, then live.

---

## 0. Before you start

Collect these first. You will paste them in later steps.

| You need | Example | Used in |
|---|---|---|
| A Linux VM for IQ Core and Semaphore (Ubuntu 24.04, 4 GB RAM free, 20 GB disk) | 192.168.1.152 | Parts 1–2 |
| `sudo` on that VM and on every target server | the team's `ubuntu` pem key | Parts 1–3 |
| A Groq API key | `gsk_…` (56 characters) | Part 1 |
| Admin login to each Zammad | – | Part 4 |
| Write access to the `Agentic_IQ_Core` and `iq-playbooks` GitHub repos | – | Parts 1–2 |
| Each VM's network MTU (`ip -o link`, the main interface) | 1450 on OpenStack | Part 2 |

Network: the Zammads must reach IQ Core's port; IQ Core must reach each Zammad and
Semaphore; Semaphore must reach GitHub (HTTPS) and every target server (SSH, port 22).
On OpenStack, security groups apply between VMs too: if a check below hangs, add an
ingress rule for that port.

---

## Part 1. IQ Core

**1.1 Install.**

```bash
cd ~ && git clone https://github.com/NikunjIQ/Agentic_IQ_Core.git && cd Agentic_IQ_Core
```

Use the `ansible` branch until it is merged into `main`, then `main`:

```bash
git switch ansible && git log --oneline -1
```
```bash
python3 -m venv venv && venv/bin/pip install -r backend/requirements.txt
```

**1.2 Settings file.**

```bash
cp backend/.env.example backend/.env && chmod 600 backend/.env
```

Leave everything else empty for now: Zammad and Ansible values are added in parts 4
and 5, and every switch in it defaults to `off`.

**1.3 Run it as a service.**

```bash
sudo tee /etc/systemd/system/iqcore.service > /dev/null <<'EOF'
[Unit]
Description=IQ Core (FastAPI backend and UI)
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/Agentic_IQ_Core/backend
ExecStart=/home/ubuntu/Agentic_IQ_Core/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now iqcore && sleep 3 && curl -s http://localhost:8000/health
```

Expect `{"status":"ok"}`.

**1.4 First login.** Open `http://<IQ Core IP>:8000` and create an account. **The first
account ever created is the admin.** On **Connections**, enter the **Groq API key**
(system setting) and each system's credentials, using **Test** before **Save**.

**Back up together:** `backend/iqcore.db`, `backend/secret.key` and
`backend/credentials.json`. The database's secrets cannot be read without the key.

---

## Part 2. Ansible server (Semaphore)

Semaphore is an open-source web UI and API around Ansible. It holds the SSH key, the
server list and the playbook templates; IQ Core only asks it to run a template on one host.

**2.1 Docker.** Check first; Ubuntu VMs here usually have it:

```bash
docker --version && docker compose version && systemctl is-active docker
```

Install only if missing:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 && sudo systemctl enable --now docker
```

**2.2 Secrets.** Generated on the server; never typed or pasted anywhere.

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

Back up `/opt/semaphore/.env` off the VM: without `SEMAPHORE_ACCESS_KEY_ENCRYPTION`,
every key stored in Semaphore must be re-entered.

**2.3 Compose file.** Set the `mtu` to the VM's main interface MTU (step 0). Leaving
Docker's default 1500 on a 1450 network makes HTTPS from the container hang and the
first run fail with `Connection reset by peer` while cloning from GitHub.

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
```bash
cd /opt/semaphore && sudo docker compose config --quiet && sudo docker compose up -d
```

The first start takes about a minute (database setup). Then:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000
```

Expect `200`. Check the container can reach GitHub (expect a line ending `refs/heads/main`):

```bash
sudo docker compose exec semaphore git ls-remote https://github.com/NikunjIQ/iq-playbooks.git
```

**2.4 Log in** at `http://<VM IP>:3000` as `admin`, with the password from:

```bash
sudo grep SEMAPHORE_ADMIN_PASSWORD /opt/semaphore/.env
```

Change it at once (user menu, bottom left).

**2.5 SSH key Semaphore uses to reach servers.**

```bash
ssh-keygen -t ed25519 -N "" -C "semaphore" -f ~/semaphore_ed25519
```
```bash
cat ~/semaphore_ed25519.pub
```

Keep the `.pub` line for Part 3. The private key goes into Semaphore in 2.6, then is deleted.

**2.6 Configure Semaphore (web UI).** Names matter: IQ Core looks templates up by name.

1. **Project:** `IQ Core diagnostics`. Only read-only diagnostic templates go here: a
   token that can run one template in a project can run all of them.
2. **Key Store → New Key**, twice:
   - `none`, type **None** (the playbook repo is public).
   - `iqdiag-ssh`, type **SSH Key**, username `iqdiag`, private key = the output of
     `cat ~/semaphore_ed25519` (include the BEGIN/END lines).
3. **Repositories → New:** name `iq-playbooks`, URL
   `https://github.com/NikunjIQ/iq-playbooks.git`, branch `main`, key `none`.
4. **Inventory → New:** name `linux`, user credentials `iqdiag-ssh`, sudo credentials
   empty, type **Static**. Content (hosts by IP; add each server in Part 3):
   ```ini
   [linux]
   ```
5. **Environment** (or **Variable Groups**) **→ New:** name `default`, extra variables
   `{}`, environment variable `ANSIBLE_HOST_KEY_CHECKING` = `False` (Semaphore's container
   has never seen the servers' host keys; see bottleneck B9).
6. **Task Templates → New → Ansible Playbook:** name **`Top processes`**, playbook
   `diag_top_processes.yml`, inventory `linux`, repository `iq-playbooks`, environment
   `default`, and turn on the **Limit** prompt. Without it, the host IQ Core asks for is
   ignored.

Once `iqdiag-ssh` is saved:

```bash
shred -u ~/semaphore_ed25519
```

**2.7 IQ Core's own Semaphore user.**

1. Admin menu → **Users → New**: username `iqcore`, a password of **letters and numbers
   only** (it travels inside JSON), **not** admin. Save the password in a password manager.
2. Project `IQ Core diagnostics` → **Team** → add `iqcore` with role **Task Runner**.

Semaphore's UI has no button for API tokens. This creates one and writes it straight
into IQ Core's `.env`, so nobody copies it:

```bash
IFS= read -rs -p "iqcore password: " P; echo
```
```bash
curl -s -o /dev/null -w "login: %{http_code}\n" -c /tmp/sem.cookie -H 'Content-Type: application/json' -d "{\"auth\":\"iqcore\",\"password\":\"$P\"}" http://localhost:3000/api/auth/login; unset P
```

Expect `login: 204`. Then:

```bash
T=$(curl -s -b /tmp/sem.cookie -X POST http://localhost:3000/api/user/tokens | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])") && printf '\nANSIBLE_MODE=off\nANSIBLE_URL=http://localhost:3000\nANSIBLE_PROJECT_ID=1\nANSIBLE_TOKEN=%s\n' "$T" >> ~/Agentic_IQ_Core/backend/.env && echo "saved, length ${#T}"; unset T; rm -f /tmp/sem.cookie
```

Expect `saved, length 44`.

---

## Part 3. Each target server

Do this on every Linux server Ansible should diagnose. Nothing else is installed on it.

**3.1 On the target server**, create the `iqdiag` user (no password, no sudo) with
Semaphore's public key from 2.5:

```bash
sudo useradd -m -s /bin/bash iqdiag
```
```bash
sudo install -d -m 700 -o iqdiag -g iqdiag /home/iqdiag/.ssh
```
```bash
echo 'ssh-ed25519 AAAA...PASTE THE .pub LINE... semaphore' | sudo tee /home/iqdiag/.ssh/authorized_keys > /dev/null
```
```bash
sudo chown iqdiag:iqdiag /home/iqdiag/.ssh/authorized_keys && sudo chmod 600 /home/iqdiag/.ssh/authorized_keys
```

**3.2 In Semaphore**, add the server's IP as a new line under `[linux]` in the `linux`
inventory. Use the exact name Zabbix shows as the ticket's `Host:`. If Zabbix uses a
one-word name instead of the IP, add `thatname ansible_host=<IP>`. Names with spaces
cannot be used.

**3.3 Test by hand** in Semaphore: `Top processes` → **Run** → Limit = the IP. It should
end with a `Report` task (load, memory, top processes) and `changed=0`.

---

## Part 4. Connect a Zammad (suggestions)

**4.1 In Zammad (admin):**

1. **Users → New** (`/#manage/users`): `IQ Core Assist`, a unique email, role **Agent**,
   and under **Group permissions** tick **READ** and **CHANGE** on every group Zabbix
   tickets land in. Without them every ticket fails with `403` and IQ Core logs "ticket
   not found". (If the ticket screen has no Group field, the Zammad has one group,
   usually `Users`.)
2. **System → API** (`/#system/api`): turn on **API Token Access**.
3. In a **private window**, log in as IQ Core Assist → avatar → **Profile → Token
   Access → Create**: name `IQ Core ticket assist`, permission **`ticket.agent`**. Copy
   the token (shown once).

**4.2 On the IQ Core VM**, add this Zammad's values to `backend/.env`. You are asked for
the URL and token; a webhook secret is generated and printed for 4.3.

```bash
cd ~/Agentic_IQ_Core/backend && read -p "Zammad URL, with http:// (e.g. http://192.168.1.205:8082): " URL; IFS= read -rs -p "Zammad token: " T; echo; S=$(openssl rand -hex 32); printf '\nZAMMAD_URL=%s\nZAMMAD_WRITE_TOKEN=%s\nZAMMAD_WEBHOOK_SECRET=%s\nTICKET_ASSIST_MODE=dry_run\nTICKET_ASSIST_DAILY_LIMIT=40\n' "$URL" "$T" "$S" >> .env; echo "Webhook secret for Zammad: $S"; unset T S URL
```

The URL **must** start with `http://`: `curl` works without it, IQ Core does not.
The appended lines override the empty ones copied from `.env.example`: when a key
appears twice, the last line wins.

```bash
sudo systemctl restart iqcore
```

Check IQ Core can read Zammad as IQ Core Assist (expect `True IQ Core Assist`):

```bash
cd ~/Agentic_IQ_Core/backend && ../venv/bin/python -c "from dotenv import load_dotenv; load_dotenv('.env'); import os,requests; r=requests.get(os.environ['ZAMMAD_URL']+'/api/v1/users/me', headers={'Authorization':'Token token='+os.environ['ZAMMAD_WRITE_TOKEN']}, timeout=10); print(r.ok, r.json().get('firstname'), r.json().get('lastname'))"
```

**4.3 In Zammad: webhook and trigger.**

1. **Webhook** (`/#manage/webhook`) → New: name `IQ Core ticket assist`, endpoint
   `http://<IQ Core IP>:8000/hooks/zammad/ticket-created`, **HMAC SHA1 signature token** =
   the secret from 4.2, custom payload **off**, active.
2. **Trigger** (`/#manage/trigger`) → New: name `IQ Core: suggest fix on new ticket`,
   activated by **Action**, conditions **Action is created** and **Tags contains one
   not** `iqcore-suggested`, execute **Webhook → IQ Core ticket assist**, active.

**4.4 Check the Zammad's existing triggers (with its owner).** IQ Core's note arrives
about 90 seconds after a ticket is created. Zammad counts it as an update, so any
trigger that fires on **article created** or **ticket updated** and sends email also
fires on it. One that emails the customer could send them the internal AI note. Add
the condition **Article visibility is public** to such triggers. On 205 this was done
to "2 New Article Created". The 90-second delay already avoids triggers on "created
within the last minute".

**4.5 Test (dry run).** In Zammad: **+ → New Ticket → Received Call** (not Send Email),
customer = yourself, tags empty, title `TEST - Load average is too high`, text:

```text
Problem started at 12:00:00 on 2026.10.30
Problem name: Linux: Load average is too high (per CPU load over 1.5 for 5m)
Host: 192.168.1.163
Severity: Average
Operational data: 3.2
Original problem ID: 9990001
```

`Original problem ID` marks it as a Zabbix ticket; `Problem name` picks the playbook;
`Host` is the server. Then on the IQ Core VM:

```bash
tail -n 1 ~/Agentic_IQ_Core/backend/ticket_assist.jsonl | python3 -c "import json,sys; r=json.loads(sys.stdin.read()); s=r.pop('suggestion',''); print(json.dumps(r, indent=2)); print(s[-600:])"
```

Expect `"outcome": "dry_run"` and `"host": "192.168.1.163"`. Nothing was written to the ticket.

**4.6 Go live.**

```bash
sed -i 's/^TICKET_ASSIST_MODE=.*/TICKET_ASSIST_MODE=write/' ~/Agentic_IQ_Core/backend/.env && sudo systemctl restart iqcore
```

Create another test ticket (new `Original problem ID`). About 90 seconds later it gets
an internal note. Open the ticket → **⋯ → History** and check no unexpected trigger fired.

### 4b. A second Zammad (current method)

IQ Core talks to one Zammad per running service, so each extra Zammad gets its own
service on the next port, with its settings **outside the repo** so tokens can never be
committed. This is a stopgap until IQ Core manages several Zammads from its admin page.

1. Do **4.1** on the new Zammad.
2. On the IQ Core VM:

```bash
mkdir -p ~/iqcore-colleague && chmod 700 ~/iqcore-colleague && cd ~/iqcore-colleague
```
```bash
read -p "Zammad URL, with http://: " URL; IFS= read -rs -p "Zammad token: " T; echo; S=$(openssl rand -hex 32); umask 077; printf 'ZAMMAD_URL=%s\nZAMMAD_WRITE_TOKEN=%s\nZAMMAD_WEBHOOK_SECRET=%s\nTICKET_ASSIST_MODE=dry_run\nTICKET_ASSIST_DAILY_LIMIT=40\nTICKET_ASSIST_LOG=/home/ubuntu/iqcore-colleague/ticket_assist.jsonl\nIQCORE_CONFIG_PATH=/home/ubuntu/iqcore-colleague/credentials.json\nANSIBLE_MODE=dry_run\n' "$URL" "$T" "$S" > env; echo "Webhook secret for Zammad: $S"; unset T S URL
```

`IQCORE_CONFIG_PATH` points away from the main `credentials.json` on purpose: settings
saved there (which can include the main Zammad's URL) are applied at startup and would
override this service's. So the Groq key
must be copied in (expect `copied, length 56`):

```bash
python3 -c "import json; k=json.load(open('/home/ubuntu/Agentic_IQ_Core/backend/credentials.json')).get('GROQ_API_KEY',''); assert k, 'no GROQ_API_KEY'; open('/home/ubuntu/iqcore-colleague/env','a').write('GROQ_API_KEY='+k+'\n'); print('copied, length', len(k))"
```

```bash
sudo tee /etc/systemd/system/iqcore-colleague.service > /dev/null <<'EOF'
[Unit]
Description=IQ Core ticket assist for a second Zammad
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/Agentic_IQ_Core/backend
EnvironmentFile=/home/ubuntu/iqcore-colleague/env
ExecStart=/home/ubuntu/Agentic_IQ_Core/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8001
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now iqcore-colleague && sleep 3 && curl -s http://localhost:8001/health
```

3. From the **Zammad's VM**, check `curl -s http://<IQ Core IP>:8001/health` returns
   `{"status":"ok"}`.
4. Do **4.3–4.6** with port **8001**, the log at `~/iqcore-colleague/ticket_assist.jsonl`,
   settings in `~/iqcore-colleague/env`, and service `iqcore-colleague`.

Ansible settings (`ANSIBLE_URL`, `ANSIBLE_TOKEN`) are read from the main `backend/.env`;
only the switch lives in the second service's `env`.

### 4c. Any number of Zammads, one service (replaces 4b)

IQ Core stores each Zammad as a **connection** in its database (token and webhook
secret encrypted), with its own webhook path, mode, daily limit and Ansible switch.
Do **4.1** in the Zammad first.

**On the page (recommended).** Sign in to IQ Core as an admin → **Ticket assist** tab:

1. **Add a Zammad:** name (e.g. `zammad183`), Zammad URL with `http://`, IQ Core
   Assist's token, Suggestions **Dry run**, daily limit, and tick **Allow Ansible
   diagnostics** if its tickets may use Ansible. **Save**.
2. A **"Connect … in Zammad"** box appears with the **Endpoint** and **Signature token**
   (copy buttons) and the webhook/trigger steps. **Copy the token now**: it is not shown
   again. If the endpoint shows an address Zammad cannot reach (e.g. `localhost`), use
   the IQ Core VM's IP with the same path.
3. **Test** on the row signs in to that Zammad as IQ Core Assist and shows the result.
4. Do **4.3** in the Zammad with that endpoint and token, then test with **4.5** (the log
   line carries `"zammad": "<name>"`).
5. **Edit** → Suggestions **Write** → **Save** to go live. A blank token keeps the stored
   one; **Issue a new webhook secret** gives a fresh one (update the Zammad webhook after).
   **Delete** removes it; that Zammad's webhook is then refused.

**Or through the API**, from the IQ Core VM. Sign in as an IQ Core admin (expect a reply containing `"ok":true`):

```bash
read -p "IQ Core admin username: " U; IFS= read -rs -p "Password: " P; echo; curl -s -c /tmp/iq.cookie -H 'Content-Type: application/json' -d "{\"username\":\"$U\",\"password\":\"$P\"}" http://localhost:8000/auth/login; echo; unset P
```

Add the connection. The reply shows its `webhook_path` and, **this once**, its
`webhook_secret`:

```bash
read -p "Connection name (e.g. zammad183): " N; read -p "Zammad URL, with http://: " URL; IFS= read -rs -p "Zammad token: " T; echo; curl -s -b /tmp/iq.cookie -X PUT -H 'Content-Type: application/json' -d "{\"url\":\"$URL\",\"write_token\":\"$T\",\"assist_mode\":\"dry_run\",\"daily_limit\":40,\"ansible_enabled\":true}" "http://localhost:8000/admin/zammads/$N"; echo; unset T
```

Check IQ Core can sign in to it (expect `{"ok":true,"user":"IQ Core Assist"}`):

```bash
curl -s -b /tmp/iq.cookie -X POST "http://localhost:8000/admin/zammads/$N/test"; echo
```

In the Zammad, do **4.3** with endpoint `http://<IQ Core IP>:8000` + the `webhook_path`
and the `webhook_secret` from the reply. Test with **4.5**: the log line carries
`"zammad": "<name>"`. Go live by repeating the PUT with `"assist_mode":"write"` and an
empty `"write_token":""` (blank keeps the stored token). Other calls:

| Need | Call |
|---|---|
| List connections (never shows tokens or secrets) | `curl -s -b /tmp/iq.cookie http://localhost:8000/admin/zammads` |
| New webhook secret | the PUT with `"new_secret":true`, then update the Zammad webhook |
| Remove | `curl -s -b /tmp/iq.cookie -X DELETE http://localhost:8000/admin/zammads/<name>` |

`ANSIBLE_MODE` in `backend/.env` stays the master switch for Ansible; a connection's
`ansible_enabled` decides whether its tickets may use it.

**Moving a 4b Zammad onto a connection:** add it as above, point its Zammad webhook at
the new endpoint and secret, send a test ticket, then retire the extra service:

```bash
sudo systemctl disable --now iqcore-colleague && sudo rm /etc/systemd/system/iqcore-colleague.service && sudo systemctl daemon-reload && rm -rf ~/iqcore-colleague
```

Clean up the session file when done: `rm -f /tmp/iq.cookie`.

---

## Part 5. Turn on Ansible diagnostics

Needs parts 2–4. When a ticket's alert matches a playbook (the table `PLAYBOOK_FOR_ALERT`
in `backend/tools/ansible_tool.py`) and its host is in the inventory, IQ Core runs the
template on that host and adds the report to the note. Anything else gets the suggestion only.

**5.1 Check IQ Core can see the template and inventory** without running anything
(expect `{'playbook': 'Top processes', 'host': '<IP>', 'status': 'dry_run'}`):

```bash
cd ~/Agentic_IQ_Core/backend && ../venv/bin/python -c "from dotenv import load_dotenv; load_dotenv('.env'); from tools.ansible_tool import run_ansible; print(run_ansible('Top processes', '<a server IP from the inventory>', dry_run=True))"
```

`401` = bad token (redo 2.7). `403` = `iqcore` lacks a role in the project.
"not in the inventory" = Part 3.2 not done for that IP.

**5.2 Dry run, then live.** Set `ANSIBLE_MODE=dry_run` in `backend/.env` (and in
`~/iqcore-colleague/env` for a second Zammad), restart, send a test ticket (4.5), and
check the log shows `"ansible": {... "status": "dry_run"}`. Then:

```bash
sed -i 's/^ANSIBLE_MODE=.*/ANSIBLE_MODE=run/' ~/Agentic_IQ_Core/backend/.env && sudo systemctl restart iqcore
```

A test ticket should now get a note ending with
**"IQ Core diagnostics: 'Top processes' on <IP>, run read-only via Ansible (success)"**
and the report, and Semaphore shows a new task.

---

## Day-to-day

| Task | How |
|---|---|
| **Update IQ Core** | `cd ~/Agentic_IQ_Core && git pull`, then restart **every** IQ Core service: `sudo systemctl restart iqcore iqcore-colleague`. A running service keeps the old code until restarted. |
| **Change a setting** | Edit `backend/.env` (or `~/iqcore-colleague/env`), then restart that service. |
| **Turn a feature off** | `TICKET_ASSIST_MODE=off` or `ANSIBLE_MODE=off`, restart. Or deactivate the trigger in Zammad. |
| **Add a server** | Part 3. |
| **Add a playbook** | A teammate adds `diag_<what>.yml` to `iq-playbooks` by pull request (rules in its README) → a Semaphore admin creates a template in `IQ Core diagnostics` with the Limit prompt on → a developer maps the alert to the template name in `PLAYBOOK_FOR_ALERT` and deploys. |
| **Upgrade Semaphore** | Change the image tag in `/opt/semaphore/docker-compose.yml`, then `cd /opt/semaphore && sudo docker compose pull && sudo docker compose up -d`. |
| **Back up Semaphore** | `cd /opt/semaphore && sudo docker compose exec -T postgres pg_dump -U semaphore semaphore \| gzip > ~/semaphore-$(date +%F).sql.gz`, plus `/opt/semaphore/.env`. |
| **Back up IQ Core** | `backend/iqcore.db`, `backend/secret.key`, `backend/credentials.json`, `backend/.env`, and `~/iqcore-colleague/env`. |
| **Watch it work** | `tail -f ~/Agentic_IQ_Core/backend/ticket_assist.jsonl` and `journalctl -u iqcore -f`. |

---

## Troubleshooting

Every row below happened during the first deployment.

| Symptom | Cause | Fix |
|---|---|---|
| Semaphore run hangs, then `Connection reset by peer` cloning from GitHub | Docker network MTU 1500 on a 1450 network | `mtu` in the compose file (2.3), then `docker compose down && up -d` |
| `curl http://localhost:3000` gives `000` right after start | Database still initialising | Wait a minute |
| Semaphore login via API gives `401` | Wrong user/password, or a `"` or `\` in the password | Reset to letters and numbers only (2.7) |
| IQ Core → Semaphore `401` | The saved token is not a token (e.g. pasted JSON) | Redo the token command in 2.7 and replace the `ANSIBLE_TOKEN` line |
| Log: `"reason": "ticket not found"` | IQ Core Assist has no group access (Zammad answers `403`) | READ + CHANGE on the group (4.1) |
| Log: `ValueError: not a single host: '192.168.1.163</div>…'` | Old code with an HTML ticket body | Pull and **restart** (fixed in the `ansible` branch) |
| Log: `"ansible": {... "error": "… not in the inventory"}` | Ticket's host is not in Semaphore's inventory, or is a name with spaces | Part 3.2 |
| Note has raw `TASK […]` lines instead of the report | Old code read Semaphore's output before it was fully stored | Pull and **restart** |
| No log line at all | Zammad never called, or the call was refused | `journalctl -u iqcore --since "10 min ago" \| grep hooks`: nothing = check trigger/webhook active; `401` = secret mismatch; `400` = custom payload is on |
| Log line appears only ~2 minutes after the ticket | Normal in write mode: it waits until the ticket is 90 s old, runs Ansible, then logs | – |
| Customer gets an email about the AI note | A Zammad trigger fires on all new articles | 4.4 |
| "Delivery failed … XOAUTH2 … invalid_grant" in Zammad | Zammad's Microsoft 365 email channel needs reconnecting | Zammad admin → Channels → Email; unrelated to IQ Core |
| A setting change has no effect | Service not restarted, or the shell had sourced the env file | Restart from a fresh shell |
