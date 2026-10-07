# Infra Automation: Terraform + Podman + Ansible + Python

A small, fully local infrastructure automation project. It runs on Fedora with
**Podman only**. No Docker, no cloud account, no paid services.

| Week | Topic | Status |
|------|-------|--------|
| 1 | Terraform + Podman: two SSH-enabled Linux containers | Done |
| 2 | Ansible: users, Nginx, templates, verification | Planned |
| 3 | Python health-check CLI + unit tests | Planned |
| 4 | End-to-end automation script | Planned |

---

## Architecture

```
Fedora host
│
├── Terraform ──(HTTP over Unix socket)──► Podman API socket
│                                               │ creates / destroys
│                                               ▼
│                     Podman network "infra-net" 172.28.0.0/24
│                     ├── infra-web1  172.28.0.11  (systemd + sshd)
│                     └── infra-web2  172.28.0.12  (systemd + sshd)
│
│   127.0.0.1:2221 -> web1:22   127.0.0.1:2222 -> web2:22   (SSH)
│   127.0.0.1:8081 -> web1:80   127.0.0.1:8082 -> web2:80   (HTTP)
```

## Repository layout

```
infra-automation/
├── images/node/Containerfile   # server image: systemd, sshd, sudo, python3
├── terraform/
│   ├── versions.tf             # provider + Podman socket connection
│   ├── variables.tf            # inputs (servers map, ports, key paths)
│   ├── main.tf                 # network + containers (for_each)
│   └── outputs.tf              # connection details + Ansible inventory
├── .gitignore
└── README.md
```

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

## Week 1: Setup and run

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

## Sample output

> Replace this block with your real output from `terraform output`.

```
(paste terraform output here)
```

## Design decisions

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

## Challenges and troubleshooting

> Add the real issues you hit here. Likely candidates are below.

| Problem | Cause | Resolution |
|---------|-------|------------|
| Terraform cannot reach the Docker daemon | Podman socket not running or wrong path | `systemctl --user enable --now podman.socket`; set `TF_VAR_podman_socket` |
| `Permission denied (publickey)` | Key not injected, or user account locked | Check `/etc/ssh/keys/devops.pub` in the container; account is created with `-p '*'` so it is not locked |
| `Connection refused` right after apply | systemd still booting | Wait a few seconds; check `podman logs infra-web1` |
| `address already in use` | Host port taken | `ss -tlnp \| grep 2221`; change ports in `variables.tf` |
| Host key warnings after recreate | New container, new host key | `UserKnownHostsFile=/dev/null` in SSH options |

Debug from the bottom layer up: Podman, then the container, then SSH, then Terraform.

## Not committed

`terraform.tfstate*`, `.terraform/` and SSH keys are excluded via `.gitignore`.
`terraform/.terraform.lock.hcl` **is** committed to pin the provider version.
