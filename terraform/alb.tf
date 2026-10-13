variable "enable_federate" {
  description = "Provision the AWS IAM integration for the separately installed ALB controller."
  type        = bool
  default     = false
}
data "aws_iam_policy_document" "alb_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "alb" {
  count              = var.enable_federate ? 1 : 0
  name               = "${var.cluster_name}-alb-controller"
  assume_role_policy = data.aws_iam_policy_document.alb_assume.json
}
resource "aws_iam_policy" "alb" {
  count       = var.enable_federate ? 1 : 0
  name        = "${var.cluster_name}-alb-controller"
  description = "Official AWS Load Balancer Controller v2.16.0 policy"
  policy      = file("${path.module}/policies/aws-load-balancer-controller-v2.16.0.json")
}
resource "aws_iam_role_policy_attachment" "alb" {
  count      = var.enable_federate ? 1 : 0
  role       = aws_iam_role.alb[0].name
  policy_arn = aws_iam_policy.alb[0].arn
}
resource "aws_eks_pod_identity_association" "alb" {
  count           = var.enable_federate ? 1 : 0
  cluster_name    = aws_eks_cluster.main.name
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"
  role_arn        = aws_iam_role.alb[0].arn
  depends_on      = [aws_eks_addon.core, aws_iam_role_policy_attachment.alb]
}
output "vpc_id" { value = aws_vpc.main.id }
output "alb_controller_role_arn" { value = try(aws_iam_role.alb[0].arn, null) }
