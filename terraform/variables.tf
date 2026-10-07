variable "podman_socket" {
  description = "Podman API socket URI (rootless)."
  type        = string
  default     = "unix:///run/user/1000/podman/podman.sock"
}

variable "image_name" {
  description = "Locally built server image."
  type        = string
  default     = "localhost/infra-node:latest"
}

variable "ssh_public_key_path" {
  description = "Public key injected into the servers."
  type        = string
  default     = "~/.ssh/infra_lab.pub"
}

variable "ssh_private_key_path" {
  description = "Private key path (only used to print connection details)."
  type        = string
  default     = "~/.ssh/infra_lab"
}

variable "ssh_user" {
  type    = string
  default = "devops"
}

variable "network_subnet" {
  type    = string
  default = "172.28.0.0/24"
}

variable "servers" {
  description = "Servers to create: name => settings."
  type = map(object({
    ip        = string
    ssh_port  = number
    http_port = number
  }))
  default = {
    web1 = { ip = "172.28.0.11", ssh_port = 2221, http_port = 8081 }
    web2 = { ip = "172.28.0.12", ssh_port = 2222, http_port = 8082 }
  }
}