terraform {
  backend "s3" {}
  required_version = ">= 1.13.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0, < 7.0" }
  }
}
provider "aws" {
  allowed_account_ids = ["447061058131"]
  region              = var.region
  default_tags {
    tags = { Project = var.cluster_name, ManagedBy = "Terraform" }
  }
}
