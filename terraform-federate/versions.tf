terraform {
  required_version = ">= 1.13.0, < 2.0.0"
  required_providers {
    pingfederate = { source = "pingidentity/pingfederate", version = "1.10.0" }
  }
}

# Credentials are supplied by scripts/federate_config.py as provider environment
# variables. Never disable TLS verification, including on local port forwards.
provider "pingfederate" {
  https_host               = var.admin_url
  product_version          = var.product_version
  ca_certificate_pem_files = [var.ca_certificate_file]
  insecure_trust_all_tls   = false
}
