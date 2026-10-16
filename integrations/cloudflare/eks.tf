# Companion for E:/development/projects/cloudflare/cloudflare-ssl.
# Defaults are empty: no existing certificates, Access policies or DNS change.
variable "eks_alb_hosts" {
  description = "EKS public ALBs keyed by argocd, admin or id. Reuse existing Access policy IDs for administrative hosts."
  type = map(object({
    alb_dns_name      = string
    access_policy_ids = optional(list(string), [])
  }))
  default = {}
  validation {
    condition = alltrue([for name, host in var.eks_alb_hosts :
      contains(["argocd", "admin", "id"], name) &&
      endswith(host.alb_dns_name, ".elb.amazonaws.com") &&
      (name == "id" || length(host.access_policy_ids) > 0)
    ])
    error_message = "Use argocd/admin/id with actual ALB DNS names; argocd and admin require existing Access policy IDs."
  }
}
variable "enable_eks_edge_certificate" {
  description = "Order a separate companion certificate pack for *.eks.darkedges.com without replacing the current zone pack."
  type        = bool
  default     = false
}
resource "cloudflare_dns_record" "eks_alb" {
  for_each = var.eks_alb_hosts
  zone_id  = var.zone_id
  name     = "${each.key}.eks.darkedges.com"
  type     = "CNAME"
  content  = each.value.alb_dns_name
  proxied  = true
  ttl      = 1
  comment  = "EKS ALB origin; managed alongside Cloudflare Access"
}
resource "cloudflare_zero_trust_access_application" "eks_admin" {
  for_each         = { for name, host in var.eks_alb_hosts : name => host if length(host.access_policy_ids) > 0 }
  account_id       = var.account_id
  name             = "EKS ${each.key}"
  domain           = "${each.key}.eks.darkedges.com"
  type             = "self_hosted"
  session_duration = "8h"
  policies         = [for id in each.value.access_policy_ids : { id = id }]
}
resource "cloudflare_certificate_pack" "eks_edge" {
  count                 = var.enable_eks_edge_certificate ? 1 : 0
  zone_id               = var.zone_id
  type                  = "advanced"
  hosts                 = ["darkedges.com", "*.eks.darkedges.com"]
  certificate_authority = var.certificate_authority
  validation_method     = var.validation_method
  validity_days         = var.validity_days
  cloudflare_branding   = var.cloudflare_branding
  lifecycle { create_before_destroy = true }
}
