output "cluster_name" {
  value = aws_eks_cluster.main.name
}
output "region" {
  value = var.region
}
output "availability_zones" {
  value = var.availability_zones
}
output "repositories" {
  value = { for name, repo in aws_ecr_repository.images : name => repo.repository_url }
}
output "addon_versions" {
  value = merge({ for name, addon in aws_eks_addon.core : name => addon.addon_version }, { aws-ebs-csi-driver = aws_eks_addon.ebs.addon_version })
}

output "cluster_arn" {
  value = aws_eks_cluster.main.arn
}
