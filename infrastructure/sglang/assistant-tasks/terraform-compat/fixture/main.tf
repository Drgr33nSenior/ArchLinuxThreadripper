terraform {
  required_version = ">= 1.5.0, < 1.8.0"
}

variable "services" {
  description = "Service name to listen port."
  type        = map(number)
  default = {
    api    = 8080
    worker = 9090
  }
}

locals {
  # Ports must be published in name order for the load-balancer template.
  ports = [for name in sort(keys(var.services)) : var.services[name]]
}

output "ports" {
  value = local.port
}

output "listener_names" {
  value = { for name, port in var.services : name => "${name}-${port}" }
}
