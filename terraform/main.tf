resource "docker_network" "infra" {
  name   = "infra-net"
  driver = "bridge"

  ipam_config {
    subnet = var.network_subnet
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

  # Publish to localhost only (rootless: container IPs aren't reachable from host).
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

  # Inject the public key. 'upload' avoids SELinux bind-mount labelling problems on Fedora.
  upload {
    file    = "/etc/ssh/keys/${var.ssh_user}.pub"
    content = file(pathexpand(var.ssh_public_key_path))
  }
}