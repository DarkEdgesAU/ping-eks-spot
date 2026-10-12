variable "region" {
  type    = string
  default = "ap-southeast-2"
}
variable "cluster_name" {
  type    = string
  default = "pd-spot-dev"
}
variable "kubernetes_version" {
  type    = string
  default = "1.35"
}
variable "availability_zones" {
  type    = list(string)
  default = ["ap-southeast-2a", "ap-southeast-2b", "ap-southeast-2c"]
  validation {
    condition     = length(distinct(var.availability_zones)) == 3
    error_message = "Specify three distinct AZs in the selected region."
  }
}
variable "admin_principal_arn" {
  type        = string
  description = "IAM role/user ARN used by your remote development session (not an STS assumed-role ARN)."
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/", var.admin_principal_arn))
    error_message = "Use an IAM role or user ARN, not an STS session ARN."
  }
}
variable "api_public_access_cidrs" {
  type        = list(string)
  description = "Public IP CIDRs allowed to reach the EKS API, e.g. your remote host's egress IP/32."
  validation {
    condition     = length(var.api_public_access_cidrs) > 0 && alltrue([for c in var.api_public_access_cidrs : can(cidrhost(c, 0)) && c != "0.0.0.0/0" && c != "::/0"])
    error_message = "Provide explicit valid CIDRs, unrestricted public API access is not accepted."
  }
}
variable "instance_types" {
  type    = list(string)
  default = ["m6i.xlarge", "m6a.xlarge", "m5.xlarge", "m5a.xlarge"]
  validation {
    condition     = length(distinct(var.instance_types)) >= 2
    error_message = "Use at least two compatible x86 instance types to diversify Spot capacity."
  }
}
variable "spot_desired_per_az" {
  type    = number
  default = 2
}
variable "spot_max_per_az" {
  type    = number
  default = 4
}
variable "enable_on_demand" {
  type    = bool
  default = false
}
variable "on_demand_desired_per_az" {
  type    = number
  default = 1
}
variable "vpc_cidr" {
  type    = string
  default = "10.70.0.0/16"
}
