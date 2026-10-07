terraform {
  required_version = ">= 1.5.0"

  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
  }
}

# Despite the name, this provider is talking to PODMAN's socket.
provider "docker" {
  host = var.podman_socket
}