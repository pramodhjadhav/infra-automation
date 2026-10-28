# Infra Automation: Terraform + Podman + Ansible + Python

A small, fully local infrastructure automation project. It runs on Fedora with
**Podman only**. No Docker, no cloud account, no paid services.

| Week | Topic | Status |
|------|-------|--------|
| 1 | Terraform + Podman: two SSH-enabled Linux containers | ✅ Done |
| 2 | Ansible: users, Nginx, templates, verification | ✅ Done |
| 3 | Python health-check CLI + unit tests | ✅ Done |
| 4 | End-to-end automation script | ✅ Done |

---

## Architecture

```
Fedora host
│
├── Terraform ──(HTTP over Unix socket)──► Podman API socket
│                                               │ creates / destroys
│                                               ▼
│                     Podman network "infra-net" 172.28.0.0/24
│                     ├── infra-web1  172.28.0.11  (systemd + sshd + nginx)
│                     └── infra-web2  172.28.0.12  (systemd + sshd + nginx)
│
│   127.0.0.1:2221 -> web1:22   127.0.0.1:2222 -> web2:22   (SSH)
│   127.0.0.1:8081 -> web1:80   127.0.0.1:8082 -> web2:80   (HTTP)
│
└── Python health-check CLI ──► reads both containers via SSH + HTTP
```

## Repository layout

```
infra-automation/
├── images/node/Containerfile      # server image: systemd, sshd, sudo, python3
├── terraform/
│   ├── versions.tf                # provider + Podman socket connection
│   ├── variables.tf               # inputs (servers map, ports, key paths)
│   ├── main.tf                    # network + containers (for_each)
│   └── outputs.tf                 # connection details + Ansible inventory
├── ansible/
│   ├── ansible.cfg
│   ├── inventory.ini              # generated from terraform output
│   ├── site.yml                   # main playbook
│   ├── group_vars/web.yml
│   └── roles/
│       ├── common/                # users, SSH keys, sudo
│       ├── nginx/                 # install, config templates, enable
│       └── verify/                # service + HTTP endpoint checks
├── python/                        # Week 3 – health-check CLI
│   ├── health_check.py            # CLI health-check application
│   ├── requirements.txt           # pinned dependencies
│   ├── servers.yml                # optional config override for --config flag
│   └── tests/
│       └── test_health_check.py   # 16 unit tests (unittest.mock)
├── run_all.sh                     # Week 4 – end-to-end automation script
├── docs/
│   ├── week2-playbook-run.txt     # sample Ansible run output
│   ├── week2-web1-page.html
│   └── week2-web2-page.html
├── .gitignore
└── README.md
```

---

## Prerequisites (Fedora)

```bash
sudo dnf install -y podman git openssh-clients curl jq
podman info --format '{{.Host.Security.Rootless}}'    # expect: true

# Podman API socket (rootless)
systemctl --user enable --now podman.socket
curl --unix-socket $XDG_RUNTIME_DIR/podman/podman.sock http://d/_ping   # expect: OK
```

Terraform (>= 1.5) must be installed. See https://developer.hashicorp.com/terraform/install

