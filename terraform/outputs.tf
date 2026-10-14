output "connection_details" {
  description = "Everything Ansible/Python/you need to reach each server."
  value = {
    for name, s in var.servers : name => {
      host         = "127.0.0.1"
      ssh_port     = s.ssh_port
      http_port    = s.http_port
      http_url     = "http://127.0.0.1:${s.http_port}"
      user         = var.ssh_user
      private_key  = pathexpand(var.ssh_private_key_path)
      container_ip = s.ip
      ssh_command  = "ssh -i ${pathexpand(var.ssh_private_key_path)} -p ${s.ssh_port} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${var.ssh_user}@127.0.0.1"
    }
  }
}

output "ansible_inventory" {
  description = "Ready-to-use Ansible INI inventory (used in Week 2)."
  value = join("\n", concat(
    ["[web]"],
    [for name, s in var.servers :
      "${name} ansible_host=127.0.0.1 ansible_port=${s.ssh_port} ansible_user=${var.ssh_user} ansible_ssh_private_key_file=${pathexpand(var.ssh_private_key_path)} http_port=${s.http_port}"
    ],
    ["", "[web:vars]", "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'"]
  ))
}