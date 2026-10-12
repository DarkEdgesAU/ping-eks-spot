# Sources and vendored material

The vendored Ping Helm chart 0.16.0 is from Ping Identity's public chart repository:
https://helm.pingidentity.com/index.yaml
https://github.com/pingidentity/helm-charts/releases/tag/ping-devops-0.16.0

Profile patterns were checked against Ping's published DevOps examples and source at commit `804766b0720d0bcdd2e0b0270b0e711d66e6f601`. This package does not include example passwords or private keys from those profiles.
https://github.com/pingidentity/pingidentity-server-profiles
https://developer.pingidentity.com/devops/docker-images/pingdirectory/README.html
https://developer.pingidentity.com/devops/deployment/deployPDMultiRegion.html
https://developer.pingidentity.com/devops/deployment/deployPDProxyBackendDiscovery.html
https://developer.pingidentity.com/devops/reference/profileStructures.html

AWS and Terraform references:
https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html
https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html
https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/eks_node_group
https://developer.hashicorp.com/terraform/language/providers/requirements

Product license and EULA obligations remain with the operator. No product binaries or license keys are redistributed by this project.