Create an SSH key used only for this lab:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/infra_lab -N "" -C "infra-lab"
```

---

## Week 1: Terraform + Podman

> **Status: ✅ Done**

### Objective

Build two Linux server containers using Podman, provision them through Terraform,
configure networking and SSH access, and expose connection details through Terraform outputs.

### Run it

**1. Build the server image**

```bash
podman build -t localhost/infra-node:latest images/node/
```

**2. Provision with Terraform**

```bash
cd terraform
export TF_VAR_podman_socket="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
terraform init
terraform validate
terraform plan
terraform apply
```

**3. Inspect the outputs**

```bash
terraform output
terraform output -json connection_details | jq .
terraform output -raw ansible_inventory
```

**4. Test SSH** (wait about 5 seconds after apply for systemd to boot)

```bash
ssh -i ~/.ssh/infra_lab -p 2221 -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null devops@127.0.0.1
ssh -i ~/.ssh/infra_lab -p 2222 -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null devops@127.0.0.1
```

**5. Verify Terraform manages the servers**

```bash
terraform plan            # "No changes."
podman rm -f infra-web1   # simulate drift
terraform plan            # detects web1 is missing
terraform apply           # recreates it
```

**6. Tear down**

```bash
terraform destroy
```

### Design decisions (Week 1)

- **Docker provider against Podman's socket.** Terraform has no official Podman
  provider. Podman exposes a Docker-compatible API, so the `kreuzwerker/docker`
  provider works unchanged when pointed at Podman's socket. No Docker daemon is involved.
- **Rootless Podman.** Safer (no root needed), but container IPs are not reachable
  from the host, so ports are published on `127.0.0.1`. Ports are above 1024
  because rootless users cannot bind lower ones.
- **systemd inside the containers.** Makes service management (`systemctl status nginx`)
  realistic for the Ansible and health-check weeks.
- **Key-only SSH.** Password login and root login are disabled. The public key is
  injected by Terraform.
- **`upload` instead of a bind mount for the key.** Avoids SELinux labelling problems on Fedora.
- **`for_each` over a `servers` map.** Adding a server means adding one map entry,
  not copying a resource block.
- **Image built outside Terraform.** Image problems surface before Terraform is
  involved, which keeps debugging simple.

### Challenges (Week 1)

| Problem | Cause | Resolution |
|---------|-------|------------|
| Terraform cannot reach the Docker daemon | Podman socket not running or wrong path | `systemctl --user enable --now podman.socket`; set `TF_VAR_podman_socket` |
| `Permission denied (publickey)` | Key not injected, or user account locked | Check `/etc/ssh/keys/devops.pub` in the container; account is created with `-p '*'` so it is not locked |
| `Connection refused` right after apply | systemd still booting | Wait a few seconds; check `podman logs infra-web1` |
| `address already in use` | Host port taken | `ss -tlnp \| grep 2221`; change ports in `variables.tf` |
| Host key warnings after recreate | New container, new host key | `UserKnownHostsFile=/dev/null` in SSH options |
| Perpetual diff in `terraform plan` | Podman reports `pid_mode` and `ulimits` defaults not in config | Added `lifecycle { ignore_changes = [pid_mode, ulimit] }` |

---

## Week 2: Ansible Configuration

> **Status: ✅ Done**

### Objective

Use Ansible to connect to both servers and automate their configuration — create
users, install/configure Nginx, deploy configuration using templates, and verify
the service and HTTP endpoint.

### Run it

```bash
# Generate inventory from Terraform output
terraform -chdir=terraform output -raw ansible_inventory > ansible/inventory.ini

cd ansible
ansible all -m ping
ansible-playbook site.yml
ansible-playbook site.yml    # second run: changed=0  (idempotent)
```

### Roles

| Role | What it does |
|------|--------------|
| `common` | Creates `appadmin` and `monitor` users, installs their SSH keys, grants passwordless sudo to `appadmin` |
| `nginx` | Installs Nginx, applies a systemd drop-in fix, deploys `nginx.conf` and the home page from Jinja2 templates, enables and starts the service |
| `verify` | Asserts `systemctl is-active nginx`, checks the home page from inside the server and through the published host port, checks the `/health` endpoint |

### Sample run

See [`docs/week2-playbook-run.txt`](docs/week2-playbook-run.txt) — both servers
complete with `ok=17 changed=0` on the second run.

### Design decisions (Week 2)

- **Inventory generated by Terraform.** Servers are defined once in `variables.tf`;
  Ansible derives its target list from `terraform output`, eliminating duplication.
- **Roles, not a flat playbook.** Each concern (users, web server, verification)
  is isolated, independently tagged, and reusable.
- **Jinja2 templates for Nginx config and home page.** The same role works for
  both servers; the hostname is injected at deploy time.
- **verify role runs at the end of the playbook.** Behaves like a smoke test:
  the playbook fails fast if Nginx is misconfigured.

### Challenges (Week 2)

- Running Ansible from the wrong folder: `inventory.ini` is a relative path, so commands must run from `ansible/`
- `sshd` reads keys from `/etc/ssh/keys/%u.pub` (set in the Containerfile), so the role installs keys there
- Nginx in a rootless container needed a systemd drop-in (`PrivateTmp=false`)
- With Nginx stopped, the published port answers with "connection reset" instead of "refused"

---

## Week 3: Python Health-Check CLI

> **Status: ✅ Done**

### Objective

Develop a Python command-line application to monitor both servers. It checks
connectivity, CPU, memory, disk, Nginx/service status, and HTTP availability.
Includes structured logging, error handling, and unit tests.

### Structure

```
python/
├── health_check.py          # CLI entry-point (argparse)
├── requirements.txt         # paramiko, requests, tabulate, pytest
├── servers.yml              # optional server config for --config flag
└── tests/
    └── test_health_check.py # 16 unit tests using unittest.mock
