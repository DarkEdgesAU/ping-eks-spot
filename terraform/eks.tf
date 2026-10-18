data "aws_iam_policy_document" "cluster" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "cluster" {
  name               = "${var.cluster_name}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.cluster.json
}
resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}
resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.cluster_name}/cluster"
  retention_in_days = 30
}
resource "aws_eks_cluster" "main" {
  name                      = var.cluster_name
  version                   = var.kubernetes_version
  role_arn                  = aws_iam_role.cluster.arn
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  vpc_config {
    subnet_ids              = [for subnet in aws_subnet.private : subnet.id]
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.api_public_access_cidrs
  }
  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
}
resource "aws_eks_access_entry" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.admin_principal_arn
  type          = "STANDARD"
}
resource "aws_eks_access_policy_association" "admin" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.admin.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
}
data "aws_iam_policy_document" "nodes" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "nodes" {
  name               = "${var.cluster_name}-nodes"
  assume_role_policy = data.aws_iam_policy_document.nodes.json
}
resource "aws_iam_role_policy_attachment" "nodes" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryReadOnly", "AmazonEKS_CNI_Policy"])
  role       = aws_iam_role.nodes.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}
resource "aws_eks_node_group" "spot" {
  for_each        = local.zones
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "spot-${each.value}"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = [aws_subnet.private[each.key].id]
  capacity_type   = "SPOT"
  instance_types  = var.instance_types
  ami_type        = "AL2023_x86_64_STANDARD"
  disk_size       = 80
  labels          = { "identity.ping.example/capacity" = "spot" }
  scaling_config {
    min_size     = 1
    desired_size = var.spot_desired_per_az
    max_size     = var.spot_max_per_az
  }
  update_config {
    max_unavailable = 1
  }
  depends_on = [aws_iam_role_policy_attachment.nodes, aws_route_table_association.private]
}
resource "aws_eks_node_group" "on_demand" {
  for_each        = var.enable_on_demand ? local.zones : {}
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "ondemand-${each.value}"
  node_role_arn   = aws_iam_role.nodes.arn
  subnet_ids      = [aws_subnet.private[each.key].id]
  capacity_type   = "ON_DEMAND"
  instance_types  = var.instance_types
  ami_type        = "AL2023_x86_64_STANDARD"
  disk_size       = 80
  labels          = { "identity.ping.example/capacity" = "on-demand" }
  scaling_config {
    min_size     = 1
    desired_size = var.on_demand_desired_per_az
    max_size     = 4
  }
  update_config {
    max_unavailable = 1
  }
  depends_on = [aws_iam_role_policy_attachment.nodes, aws_route_table_association.private]
}
locals {
  core_addon_names = toset(["vpc-cni", "kube-proxy", "coredns", "eks-pod-identity-agent"])
}

data "aws_eks_addon_version" "core" {
  for_each           = local.core_addon_names
  addon_name         = each.key
  kubernetes_version = aws_eks_cluster.main.version
  most_recent        = true
}
resource "aws_eks_addon" "core" {
  for_each                    = local.core_addon_names
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = each.key
  addon_version               = data.aws_eks_addon_version.core[each.key].version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  depends_on                  = [aws_eks_node_group.spot]
}
data "aws_iam_policy_document" "ebs" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "ebs" {
  name               = "${var.cluster_name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs.json
}
resource "aws_iam_role_policy_attachment" "ebs" {
  role       = aws_iam_role.ebs.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEBSCSIDriverPolicyV2"
}
resource "aws_eks_pod_identity_association" "ebs" {
  cluster_name    = aws_eks_cluster.main.name
  namespace       = "kube-system"
  service_account = "ebs-csi-controller-sa"
  role_arn        = aws_iam_role.ebs.arn
}
data "aws_eks_addon_version" "ebs" {
  addon_name         = "aws-ebs-csi-driver"
  kubernetes_version = aws_eks_cluster.main.version
  most_recent        = true
}
resource "aws_eks_addon" "ebs" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "aws-ebs-csi-driver"
  addon_version               = data.aws_eks_addon_version.ebs.version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  depends_on                  = [aws_eks_addon.core, aws_eks_pod_identity_association.ebs, aws_iam_role_policy_attachment.ebs]
}
data "aws_ecr_repository" "images" {
  for_each = toset(["pingdirectory", "pingdirectoryproxy", "pingfederate", "pingaccess"])
  name     = "${var.cluster_name}/${each.key}"
}

# Ownership moves to foundation/. Forget old addresses without deleting images.
removed {
  from = aws_ecr_repository.images
  lifecycle {
    destroy = false
  }
}
