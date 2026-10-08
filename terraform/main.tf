resource "docker_network" "infra" {
  name   = "infra-net"
  driver = "bridge"

  ipam_config {
    subnet = var.network_subnet
  }

  # Podman fills in ipam defaults (driver, gateway) that otherwise
  # make Terraform want to recreate the network on every plan.
  lifecycle {
    ignore_changes = [ipam_options, ipam_config]
  }
}

resource "docker_container" "server" {
  for_each = var.servers

  name     = "infra-${each.key}"
  hostname = each.key
  image    = var.image_name
  restart  = "unless-stopped"
  must_run = true

  networks_advanced {
    name         = docker_network.infra.name
    ipv4_address = each.value.ip
  }

  ports {
    internal = 22
    external = each.value.ssh_port
    ip       = "127.0.0.1"
  }

  ports {
    internal = 80
    external = each.value.http_port
    ip       = "127.0.0.1"
  }

  upload {
    file    = "/etc/ssh/keys/${var.ssh_user}.pub"
    content = file(pathexpand(var.ssh_public_key_path))
  }

  # Podman reports pid_mode and ulimits that we never set in code.
  lifecycle {
    ignore_changes = [pid_mode, ulimit]
  }
}