```

### Checks performed per server

| Check | Method | Pass condition |
|-------|--------|----------------|
| SSH connectivity | `paramiko` connect with key auth | Connection established |
| CPU usage | `top -bn1` over SSH | < 90 % |
| Memory usage | `free -m` over SSH | < 90 % used |
| Disk usage | `df -h /` over SSH | < 90 % used |
| Nginx service status | `systemctl is-active nginx` over SSH | stdout == `active` |
| HTTP home page | `requests.get` to host port | HTTP 200 |
| HTTP `/health` endpoint | `requests.get` to host port | HTTP 200, body == `ok` |

### Run it

```bash
cd python
source .venv/bin/activate              # activate the virtualenv
python health_check.py                 # check both servers, print table
python health_check.py --server web1   # single server
python health_check.py --output json   # JSON report
python health_check.py --config servers.yml  # use YAML config instead of defaults
pytest tests/ -v                       # run 16 unit tests
```

### Sample output

```
Infra Health Report  —  2025-07-08 10:42:17
================================================================

Server  SSH        CPU    MEM    DISK   NGINX   HTTP  /health  Overall     Time(s)
------  ---------  -----  -----  -----  ------  ----  -------  ----------  -------
web1    connected  12.4%  33.8%  18%    active  200   ok       HEALTHY     1.23s
web2    connected  9.1%   31.2%  18%    active  200   ok       HEALTHY     1.19s

All servers healthy.
```

### Design decisions (Week 3)

- **`paramiko` for SSH** — gives programmatic access to run commands over SSH without shelling out to `ssh`. Uses key-only auth (`allow_agent=False`, `look_for_keys=False`).
- **`AutoAddPolicy`** — mirrors `-o StrictHostKeyChecking=no` used by SSH and Ansible; necessary because ephemeral containers get a new host key when recreated.
- **`SSHSession` context manager** — guarantees the connection is always closed even if a check raises an exception (`__exit__` calls `close()`).
- **One function per check** — each returns a `CheckResult` dataclass and never raises; failures are returned as `STATUS_FAIL`, not unhandled exceptions.
- **HTTP checks are independent of SSH** — even if SSH is broken, the HTTP port may still answer; both are always tested.
- **Three overall states: HEALTHY / DEGRADED / UNREACHABLE** — `UNREACHABLE` means SSH itself failed; `DEGRADED` means SSH worked but at least one metric failed.
- **Structured JSON logging to file** — machine-readable and parseable by log collectors; human-readable format on the console.
- **`--config servers.yml`** — decouples the server list from the code so the same script works for any environment without editing Python.

### Challenges (Week 3)

- `RejectPolicy` caused "not found in known_hosts" errors because the containers get a new host key on each recreate → changed to `AutoAddPolicy`.
- `top` output format varies between Linux distributions; used regex on the idle% field (`\d+\.\d+\s+id`) instead of positional parsing.

---

## Week 4: End-to-End Integration

> **Status: ✅ Done**

### Objective

A single script (`run_all.sh`) integrates all four tools — Podman, Terraform,
Ansible, and Python — into one automated workflow that can be run from a clean
state.

### Run it

```bash
chmod +x run_all.sh
./run_all.sh                   # full workflow
./run_all.sh --skip-build      # skip image build (image already exists)
./run_all.sh --skip-tests      # skip unit tests
./run_all.sh --destroy         # tear down infrastructure at the end
```

### What the script does (step by step)

| Step | Tool | What happens |
|------|------|-------------|
| Pre-flight | bash | Checks `podman`, `terraform`, `ansible-playbook`, `python3` exist; verifies the Podman socket and SSH key are present |
| 1 | Podman | Builds `localhost/infra-node:latest` from `images/node/Containerfile` — skipped if image already exists |
| 2 | Terraform | Runs `terraform init` + `terraform apply -auto-approve`; exports `TF_VAR_podman_socket` automatically |
| 3 | Terraform + Ansible | Runs `terraform output -raw ansible_inventory` to regenerate `ansible/inventory.ini`; pings both hosts to confirm reachability |
| 4 | Ansible | Runs `ansible-playbook site.yml` — creates users, configures Nginx, verifies service |
| 5 | Python / pytest | Activates the venv and runs `pytest tests/ -v` — 16 unit tests, no live infra needed |
| 6 | Python | Runs `python health_check.py` — live SSH + HTTP checks; exits `0` if all healthy |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All steps passed and both servers are healthy |
| `1` | Any step failed (detailed reason printed before exit) |

---

## Not committed

`terraform.tfstate*`, `.terraform/` (except `.terraform.lock.hcl`) and SSH keys
are excluded via `.gitignore`.
`terraform/.terraform.lock.hcl` **is** committed to pin the provider version.
