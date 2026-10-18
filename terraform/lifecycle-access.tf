variable "lifecycle_principal_arn" {
  type        = string
  default     = null
  description = "Persistent GitHub OIDC role allowed to restore workloads"
}
resource "aws_eks_access_entry" "lifecycle" {
  count         = var.lifecycle_principal_arn == null ? 0 : 1
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.lifecycle_principal_arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "lifecycle" {
  count         = var.lifecycle_principal_arn == null ? 0 : 1
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.lifecycle[0].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
