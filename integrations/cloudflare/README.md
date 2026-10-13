`eks.tf` is also installed in the user's named `cloudflare-ssl` project. Its
defaults create nothing. Supply actual ALB DNS targets and existing reusable
Access policy IDs there, after EKS ingress exists:

```hcl
eks_alb_hosts = {
  argocd = {
    alb_dns_name      = "ACTUAL-ARGO-ALB.ap-southeast-2.elb.amazonaws.com"
    access_policy_ids = ["EXISTING-REUSABLE-POLICY-ID"]
  }
  admin = {
    alb_dns_name      = "ACTUAL-PF-ADMIN-ALB.ap-southeast-2.elb.amazonaws.com"
    access_policy_ids = ["EXISTING-REUSABLE-POLICY-ID"]
  }
  id = {
    alb_dns_name = "ACTUAL-PF-ENGINE-ALB.ap-southeast-2.elb.amazonaws.com"
  }
}
```

Leave the Access policy contents managed where they already are. The companion
only references IDs and creates exact-host applications; if an exact-host app
already exists, import it before applying to avoid duplicate ownership. A
wildcard Access application may already cover the host: inspect precedence and
reuse the intended existing deployment instead of adding a conflicting app.

If no active edge certificate covers `*.eks.darkedges.com`, explicitly enable
`enable_eks_edge_certificate=true`. This adds a separate pack rather than
editing/replacing the existing zone-wide pack. Review certificate limits and
wait for issuance before DNS cutover. It does not provide the ALB's ACM origin
certificate or change the zone's encryption mode. Use Full (strict), disable
caching on these hosts, and configure tenant-specific origin protection.

No live Cloudflare changes or backend migration are performed by adding this
file. Review a plan in the Cloudflare project before applying; its existing
backend must be initialized using that project's documented workflow.
