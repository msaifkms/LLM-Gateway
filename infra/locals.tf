data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  # Regular AZs only, no Local Zones.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)

  container_name = "gateway"
  container_port = 8000

  # SSM paths. Provider keys are injected into the task; issued gateway keys
  # (issue-key --to-ssm) are written under key_path by one-off admin tasks.
  ssm_prefix   = "/${var.project}"
  ssm_key_path = "/${var.project}/keys"

  https = var.certificate_arn != ""
}
