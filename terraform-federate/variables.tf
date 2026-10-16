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
variable "public_base_url" {
  type    = string
  default = ""
  validation {
    condition     = var.public_base_url == "" || (startswith(var.public_base_url, "https://") && !endswith(var.public_base_url, "/"))
    error_message = "Use an HTTPS public base URL without a trailing slash."
  }
}
variable "ldap_password" {
  type      = string
  sensitive = true
}
