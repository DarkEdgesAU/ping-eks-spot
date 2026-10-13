variable "colour" {
  type = string
  validation {
    condition     = contains(["blue", "green"], var.colour) && var.colour == terraform.workspace
    error_message = "Use a separate blue or green workspace matching colour."
  }
}
variable "admin_url" {
  type = string
  validation {
    condition     = startswith(var.admin_url, "https://")
    error_message = "The candidate admin API must use HTTPS."
  }
}
variable "product_version" {
  type    = string
  default = "13.1"
}
variable "ca_certificate_file" { type = string }
variable "base_dn" { type = string }
variable "engine_hostname" { type = string }
variable "ldap_password" {
  type      = string
  sensitive = true
}
