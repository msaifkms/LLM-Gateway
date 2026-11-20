terraform {
  # 1.11+ for S3-native state locking (use_lockfile) in the main config.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # This config creates the bucket that holds the main config's state, so its
  # own (tiny, secret-free) state stays local: infra/bootstrap/terraform.tfstate,
  # which .gitignore keeps out of git.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project    = var.project
      managed-by = "terraform"
      stack      = "bootstrap"
    }
  }
}
