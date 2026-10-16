resource "pingfederate_server_settings" "identity" {
  federation_info = {
    base_url          = var.public_base_url != "" ? var.public_base_url : "https://${var.engine_hostname}"
    saml_2_entity_id  = "https://${var.engine_hostname}"
    saml_1x_issuer_id = var.engine_hostname
  }
}
resource "pingfederate_server_settings_general" "identity" {
  datastore_validation_interval_secs      = 300
  disable_automatic_connection_validation = false
}
resource "pingfederate_certificate_ca" "internal" {
  ca_id     = "50z1hm968rdu2irr31i5ridbh"
  file_data = filebase64(var.ca_certificate_file)
}
resource "pingfederate_keypairs_ssl_server_settings" "internal" {
  admin_console_cert_ref      = { id = "sslservercert" }
  active_admin_console_certs  = [{ id = "sslservercert" }]
  runtime_server_cert_ref     = { id = "sslservercert" }
  active_runtime_server_certs = [{ id = "sslservercert" }]
}
resource "pingfederate_data_store" "directory" {
  data_store_id = "LDAP-D803C87FAB2ADFB4B0A947B64BA6F0C6093A5CA3"
  ldap_data_store = {
    name               = "pingdirectory"
    ldap_type          = "PING_DIRECTORY"
    user_dn            = "uid=pingfederate,ou=services,${var.base_dn}"
    password           = var.ldap_password
    bind_anonymously   = false
    use_ssl            = true
    use_start_tls      = false
    verify_host        = true
    use_dns_srv_record = false
    hostnames          = ["pd-pingdirectoryproxy.identity-proxy.svc.cluster.local:636"]
    hostnames_tags = [{
      default_source = true
      hostnames      = ["pd-pingdirectoryproxy.identity-proxy.svc.cluster.local:636"]
    }]
    min_connections        = 2
    max_connections        = 20
    connection_timeout     = 5000
    read_timeout           = 5000
    create_if_necessary    = true
    test_on_borrow         = true
    test_on_return         = false
    max_wait               = 5000
    time_between_evictions = 60000
    dns_ttl                = 60000
  }
  depends_on = [pingfederate_certificate_ca.internal]
}

# Adopt the objects installed by the pinned, sanitized baseline, retaining IDs
# referenced by its adapters. No second LDAP store or replacement bootstrap.
import {
  to = pingfederate_server_settings.identity
  id = "server-settings"
}
import {
  to = pingfederate_server_settings_general.identity
  id = "general-settings"
}
import {
  to = pingfederate_certificate_ca.internal
  id = "50z1hm968rdu2irr31i5ridbh"
}
import {
  to = pingfederate_keypairs_ssl_server_settings.internal
  id = "ssl-settings"
}
import {
  to = pingfederate_data_store.directory
  id = "LDAP-D803C87FAB2ADFB4B0A947B64BA6F0C6093A5CA3"
}
