terraform {
  required_version = ">= 1.13.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0, < 7.0" }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = ["447061058131"]
  default_tags {
    tags = { Project = var.cluster_name, ManagedBy = "Terraform", Lifecycle = "persistent" }
  }
}

variable "region" { default = "ap-southeast-2" }
variable "cluster_name" { default = "pd-spot-dev" }
variable "github_oidc_subject" {
  type        = string
  description = "Exact verified post-transfer subject for protected eks-lifecycle environment"
}
variable "github_oidc_provider_arn" {
  type        = string
  default     = null
  description = "Existing persistent GitHub OIDC provider ARN; shared provider is not recreated"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_oidc_provider_arn == null ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  lifecycle { prevent_destroy = true }
}
variable "deployment_policy_json" {
  type        = string
  description = "Reviewed least-privilege policy for disposable EKS provisioning; no AdministratorAccess default"
}

resource "aws_kms_key" "lifecycle" {
  description             = "Persistent Ping lifecycle backup and state envelope encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  lifecycle { prevent_destroy = true }
}
resource "aws_kms_alias" "lifecycle" {
  name          = "alias/${var.cluster_name}-lifecycle"
  target_key_id = aws_kms_key.lifecycle.key_id
}
resource "aws_s3_bucket" "durable" {
  for_each      = toset(["state", "backups"])
  bucket        = "${var.cluster_name}-447061058131-${each.key}"
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_versioning" "durable" {
  for_each = aws_s3_bucket.durable
  bucket   = each.value.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_public_access_block" "durable" {
  for_each                = aws_s3_bucket.durable
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "durable" {
  for_each = aws_s3_bucket.durable
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.lifecycle.arn
    }
    bucket_key_enabled = true
  }
}
resource "aws_s3_bucket_policy" "durable" {
  for_each = aws_s3_bucket.durable
  bucket   = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [each.value.arn, "${each.value.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_ecr_repository" "images" {
  for_each             = toset(["pingdirectory", "pingdirectoryproxy", "pingfederate", "pingaccess"])
  name                 = "${var.cluster_name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role" "lifecycle" {
  name                 = "${var.cluster_name}-github-lifecycle"
  max_session_duration = 14400
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = var.github_oidc_provider_arn != null ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = var.github_oidc_subject
    } }
  }] })
}
resource "aws_iam_role_policy" "deployment" {
  name   = "reviewed-eks-deployment"
  role   = aws_iam_role.lifecycle.id
  policy = var.deployment_policy_json
}
resource "aws_iam_role_policy" "storage" {
  name = "persistent-lifecycle-storage"
  role = aws_iam_role.lifecycle.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = [for b in aws_s3_bucket.durable : b.arn] },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = ["${aws_s3_bucket.durable["state"].arn}/eks/*", "${aws_s3_bucket.durable["backups"].arn}/*"] },
    { Effect = "Allow", Action = ["s3:DeleteObject"], Resource = ["${aws_s3_bucket.durable["state"].arn}/eks/*.tflock", "${aws_s3_bucket.durable["backups"].arn}/lifecycle/operation.lock"] },
    { Effect = "Allow", Action = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"], Resource = [aws_kms_key.lifecycle.arn] }
  ] })
}
output "state_bucket" { value = aws_s3_bucket.durable["state"].id }
output "backup_bucket" { value = aws_s3_bucket.durable["backups"].id }
output "kms_key_arn" { value = aws_kms_key.lifecycle.arn }
output "role_arn" { value = aws_iam_role.lifecycle.arn }
